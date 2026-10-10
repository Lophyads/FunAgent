"""Small validation and controlled connection locking shared by two stores."""
import unicodedata

from ..contracts import DomainError, controlled_lock


def text(value):
    return isinstance(value, str) and bool(value.strip())


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def validate_query(query):
    if (type(query.include_deleted) is not bool or not isinstance(query.text, str) or len(query.text) > 8000 or
            type(query.limit) is not int or not 1 <= query.limit <= 100 or
            query.kind not in {None, "semantic", "episodic"} or
            any(not text(v) for v in (*query.source_ids, *query.types))):
        raise DomainError("INVALID_REQUEST", "Invalid memory query")


def validate_content(content, evidence, type):
    if not text(content) or len(content) > 65536 or not text(type) or not evidence or any(not text(e) for e in evidence):
        raise DomainError("INVALID_REQUEST", "Memory content requires trusted evidence")
