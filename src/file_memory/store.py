"""Atomic, cross-process safe storage for the two small memory documents."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import portalocker

logger = logging.getLogger(__name__)
Target = Literal["memory", "user"]
Action = Literal["add", "replace", "remove"]
SEPARATOR = "\n§\n"
_NAMES = {"memory": "MEMORY.md", "user": "USER.md"}


class MemoryError(ValueError):
    """A memory document cannot be read or safely changed."""


class MemoryCapacityError(MemoryError):
    """The requested write would exceed a document's character limit."""


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    target: Target
    entries: tuple[str, ...]
    version: str
    chars: int


@dataclass(frozen=True, slots=True)
class MemoryOperation:
    action: Action
    target: Target
    content: str = ""
    old_text: str = ""


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    target: Target
    action: Action
    changed: bool
    version: str
    chars: int
    entries: int


class FileMemoryStore:
    def __init__(self, directory: Path, *, memory_char_limit: int = 2200,
                 user_char_limit: int = 1375) -> None:
        if memory_char_limit < 1 or user_char_limit < 1:
            raise ValueError("memory character limits must be positive")
        self._configured_directory = Path(directory).expanduser().absolute()
        self.directory = self._configured_directory.resolve()
        self.limits = {"memory": memory_char_limit, "user": user_char_limit}

    def _path(self, target: Target) -> Path:
        if target not in _NAMES:
            raise MemoryError("target must be memory or user")
        if self._configured_directory.is_symlink() or self.directory.is_symlink():
            raise MemoryError("memory directory must not be a symlink")
        path = self.directory / _NAMES[target]
        if path.is_symlink():
            raise MemoryError("memory file must not be a symlink")
        return path

    @staticmethod
    def _parse(target: Target, raw: str) -> MemoryDocument:
        if raw and ("\r" in raw or raw.startswith("\ufeff")):
            raise MemoryError("memory file has unsupported formatting")
        entries = tuple(raw.split(SEPARATOR)) if raw else ()
        if (len(set(entries)) != len(entries) or any(
            not item.strip() or item != item.strip() or "§" in item.split("\n")
            for item in entries
        )):
            raise MemoryError("memory file has malformed entries")
        return MemoryDocument(target, entries, hashlib.sha256(raw.encode("utf-8")).hexdigest(), len(raw))

    def _read_path(self, path: Path, target: Target) -> MemoryDocument:
        try:
            if not path.exists():
                return self._parse(target, "")
            if not path.is_file() or path.is_symlink():
                raise MemoryError("memory path is not a regular file")
            if path.stat().st_size > 1_000_000:
                raise MemoryError("memory file is too large")
            with path.open("r", encoding="utf-8", newline="") as handle:
                return self._parse(target, handle.read())
        except (OSError, UnicodeError) as exc:
            raise MemoryError(f"cannot read memory file: {type(exc).__name__}") from exc

    def read(self, target: Target) -> MemoryDocument:
        return self._read_path(self._path(target), target)

    def read_prompt_blocks(self, max_chars: int) -> tuple[str, ...]:
        remaining = max(0, max_chars)
        blocks: list[str] = []
        for target in ("user", "memory"):
            try:
                document = self.read(target)
            except MemoryError as exc:
                logger.warning("memory.load_failed target=%s error_type=%s", target, type(exc).__name__)
                continue
            for entry in document.entries:
                block = f"{_NAMES[target]}: {entry}"
                if len(block) <= remaining:
                    blocks.append(block)
                    remaining -= len(block)
        return tuple(blocks)

    def apply(self, operation: MemoryOperation) -> MemoryWriteResult:
        if operation.action not in {"add", "replace", "remove"}:
            raise MemoryError("invalid memory action")
        path = self._path(operation.target)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.is_symlink() or path.is_symlink():
                raise MemoryError("memory path must not be a symlink")
            if Path(str(path) + ".lock").is_symlink():
                raise MemoryError("memory lock path must not be a symlink")
            with portalocker.Lock(str(path) + ".lock", timeout=5):
                document = self._read_path(path, operation.target)
                entries = list(document.entries)
                if operation.action == "add":
                    content = self._entry(operation.content)
                    if content in entries:
                        return self._result(operation, document, False)
                    entries.append(content)
                else:
                    old = self._entry(operation.old_text)
                    matches = [i for i, item in enumerate(entries) if old in item]
                    if len(matches) != 1:
                        raise MemoryError(f"old_text must match exactly one entry; found {len(matches)}")
                    if operation.action == "replace":
                        content = self._entry(operation.content)
                        if content in entries and content != entries[matches[0]]:
                            raise MemoryError("replacement duplicates an existing entry")
                        entries[matches[0]] = content
                    else:
                        entries.pop(matches[0])
                raw = SEPARATOR.join(entries)
                if len(raw) > self.limits[operation.target] and (
                    operation.action == "add" or len(raw) >= document.chars
                ):
                    raise MemoryCapacityError("memory file character limit exceeded")
                if raw == SEPARATOR.join(document.entries):
                    return self._result(operation, document, False)
                self._atomic_write(path, raw)
                updated = self._parse(operation.target, raw)
                logger.info("memory.write target=%s action=%s version=%s", operation.target, operation.action, updated.version[:12])
                return self._result(operation, updated, True)
        except portalocker.LockException as exc:
            raise MemoryError("memory file is busy") from exc
        except OSError as exc:
            raise MemoryError(f"cannot write memory file: {type(exc).__name__}") from exc

    @staticmethod
    def _entry(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or value != value.strip() or "§" in value.split("\n") or "\r" in value:
            raise MemoryError("memory entry must be nonblank, trimmed, and contain no separator")
        return value

    @staticmethod
    def _result(operation: MemoryOperation, doc: MemoryDocument, changed: bool) -> MemoryWriteResult:
        return MemoryWriteResult(operation.target, operation.action, changed, doc.version, doc.chars, len(doc.entries))

    @staticmethod
    def _atomic_write(path: Path, raw: str) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix=".memory-", delete=False) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
