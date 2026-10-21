"""Bounded directory browsing over the unchanged physical List protocol."""
import base64
from dataclasses import replace
import json

from ..frame.contracts import DomainError

PAGE_SIZE = 100
SCAN_LIMIT = 500


def _encode(scope, entry, offset):
    raw = json.dumps({"version": 1, "scope": scope, "entry": entry, "offset": offset}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def ReadDirectory(reader, snapshot, query, context, *, topic_id=None, unclassified=False):
    scope = {key: getattr(query, key) for key in ("source_id", "type", "subject", "event_from", "event_to", "origin")}
    scope.update(topic_id=topic_id, unclassified=unclassified)
    entry, offset = None, 0
    if query.cursor is not None:
        try:
            if not isinstance(query.cursor, str) or len(query.cursor) > 8192:
                raise ValueError()
            value = json.loads(base64.b64decode(query.cursor, altchars=b"-_", validate=True))
            if (not isinstance(value, dict) or set(value) != {"version", "scope", "entry", "offset"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or value["scope"] != scope or type(value["offset"]) is not int or not 0 <= value["offset"] < PAGE_SIZE
                    or (value["entry"] is not None and not isinstance(value["entry"], str))):
                raise ValueError()
            entry, offset = value["entry"], value["offset"]
        except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
            raise DomainError("INVALID_REQUEST", "Directory cursor does not match this query") from None
    records, scanned, visited = [], 0, set()
    while scanned < SCAN_LIMIT:
        if context.Remaining() <= 0:
            break
        context.CheckCancelled()
        page = reader.List(replace(query, limit=PAGE_SIZE, cursor=entry), context)
        if offset > len(page.items):
            raise DomainError("INVALID_REQUEST", "Directory cursor offset is invalid; refresh the list")
        for index in range(offset, len(page.items)):
            record = page.items[index]
            scanned += 1
            assigned = snapshot.assignments.get(record.ref.record_id)
            if ((topic_id is None or assigned == topic_id)
                    and (not unclassified or assigned is None)):
                records.append(record)
            offset = index + 1
            if len(records) >= query.limit or scanned >= SCAN_LIMIT or context.Remaining() <= 0:
                if offset < len(page.items):
                    return records, _encode(scope, entry, offset)
                return records, _encode(scope, page.next_cursor, 0) if page.next_cursor is not None else None
        entry, offset = page.next_cursor, 0
        if entry is None:
            return records, None
        if entry in visited:
            raise DomainError("MEMORY_STORAGE", "Memory store returned a repeated list cursor")
        visited.add(entry)
        # Empty pages also consume scanning budget, bounding a broken/sparse backend.
        if not page.items:
            scanned += 1
    return records, _encode(scope, entry, offset)
