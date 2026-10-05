"""Backend-neutral records and the contract used by every memory entry point."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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


@runtime_checkable
class MemoryStore(Protocol):
    def add(self, draft: MemoryDraft) -> MemoryWriteResult: ...
    def list(self, query: MemoryQuery) -> MemoryPage: ...
    def search(self, query: MemorySearch) -> Sequence[MemoryEntry]: ...
    def update(self, entry_id: str, patch: MemoryPatch, *, expected_version: int) -> MemoryWriteResult: ...
    def delete(self, entry_id: str, *, expected_version: int) -> MemoryWriteResult: ...
    def close(self) -> None: ...
