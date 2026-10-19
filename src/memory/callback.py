"""A startup-bound, fixed read-only query callback; no model or SDK coupling."""
from collections.abc import Mapping
from threading import Lock

from ..frame.contracts import ControlError, DomainError
from .contracts import MemoryQueryBatch, MemoryRecord, MemoryRef, MemorySearchResult
from .failure import BindIssue, IssueWarnings
from .common import controlled_lock, text, validate_query, validate_metadata, timestamp


class CallbackMemoryStore:
    supported_types = frozenset({"knowledge"})
    capabilities = frozenset({"search"})

    def __init__(self, source_id, query_callback, kind="semantic"):
        if not text(source_id) or kind != "semantic" or not callable(query_callback):
            raise DomainError("CONFIG_INVALID", "Callback memory requires a semantic source and query")
        self.source_id, self.kind = source_id, kind
        self._query = query_callback
        self._lock, self._closed = Lock(), False

    def Search(self, query, context):
        context.CheckCancelled()
        validate_query(query)
        if (query.source_ids and self.source_id not in query.source_ids) or (query.kind and query.kind != self.kind):
            return MemorySearchResult()
        with controlled_lock(self._lock, context):
            if self._closed:
                raise DomainError("NOT_READY", "Memory store is closed")
            try:
                rows = self._query(query, context)
                context.CheckCancelled()
            except (ControlError, DomainError):
                raise
            except Exception:
                context.CheckCancelled()
                raise DomainError("MEMORY_SOURCE", "Memory query failed") from None
        if isinstance(rows, (list, tuple)):
            rows = MemoryQueryBatch(rows=rows)
        if not isinstance(rows, MemoryQueryBatch):
            raise DomainError("MEMORY_PROTOCOL", "Invalid memory evidence")
        batch = rows
        issues = tuple(BindIssue(issue, self.source_id, read=True) for issue in batch.issues)
        records, seen = [], set()
        for row in batch.rows:
            if (not isinstance(row, Mapping) or
                    any(not text(row.get(key)) for key in ("record_id", "type", "content", "citation")) or
                    row.get("source_id", self.source_id) != self.source_id or
                    row.get("kind", self.kind) != self.kind or
                    row["record_id"] in seen or
                    (row.get("version") is not None and not text(row["version"]))):
                raise DomainError("MEMORY_PROTOCOL", "Invalid memory evidence")
            seen.add(row["record_id"])
            try:
                subject, event_at, origin = validate_metadata(row.get("subject"), row.get("event_at"), row.get("origin"))
                created_at, updated_at = timestamp(row.get("created_at")), timestamp(row.get("updated_at"))
            except DomainError:
                raise DomainError("MEMORY_PROTOCOL", "Invalid memory metadata") from None
            if ((not query.types or row["type"] in query.types) and
                    (query.subject is None or subject == query.subject) and
                    (query.origin is None or origin == query.origin) and
                    (query.event_from is None or event_at is not None and event_at >= timestamp(query.event_from)) and
                    (query.event_to is None or event_at is not None and event_at <= timestamp(query.event_to))):
                records.append(MemoryRecord(MemoryRef(self.source_id, row["record_id"]), self.kind,
                                            row["type"], row["content"], row["citation"], row.get("version"),
                                            subject=subject, event_at=event_at, created_at=created_at, updated_at=updated_at, origin=origin))
        return MemorySearchResult(tuple(records[:query.limit]),
            {self.source_id: "partial" if issues else batch.status}, IssueWarnings(issues), issues=issues)

    def Close(self):
        with self._lock:
            self._closed = True
