"""Data shared by memory managers, stores and read-only retrieval tools."""
from dataclasses import dataclass, field
from collections.abc import Mapping
from enum import Enum

from ..frame.contracts import DomainError, freeze

TOPIC_SYNC_MESSAGE = ("正文已经变化，但主题目录同步失败。请刷新列表核对，请勿自动重复提交。"
                      "新增正文可从未归类中手工归类。")


class TopicConflict(DomainError):
    """A controlled topic-domain message that management may show verbatim."""
    def __init__(self, message):
        super().__init__("CONFLICT", message)


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


@dataclass(frozen=True, kw_only=True)
class MemoryIssue:
    code: str
    stage: str
    source_id: str | None = None
    http_status: int | None = None
    retryable: bool = False
    effect: str = "none"
    expected_dimensions: int | None = None
    actual_dimensions: int | None = None

    def __post_init__(self):
        from .failure import ValidateIssue
        ValidateIssue(self)


class MemoryFailure(DomainError):
    def __init__(self, issue: MemoryIssue, *, ref: MemoryRef | None = None,
                 version: str | None = None, related: tuple[MemoryIssue, ...] = ()):
        from .failure import FailureMessage, ValidateFailure
        self.issue, self.ref, self.version, self.related = issue, ref, version, tuple(related)
        ValidateFailure(self)
        super().__init__(issue.code, FailureMessage(issue))


@dataclass(frozen=True, kw_only=True)
class MemoryQueryBatch:
    rows: tuple[Mapping, ...]
    status: str = "ok"
    issues: tuple[MemoryIssue, ...] = ()

    def __post_init__(self):
        from .failure import ValidateIssue
        if (not isinstance(self.status, str) or self.status not in {"ok", "partial"} or
                not isinstance(self.rows, (list, tuple)) or len(self.rows) > 1000 or
                any(not isinstance(row, Mapping) for row in self.rows) or
                not isinstance(self.issues, (list, tuple)) or len(self.issues) > 16):
            raise DomainError("MEMORY_PROTOCOL", "Invalid memory batch status")
        for issue in self.issues:
            ValidateIssue(issue)
        object.__setattr__(self, "rows", tuple(freeze(row) for row in self.rows))
        object.__setattr__(self, "issues", tuple(self.issues))


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
    index_status: str | None = None

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
    issues: tuple[MemoryIssue, ...] = field(default=(), kw_only=True)

    def __post_init__(self):
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "source_status", freeze(self.source_status))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True)
class MemoryPrepareRequest:
    text: str


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


@dataclass(frozen=True)
class Topic:
    topic_id: str
    parent_id: str | None
    name: str
    description: str


@dataclass(frozen=True)
class TopicSnapshot:
    source_id: str
    revision: int = 0
    topics: Mapping[str, Topic] = field(default_factory=dict)
    assignments: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "topics", freeze(self.topics))
        object.__setattr__(self, "assignments", freeze(self.assignments))


@dataclass(frozen=True)
class TopicSelection:
    topic_id: str | None = None
    parent_id: str | None = None
    name: str | None = None
    description: str | None = None

    def __post_init__(self):
        existing = isinstance(self.topic_id, str) and bool(self.topic_id.strip())
        new = self.topic_id is None and self.name is not None and self.description is not None
        if not ((existing and self.parent_id is None and self.name is None and self.description is None) or new):
            raise DomainError("INVALID_REQUEST", "Select an existing topic or define one new topic")

    @classmethod
    def FromData(cls, value):
        if not isinstance(value, Mapping) or set(value) not in ({"topic_id"}, {"parent_id", "name", "description"}):
            raise DomainError("INVALID_REQUEST", "Select an existing topic or define one new topic")
        return cls(**value)


@dataclass(frozen=True)
class TopicChange:
    topic: Topic
    revision: int


@dataclass(frozen=True)
class MemoryWriteReceipt:
    """Application receipt binds body data to the exact classification commit."""
    record: MemoryRecord
    topic_snapshot: TopicSnapshot
