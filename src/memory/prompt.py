"""Select database records for frozen bootstrap context or explicit export."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from agentcore.prompt_builder import PromptBuildRequest, SystemPromptBuilder
from .store import MemoryError, MemoryQuery, MemoryStore

logger = logging.getLogger(__name__)


def iter_entries(store: MemoryStore, kind):
    cursor = None
    while True:
        page = store.list(MemoryQuery(kind, 200, cursor))
        yield from page.entries
        cursor = page.next_cursor
        if cursor is None:
            return


def format_entry(entry) -> str:
    subject = f" [{entry.subject}]" if entry.subject else ""
    return f"{entry.kind} id={entry.id} v{entry.version}{subject}: {entry.content}"


def core_blocks(store: MemoryStore, max_chars: int) -> tuple[str, ...]:
    remaining, blocks = max(0, max_chars), []
    for kind in ("user", "note"):
        if remaining <= 0:
            break
        for entry in iter_entries(store, kind):
            block = format_entry(entry)
            if len(block) <= remaining:
                blocks.append(block)
                remaining -= len(block)
            if remaining <= 0:
                break
    return tuple(blocks)


class MemoryPromptBuilder:
    def __init__(self, base: SystemPromptBuilder, store: MemoryStore, max_chars: int):
        self.base, self.store, self.max_chars = base, store, max_chars

    def build(self, request: PromptBuildRequest):
        available = max(0, self.max_chars - sum(map(len, request.bootstrap_memory)))
        try:
            blocks = core_blocks(self.store, available)
        except MemoryError as exc:
            logger.warning("memory.bootstrap_failed error_type=%s", type(exc).__name__)
            blocks = ()
        return self.base.build(replace(request, bootstrap_memory=(*request.bootstrap_memory, *blocks)))


def export_markdown(store: MemoryStore, directory: Path) -> dict[str, int]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result, outputs = {}, []
    for kind, filename in (("user", "USER.md"), ("note", "MEMORY.md")):
        target = directory / filename
        if target.exists() or target.is_symlink():
            raise MemoryError(f"export would overwrite {target}; choose a new output directory")
        entries = list(iter_entries(store, kind))
        lines = [f"# {filename}", "", "Generated database view. Editing this file does not update memory.", ""]
        lines.extend("- " + format_entry(entry).replace("\n", "\n  ") for entry in entries)
        outputs.append((target, "\n".join(lines) + "\n"))
        result[filename] = len(entries)
    for target, text in outputs:
        with target.open("x", encoding="utf-8", newline="") as file:
            file.write(text)
    return result


def import_legacy(store: MemoryStore, directory: Path) -> dict[str, int]:
    from .store import MemoryDraft
    # Validate both documents before writing; input files are never modified.
    documents = []
    if Path(directory).is_symlink():
        raise MemoryError("legacy directory must not be a symlink")
    for kind, filename in (("user", "USER.md"), ("note", "MEMORY.md")):
        path = Path(directory) / filename
        if path.is_symlink():
            raise MemoryError("legacy memory must not be a symlink")
        if not path.exists():
            continue
        if path.stat().st_size > 1_000_000:
            raise MemoryError(f"legacy document is too large: {path}")
        try:
            raw = path.read_bytes().decode("utf-8")
        except UnicodeError:
            raise MemoryError(f"legacy memory must be UTF-8: {path}") from None
        entries = raw.split("\n§\n") if raw else []
        if "\r" in raw or raw.startswith("\ufeff") or any(
            not item.strip() or item != item.strip() or "§" in item.split("\n") or len(item) > 5000 for item in entries
        ):
            raise MemoryError(f"malformed legacy memory: {path}")
        documents.append((kind, filename, entries))
    result = {"imported": 0, "duplicates": 0}
    for kind, filename, entries in documents:
        for content in entries:
            write = store.add(MemoryDraft(kind, content, source={"type": "legacy_import", "file": filename}))
            result["imported" if write.changed else "duplicates"] += 1
    return result
