"""Data shared by memory managers, stores and read-only retrieval tools."""
from dataclasses import dataclass, field
from collections.abc import Mapping
from enum import Enum

from ..frame.contracts import freeze


@dataclass(frozen=True)
class EmbeddingProfile:
    model: str
    dimensions: int
    query_prefix: str = ""
    document_prefix: str = ""


class Unchanged(Enum):
    VALUE = "unchanged"


UNSET = Unchanged.VALUE


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
    subject: str | None = None
    event_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    origin: str | None = None
    truncated: bool = False

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
    subject: str | None = None
    event_from: str | None = None
    event_to: str | None = None
    origin: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "types", tuple(self.types))


@dataclass(frozen=True)
class MemoryListQuery:
    source_id: str
    type: str | None = None
    limit: int = 50
    cursor: str | None = None
    subject: str | None = None
    event_from: str | None = None
    event_to: str | None = None
    origin: str | None = None


@dataclass(frozen=True)
class MemoryDraft:
    source_id: str
    content: str
    write_key: str
    evidence: tuple[str, ...]
    type: str = "note"
    subject: str | None = None
    event_at: str | None = None
    origin: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class MemoryPatch:
    content: str
    evidence: tuple[str, ...]
    type: str | None = None
    subject: str | None | Unchanged = UNSET
    event_at: str | None | Unchanged = UNSET
    origin: str | None | Unchanged = UNSET

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
    history: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "history", tuple(self.history))


@dataclass(frozen=True)
class RetrievalPlan:
    need_memory: bool
    need_knowledge: bool
    memory_query: str = ""
    knowledge_query: str = ""
    variant: str | None = None
    degraded: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class MemoryBranch(MemorySearchResult):
    status: str = "empty"
    queries: tuple[MemoryQuery, ...] = ()

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "queries", tuple(self.queries))


@dataclass(frozen=True)
class MemoryContext(MemoryBranch):
    facts: MemoryBranch = field(default_factory=lambda: MemoryBranch(status="skipped"))
    knowledge: MemoryBranch = field(default_factory=lambda: MemoryBranch(status="skipped"))
