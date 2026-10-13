"""Markdown-aware semantic block splitter."""

from __future__ import annotations

import re
from typing import Any, List, Optional

from src.libs.splitter.base_splitter import BaseSplitter

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+\S")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_IMAGE_ONLY = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")
_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_LIST_ITEM = re.compile(
    r"^\s*(?:"
    r"[-+*]\s+|"
    r"\d+[.、)]\s+|"
    r"[（(]\d+[）)]\s*|"
    r"\*\*\s*\d+\s*[.、)]\s*\*\*\s*"
    r")"
)
_BREAK_SEPARATOR_GROUPS = (
    ("\n",),
    ("。", "！", "？", ". ", "! ", "? ", "；", "; "),
    (" ",),
)
_DESCRIPTION_BIND_LIMIT = 200


class ParagraphSplitter(BaseSplitter):
    """Build chunks from Markdown structure rather than blank-line boundaries.

    Blank lines are ignored outside fenced code and tables. Headings define
    sections, consecutive list items form one block, and ordinary prose is
    accumulated by sentence until the configured size limit is reached.
    """

    def __init__(self, settings: Any, chunk_size: Optional[int] = None, **_: Any) -> None:
        ingestion = settings.ingestion
        self.chunk_size = chunk_size if chunk_size is not None else ingestion.chunk_size
        if not isinstance(self.chunk_size, int) or self.chunk_size <= 0:
            raise ValueError(f"chunk_size must be a positive integer, got: {self.chunk_size}")
        if ingestion.chunk_overlap != 0:
            raise ValueError("paragraph splitter requires ingestion.chunk_overlap=0")

    def split_text(
        self,
        text: str,
        trace: Optional[Any] = None,
        **kwargs: Any,
    ) -> List[str]:
        self.validate_text(text)
        blocks = self._parse_blocks(text)
        chunks: List[str] = []
        headings: List[tuple[int, str]] = []
        current: List[str] = []
        section_has_content = False

        def context() -> str:
            return "\n\n".join(body for _, body in headings)

        def render(parts: List[str]) -> str:
            return "\n\n".join(part for part in (context(), *parts) if part).strip()

        def flush() -> None:
            if current:
                chunks.append(render(current))
                current.clear()

        def current_is_images() -> bool:
            return bool(current) and all(_IMAGE_ONLY.match(part) for part in current)

        def add_unit(body: str) -> None:
            candidate = render([*current, body])
            if not current or len(candidate) <= self.chunk_size:
                current.append(body)
                return
            # A leading image must stay with the first content that explains it.
            if current_is_images():
                current.append(body)
                flush()
                return
            flush()
            current.append(body)

        for kind, body in blocks:
            if kind == "heading":
                flush()
                level = len(body) - len(body.lstrip("#"))
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, body))
                section_has_content = False
                continue

            capacity = self._content_capacity(context())

            if kind == "image":
                # A leading image remains pending for the following content.
                # Otherwise it belongs to the preceding content in this section.
                if section_has_content and not current and chunks:
                    chunks[-1] = f"{chunks[-1]}\n\n{body}"
                else:
                    current.append(body)
                section_has_content = True
                continue

            section_has_content = True
            if kind in {"code", "table"}:
                current_body_length = len("\n\n".join(current))
                if current and (
                    current_body_length <= _DESCRIPTION_BIND_LIMIT or current_is_images()
                ):
                    current.append(body)
                else:
                    flush()
                    current.append(body)
                # Atomic structures stay intact and close the composite block.
                flush()
                continue

            parts = (
                self._split_list(body, capacity)
                if kind == "list"
                else self._split_prose(body, capacity)
            )
            for part in parts:
                add_unit(part)

        flush()
        if not section_has_content and headings:
            trailing = context()
            if not chunks or chunks[-1] != trailing:
                chunks.append(trailing)

        chunks = [chunk.strip() for chunk in chunks if chunk.strip()]
        self.validate_chunks(chunks)
        return chunks

    def _content_capacity(self, heading_context: str) -> int:
        overhead = len(heading_context) + 2 if heading_context else 0
        return max(1, self.chunk_size - overhead)

    def _split_prose(self, body: str, limit: int) -> List[str]:
        normalized = re.sub(r"[ \t]*\n[ \t]*", " ", body).strip()
        normalized = re.sub(r" {2,}", " ", normalized)
        if len(normalized) <= limit:
            return [normalized]

        result: List[str] = []
        remaining = normalized
        while remaining:
            part, remaining = self._take(remaining, limit)
            result.append(part)
        return result

    def _split_list(self, body: str, limit: int) -> List[str]:
        items: List[str] = []
        current_item: List[str] = []
        for line in body.splitlines():
            if _LIST_ITEM.match(line) and current_item:
                items.append(" ".join(current_item).strip())
                current_item = [line.strip()]
            else:
                current_item.append(line.strip())
        if current_item:
            items.append(" ".join(current_item).strip())

        groups: List[str] = []
        group: List[str] = []
        for item in items:
            if len(item) > limit:
                if group:
                    groups.append("\n".join(group))
                    group.clear()
                groups.extend(self._split_prose(item, limit))
                continue
            candidate = "\n".join((*group, item))
            if group and len(candidate) > limit:
                groups.append("\n".join(group))
                group = [item]
            else:
                group.append(item)
        if group:
            groups.append("\n".join(group))
        return groups

    @staticmethod
    def _take(text: str, limit: int) -> tuple[str, str]:
        if len(text) <= limit:
            return text.strip(), ""

        window = text[:limit]
        minimum = max(1, limit // 2)
        cut = 0
        for separators in _BREAK_SEPARATOR_GROUPS:
            candidates = [
                position + len(separator)
                for separator in separators
                if (position := window.rfind(separator)) >= minimum
            ]
            if candidates:
                cut = max(candidates)
                break
        if cut == 0:
            cut = limit
        return text[:cut].strip(), text[cut:].lstrip()

    @classmethod
    def _parse_blocks(cls, text: str) -> List[tuple[str, str]]:
        """Parse Markdown blocks without treating blank lines as boundaries."""
        blocks: List[tuple[str, str]] = []
        paragraph: List[str] = []
        list_lines: List[str] = []
        lines = text.splitlines()
        index = 0

        def flush_paragraph() -> None:
            if paragraph:
                blocks.append(("paragraph", "\n".join(paragraph).strip()))
                paragraph.clear()

        def flush_list() -> None:
            if list_lines:
                blocks.append(("list", "\n".join(list_lines).strip()))
                list_lines.clear()

        def flush_text() -> None:
            flush_paragraph()
            flush_list()

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if not stripped:
                index += 1
                continue

            fence_match = _FENCE.match(line)
            if fence_match:
                flush_text()
                marker = fence_match.group(1)
                fence = [line]
                index += 1
                while index < len(lines):
                    fence_line = lines[index]
                    fence.append(fence_line)
                    closing = re.match(r"^\s*([`~]+)\s*$", fence_line)
                    if (
                        closing
                        and closing.group(1)[0] == marker[0]
                        and len(closing.group(1)) >= len(marker)
                    ):
                        index += 1
                        break
                    index += 1
                blocks.append(("code", "\n".join(fence).strip()))
                continue

            if _HEADING.match(line):
                flush_text()
                blocks.append(("heading", stripped))
                index += 1
                continue

            if _IMAGE_ONLY.match(line):
                flush_text()
                blocks.append(("image", stripped))
                index += 1
                continue

            if (
                "|" in line
                and index + 1 < len(lines)
                and _TABLE_SEPARATOR.match(lines[index + 1])
            ):
                flush_text()
                table = [line, lines[index + 1]]
                index += 2
                while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                    table.append(lines[index])
                    index += 1
                blocks.append(("table", "\n".join(table).strip()))
                continue

            if _LIST_ITEM.match(line):
                flush_paragraph()
                list_lines.append(stripped)
                index += 1
                continue

            # Indented lines immediately following a list item are continuations.
            if list_lines and line[:1].isspace():
                list_lines.append(stripped)
                index += 1
                continue

            flush_list()
            paragraph.append(stripped)
            index += 1

        flush_text()
        return blocks
