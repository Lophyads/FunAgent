"""Workspace-scoped structured file tools."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from agentcore.interfaces import ToolContext
from .registry import ToolEntry, ToolRegistry


_IGNORED_NAMES = {".env", ".git", ".venv", "__pycache__", "node_modules"}
_MAX_TEXT_BYTES = 2_000_000


class ListDirectoryArgs(BaseModel):
    path: str = "."
    max_entries: int = Field(default=100, ge=1, le=500)


class ReadFileArgs(BaseModel):
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=500)


class SearchFilesArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    glob: str = Field(default="*", min_length=1, max_length=200)
    max_results: int = Field(default=50, ge=1, le=200)


class WriteFileArgs(BaseModel):
    path: str = Field(min_length=1)
    content: str = Field(max_length=500_000)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class PatchFileArgs(BaseModel):
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1, max_length=200_000)
    new_text: str = Field(max_length=200_000)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


def _resolve(root: Path, raw_path: str) -> Path:
    candidate = (root / raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("path is outside the workspace")
    relative = candidate.relative_to(root)
    if any(part in _IGNORED_NAMES for part in relative.parts):
        raise ValueError("path is protected")
    return candidate


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError("path is not a regular file")
    if path.stat().st_size > _MAX_TEXT_BYTES:
        raise ValueError("file is too large")
    return path.read_text(encoding="utf-8")


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=".funagent-", delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register_tools(registry: ToolRegistry) -> None:
    root = registry.workspace_root
    workspace_available = lambda: root.is_dir()

    def list_directory(args: ListDirectoryArgs, context: ToolContext) -> dict:
        path = _resolve(root, args.path)
        if not path.is_dir():
            raise ValueError("path is not a directory")
        entries = []
        for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
            if child.name in _IGNORED_NAMES:
                continue
            entries.append({
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
            })
            if len(entries) >= args.max_entries:
                break
        return {"path": str(path.relative_to(root)), "entries": entries}

    def read_file(args: ReadFileArgs, context: ToolContext) -> dict:
        path = _resolve(root, args.path)
        lines = _read_text(path).splitlines()
        start = args.start_line - 1
        selected = [
            {"line": number, "text": line[:1000]}
            for number, line in enumerate(
                lines[start:start + args.max_lines], start=args.start_line
            )
        ]
        return {
            "path": str(path.relative_to(root)),
            "lines": selected,
            "truncated": start + len(selected) < len(lines),
        }

    def search_files(args: SearchFilesArgs, context: ToolContext) -> dict:
        base = _resolve(root, args.path)
        if not base.is_dir():
            raise ValueError("path is not a directory")
        matches: list[dict[str, object]] = []
        scanned = 0
        for path in sorted(base.rglob(args.glob)):
            if any(part in _IGNORED_NAMES for part in path.relative_to(root).parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > _MAX_TEXT_BYTES:
                continue
            scanned += 1
            if scanned > 1_000:
                return {"matches": matches, "truncated": True}
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for number, line in enumerate(lines, start=1):
                if args.query.casefold() not in line.casefold():
                    continue
                matches.append({
                    "path": str(path.relative_to(root)),
                    "line": number,
                    "text": line[:300],
                })
                if len(matches) >= args.max_results:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def write_file(args: WriteFileArgs, context: ToolContext) -> dict:
        path = _resolve(root, args.path)
        if path == root or path.is_symlink() or path.is_dir():
            raise ValueError("path must be a workspace file")
        if path.exists():
            current = _read_text(path)
            if args.expected_sha256 is None:
                raise ValueError("expected_sha256 is required to replace an existing file")
            if _sha256_text(current) != args.expected_sha256.lower():
                raise ValueError("file changed since it was read")
        elif args.expected_sha256 is not None:
            raise ValueError("expected file does not exist")
        _atomic_write(path, args.content)
        return {
            "path": str(path.relative_to(root)),
            "bytes": len(args.content.encode("utf-8")),
            "sha256": _sha256_text(args.content),
        }

    def patch_file(args: PatchFileArgs, context: ToolContext) -> dict:
        path = _resolve(root, args.path)
        current = _read_text(path)
        if (
            args.expected_sha256 is not None
            and _sha256_text(current) != args.expected_sha256.lower()
        ):
            raise ValueError("file changed since it was read")
        occurrences = current.count(args.old_text)
        if occurrences != 1:
            raise ValueError(
                f"old_text must occur exactly once; found {occurrences} occurrences"
            )
        updated = current.replace(args.old_text, args.new_text, 1)
        _atomic_write(path, updated)
        return {
            "path": str(path.relative_to(root)),
            "bytes": len(updated.encode("utf-8")),
            "sha256": _sha256_text(updated),
        }

    read_entries = (
        ("list_directory", "List entries in a workspace directory.", ListDirectoryArgs, list_directory),
        ("read_file", "Read numbered UTF-8 lines from a workspace file.", ReadFileArgs, read_file),
        ("search_files", "Search literal text in UTF-8 workspace files.", SearchFilesArgs, search_files),
    )
    for name, description, args_model, handler in read_entries:
        registry.register(ToolEntry(
            tool_id=f"builtin.{name}", implementation_version="1",
            name=name, description=description, args_model=args_model,
            handler=handler, toolset="file", check_fn=workspace_available,
            side_effects="none",
        ))

    registry.register(ToolEntry(
        tool_id="builtin.write_file", implementation_version="1",
        name="write_file",
        description=(
            "Create a UTF-8 workspace file, or replace one using its expected SHA-256."
        ),
        args_model=WriteFileArgs, handler=write_file, toolset="file",
        check_fn=workspace_available,
        required_permissions=frozenset({"file.write"}),
        requires_approval=True, side_effects="possible",
    ))
    registry.register(ToolEntry(
        tool_id="builtin.patch_file", implementation_version="1",
        name="patch_file",
        description=(
            "Replace one exact text occurrence in a UTF-8 workspace file."
        ),
        args_model=PatchFileArgs, handler=patch_file, toolset="file",
        check_fn=workspace_available,
        required_permissions=frozenset({"file.write"}),
        requires_approval=True, side_effects="possible",
    ))
