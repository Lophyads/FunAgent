"""Backend-neutral records and the contract used by every memory entry point."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
import unicodedata
import uuid
from typing import Literal, Protocol, Sequence, runtime_checkable

MemoryKind = Literal["user", "note", "episode"]
KINDS = ("user", "note", "episode")


class Unset(Enum):
    VALUE = "unset"


UNSET = Unset.VALUE


class MemoryError(ValueError):
    pass


class MemoryBackendError(MemoryError):
    pass


class MemoryVersionConflict(MemoryError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryDraft:
    kind: MemoryKind
    content: str
    subject: str = ""
    source: dict = field(default_factory=dict)
    event_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    id: str
    kind: MemoryKind
    subject: str
    content: str
    version: int
    source: dict
    event_at: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class MemoryPatch:
    content: str
    subject: str | None = None
    source: dict | None = None
    event_at: str | None | Unset = UNSET


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    kind: MemoryKind | None = None
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySearch:
    query: str
    kind: MemoryKind | None = None
    limit: int = 5


@dataclass(frozen=True, slots=True)
class MemoryPage:
    entries: tuple[MemoryEntry, ...]
    total: int
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    changed: bool
    entry: MemoryEntry | None = None
    reason: Literal["duplicate", "missing"] | None = None


def validate_text(value: str, *, blank: bool = False, limit: int = 5000) -> str:
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
        raise MemoryError(f"text must be {'possibly blank' if blank else 'nonblank'} and at most {limit} characters")
    return value.strip()


def validate_draft(draft: MemoryDraft) -> MemoryDraft:
    if draft.kind not in KINDS:
        raise MemoryError("unknown memory kind")
    try:
        source = json.loads(json.dumps(draft.source, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise MemoryError("source must be JSON data") from None
    if not isinstance(source, dict):
        raise MemoryError("source must be an object")
    if draft.event_at is not None:
        try:
            datetime.fromisoformat(draft.event_at)
        except (ValueError, TypeError):
            raise MemoryError("event_at must be an ISO date or datetime") from None
    return MemoryDraft(draft.kind, validate_text(draft.content),
                       validate_text(draft.subject, blank=True, limit=200), source, draft.event_at)


def validate_query(kind, limit):
    if kind is not None and kind not in KINDS:
        raise MemoryError("unknown memory kind")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise MemoryError("limit must be an integer from 1 to 200")


def validate_version(version):
    if type(version) is not int or version < 1:
        raise MemoryError("expected_version must be a positive integer")


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def memory_key(draft: MemoryDraft) -> str:
    parts = ["core", normalized(draft.subject), normalized(draft.content)]
    if draft.kind == "episode":
        origin = {k: draft.source[k] for k in ("session_id", "turn_id", "range_start", "range_end") if k in draft.source}
        parts = ["episode", normalized(draft.subject), normalized(draft.content), draft.event_at, origin or uuid.uuid4().hex]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@runtime_checkable
class MemoryStore(Protocol):
    def add(self, draft: MemoryDraft) -> MemoryWriteResult: ...
    def list(self, query: MemoryQuery) -> MemoryPage: ...
    def search(self, query: MemorySearch) -> Sequence[MemoryEntry]: ...
    def update(self, entry_id: str, patch: MemoryPatch, *, expected_version: int) -> MemoryWriteResult: ...
    def delete(self, entry_id: str, *, expected_version: int) -> MemoryWriteResult: ...
    def close(self) -> None: ...
