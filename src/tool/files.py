"""Bounded workspace IO; path checks also reject Windows reparse points."""
from pathlib import Path, PureWindowsPath
import os
import stat
from fnmatch import fnmatchcase
import hashlib
import tempfile
from threading import Lock
from contextlib import contextmanager
from ..tool.contracts import ToolFailure

_mutations = Lock()


@contextmanager
def Mutation(context):
    while not _mutations.acquire(timeout=0.02):
        context.CheckCancelled()
    try:
        context.CheckCancelled()
        yield
    finally:
        _mutations.release()


def AtomicWrite(target, content, context, raw_path):
    if len(content.encode("utf8")) > MAX_TEXT_BYTES:
        raise DomainError("INVALID_REQUEST", "File exceeds text limit")
    temporary, directory_effect = None, False
    try:
        context.CheckCancelled()
        if not target.parent.exists():
            target.parent.mkdir(parents=True)
            directory_effect = True
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".funagent-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode("utf8"))
            stream.flush()
            os.fsync(stream.fileno())
        context.CheckCancelled()
        if Resolve(raw_path, context) != target:
            raise DomainError("DENIED", "Workspace path changed")
        os.replace(temporary, target)
        temporary = None
    except Exception:
        if directory_effect:
            raise ToolFailure("FILE_IO", effect="known") from None
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def WriteFile(args, context):
    with Mutation(context):
        target = Resolve(args["path"], context)
        expected = args.get("expected_sha256")
        if target.exists():
            current = ReadText(target, context)
            if expected is None or hashlib.sha256(current.encode("utf8")).hexdigest() != expected.lower():
                raise DomainError("CONFLICT", "Expected file content changed")
        elif expected is not None:
            raise DomainError("CONFLICT", "Expected file no longer exists")
        AtomicWrite(target, args["content"], context, args["path"])
        return {"path": args["path"], "sha256": hashlib.sha256(args["content"].encode("utf8")).hexdigest(), "written": True}


def PatchFile(args, context):
    with Mutation(context):
        target = Resolve(args["path"], context)
        current = ReadText(target, context)
        expected = args.get("expected_sha256")
        if (expected is not None and hashlib.sha256(current.encode("utf8")).hexdigest() != expected.lower()
                or current.count(args["old_text"]) != 1):
            raise DomainError("CONFLICT", "Patch does not match current content")
        changed = current.replace(args["old_text"], args["new_text"], 1)
        AtomicWrite(target, changed, context, args["path"])
        return {"path": args["path"], "sha256": hashlib.sha256(changed.encode("utf8")).hexdigest(), "patched": True}

from ..frame.contracts import DomainError

MAX_TEXT_BYTES = 2000000
PROTECTED = {".git", ".venv", "__pycache__", "node_modules", ".funagent"}


def Protected(name):
    if os.name == "nt":
        name = name.casefold()
    return name in PROTECTED or name == ".env" or name.startswith(".env.")


def Link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def Resolve(raw, context):
    context.CheckCancelled()
    path, windows = Path(raw), PureWindowsPath(raw)
    if path.anchor or windows.anchor or ".." in path.parts or ".." in windows.parts or ":" in raw:
        raise DomainError("DENIED", "Path must stay within the workspace")
    root = Path(context.workspace).resolve(strict=True)
    # Backslashes are separators on every platform, so Windows-shaped escapes stay rejected.
    parts = windows.parts
    current = root
    for part in parts:
        if Protected(part):
            raise DomainError("DENIED", "Protected workspace path")
        current /= part
        if os.path.lexists(current) and Link(current):
            raise DomainError("DENIED", "Workspace links are not allowed")
    target = current.resolve()
    if not target.is_relative_to(root):
        raise DomainError("DENIED", "Path must stay within the workspace")
    context.CheckCancelled()
    return target


def ReadText(target, context):
    context.CheckCancelled()
    if not stat.S_ISREG(target.stat().st_mode) or target.stat().st_size > MAX_TEXT_BYTES:
        raise DomainError("INVALID_REQUEST", "File must be bounded regular text")
    with target.open("rb") as stream:
        raw = stream.read(MAX_TEXT_BYTES + 1)
    context.CheckCancelled()
    if len(raw) > MAX_TEXT_BYTES or b"\0" in raw:
        raise DomainError("INVALID_REQUEST", "File must be bounded regular text")
    return raw.decode("utf8")


def Walk(base, context, depth):
    if not base.is_dir():
        raise DomainError("INVALID_REQUEST", "Path must be a directory")
    pending, rows, scanned, truncated = [(base, 1)], [], 0, False
    while pending:
        directory, level = pending.pop()
        context.CheckCancelled()
        try:
            with os.scandir(directory) as iterator:
                for item in iterator:
                    context.CheckCancelled()
                    scanned += 1
                    if scanned > 5000:
                        return sorted(rows, key=lambda r: r[0]), True
                    try:
                        target = Path(item.path)
                        if Protected(item.name) or Link(target):
                            continue
                        relative = target.relative_to(base).as_posix()
                        is_directory = item.is_dir(follow_symlinks=False)
                        rows.append((relative, target, is_directory))
                        if is_directory:
                            if level < depth:
                                pending.append((target, level + 1))
                            else:
                                truncated = True
                    except OSError:
                        continue
        except PermissionError:
            truncated = True
    return sorted(rows, key=lambda r: r[0]), truncated


def ListDirectory(args, context):
    base = Resolve(args.get("path", "."), context)
    rows, bounded = Walk(base, context, args.get("depth", 1))
    start, maximum = args.get("cursor", 0), args.get("max_entries", 100)
    page = rows[start:start + maximum]
    return {"path": args.get("path", "."), "entries": [
        {"name": name, "type": "directory" if directory else "file"} for name, _, directory in page],
        "next_cursor": start + len(page) if start + len(page) < len(rows) else None,
        "truncated": bounded or start + len(page) < len(rows)}


def SearchFiles(args, context):
    base = Resolve(args.get("path", "."), context)
    rows, truncated = Walk(base, context, args.get("depth", 8))
    matches, unreadable, count = [], 0, 0
    maximum, query = args.get("max_results", 50), args["query"].casefold()
    mode = args.get("mode", "content")
    for name, target, directory in rows:
        context.CheckCancelled()
        if directory or not fnmatchcase(name, args.get("glob", "*")):
            continue
        count += 1
        if count > 1000:
            truncated = True
            break
        if mode in {"name", "both"} and query in name.casefold():
            matches.append({"path": name, "line": None, "text": target.name})
        if mode in {"content", "both"}:
            try:
                lines = ReadText(Resolve(str(target.relative_to(Path(context.workspace).resolve())), context), context).splitlines()
                for number, line in enumerate(lines, 1):
                    context.CheckCancelled()
                    if query in line.casefold():
                        matches.append({"path": name, "line": number, "text": line[:300]})
                        if len(matches) >= maximum:
                            return {"matches": matches[:maximum], "truncated": True, "unreadable": unreadable}
            except (OSError, UnicodeError, DomainError) as error:
                from ..frame.contracts import ControlError
                if isinstance(error, ControlError):
                    raise
                unreadable += 1
        if len(matches) >= maximum:
            return {"matches": matches[:maximum], "truncated": True, "unreadable": unreadable}
    return {"matches": matches, "truncated": truncated, "unreadable": unreadable}


def ReadFile(args, context):
    text = ReadText(Resolve(args["path"], context), context)
    lines = text.splitlines()
    start, maximum = args.get("start_line", 1), args.get("max_lines", 200)
    chosen = lines[start - 1:start - 1 + maximum]
    truncated = start - 1 + len(chosen) < len(lines) or any(len(line) > 1000 for line in chosen)
    return {"path": args["path"], "lines": [{"line": start + i, "text": line[:1000]} for i, line in enumerate(chosen)],
            "truncated": truncated, "next_line": start + len(chosen) if start - 1 + len(chosen) < len(lines) else None,
            "sha256": hashlib.sha256(text.encode("utf8")).hexdigest()}


OPERATIONS = {"list_directory": ListDirectory, "search_files": SearchFiles, "read_file": ReadFile,
              "write_file": WriteFile, "patch_file": PatchFile}
