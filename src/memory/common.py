"""Small validation and controlled connection locking shared by two stores."""
import unicodedata
from datetime import datetime, timezone

from ..frame.contracts import DomainError, controlled_lock


def text(value):
    return isinstance(value, str) and bool(value.strip())


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def NormalizeTopicName(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def validate_query(query):
    if (type(query.include_deleted) is not bool or not isinstance(query.text, str) or len(query.text) > 8000 or
            type(query.limit) is not int or not 1 <= query.limit <= 100 or
            query.kind not in {None, "semantic", "episodic"} or
            any(not text(v) for v in (*query.source_ids, *query.types))):
        raise DomainError("INVALID_REQUEST", "Invalid memory query")
    validate_filters(query)


def timestamp(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, AttributeError):
        raise DomainError("INVALID_REQUEST", "Memory time requires an ISO timestamp with timezone") from None


def validate_metadata(subject, event_at, origin):
    if any(value is not None and (not text(value) or len(value) > 512) for value in (subject, origin)):
        raise DomainError("INVALID_REQUEST", "Invalid memory metadata")
    return subject, timestamp(event_at), origin


def validate_filters(query):
    validate_metadata(query.subject, None, query.origin)
    start, end = timestamp(query.event_from), timestamp(query.event_to)
    if start is not None and end is not None and start > end:
        raise DomainError("INVALID_REQUEST", "Invalid memory time range")


def filter_clauses(query):
    validate_filters(query)
    clauses, parameters = [], []
    for field in ("subject", "origin"):
        if getattr(query, field) is not None:
            clauses.append(field + "=?")
            parameters.append(getattr(query, field))
    for field, operator in (("event_from", ">="), ("event_to", "<=")):
        if getattr(query, field) is not None:
            clauses.append("event_at" + operator + "?")
            parameters.append(timestamp(getattr(query, field)))
    return clauses, parameters


def validate_content(content, evidence, type):
    if not text(content) or len(content) > 65536 or not text(type) or not evidence or any(not text(e) for e in evidence):
        raise DomainError("INVALID_REQUEST", "Memory content requires trusted evidence")
