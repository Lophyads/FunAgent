"""A startup-bound, fixed read-only query callback; no model or SDK coupling."""
from collections.abc import Mapping
from threading import Lock

from ..contracts import ControlError, DomainError
from .contracts import MemoryRecord, MemoryRef, MemorySearchResult
from .common import controlled_lock, text, validate_query


class CallbackMemoryStore:
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
        if not isinstance(rows, (list, tuple)) or len(rows) > 1000:
            raise DomainError("MEMORY_PROTOCOL", "Invalid memory evidence")
        records, seen = [], set()
        for row in rows:
            if (not isinstance(row, Mapping) or
                    any(not text(row.get(key)) for key in ("record_id", "type", "content", "citation")) or
                    row.get("source_id", self.source_id) != self.source_id or
                    row.get("kind", self.kind) != self.kind or
                    row["record_id"] in seen or
                    (row.get("version") is not None and not text(row["version"]))):
                raise DomainError("MEMORY_PROTOCOL", "Invalid memory evidence")
            seen.add(row["record_id"])
            if not query.types or row["type"] in query.types:
                records.append(MemoryRecord(MemoryRef(self.source_id, row["record_id"]), self.kind,
                                            row["type"], row["content"], row["citation"], row.get("version")))
        return MemorySearchResult(tuple(records[:query.limit]), {self.source_id: "ok"})

    def Close(self):
        with self._lock:
            self._closed = True
