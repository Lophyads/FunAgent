"""Data shared by memory managers, stores and read-only retrieval tools."""
from dataclasses import dataclass, field
from collections.abc import Mapping

from ..contracts import freeze


@dataclass(frozen=True)
class MemoryRef:
    source_id: str
    record_id: str


@dataclass(frozen=True)
class MemoryRecord:
    ref: MemoryRef
    kind: str
    type: str
    content: str
    citation: str
    version: str | None = None
    evidence: tuple[str, ...] = ()
    deleted: bool = False

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class MemoryQuery:
    text: str = ""
    source_ids: tuple[str, ...] = ()
    kind: str | None = None
    types: tuple[str, ...] = ()
    limit: int = 10
    include_deleted: bool = False

    def __post_init__(self):
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "types", tuple(self.types))


@dataclass(frozen=True)
class MemoryListQuery:
    source_id: str
    type: str | None = None
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True)
class MemoryDraft:
    source_id: str
    content: str
    write_key: str
    evidence: tuple[str, ...]
    type: str = "fact"

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class MemoryPatch:
    content: str
    evidence: tuple[str, ...]
    type: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class MemorySearchResult:
    records: tuple[MemoryRecord, ...] = ()
    source_status: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "source_status", freeze(self.source_status))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class MemoryPrepareRequest:
    text: str


@dataclass(frozen=True)
class MemoryContext(MemorySearchResult):
    status: str = "empty"
    queries: tuple[MemoryQuery, ...] = ()
