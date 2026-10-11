"""Management routes parse values; the shared memory use case owns authority."""
import re
from ..frame.contracts import DomainError
from ..memory.memory_operations_mgr import MemoryOperationsMgr
from ..memory.contracts import MemoryQuery, MemoryListQuery, MemoryRef, MemoryPatch, MemoryDraft
from .request_validation import Fields, Text

FILTERS = {"type", "subject", "origin", "event_from", "event_to"}


def _query(query, *, search=False):
    allowed = {"source_id", "limit", *FILTERS} | ({"text", "kind"} if search else {"cursor"})
    if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
        raise DomainError("INVALID_REQUEST", "Invalid memory query")
    values = {key: Text(items[0], 8000 if key == "text" else 1024) for key, items in query.items()}
    try:
        limit = int(values.pop("limit", "10" if search else "50"))
    except ValueError:
        raise DomainError("INVALID_REQUEST", "Invalid memory limit") from None
    if not 1 <= limit <= 100:
        raise DomainError("INVALID_REQUEST", "Invalid memory limit")
    source = values.pop("source_id", None)
    if search:
        types = (values.pop("type"),) if "type" in values else ()
        return MemoryQuery(source_ids=(source,) if source else (), types=types, limit=limit, **values)
    if source is None:
        raise DomainError("INVALID_REQUEST", "List requires a source")
    return MemoryListQuery(source, limit=limit, **values)


def Handle(method, route, query, body):
    operations = MemoryOperationsMgr.GetInstance()
    if route in {"/memory/search", "/memory/records"} and method == "GET":
        value = _query(query, search=route.endswith("search"))
        return 200, operations.Search(value) if route.endswith("search") else operations.List(value)
    if query:
        raise DomainError("INVALID_REQUEST", "Unexpected query")
    if route == "/memory/sources" and method == "GET":
        return 200, {"sources": operations.Sources(), "default_source_id": operations.DefaultSource()}
    if route == '/memory/records' and method == 'POST':
        Fields(body, {'source_id', 'content', 'type', 'subject', 'event_at', 'origin', 'request_key'}, {'source_id', 'content', 'type'})
        draft = MemoryDraft(source_id=Text(body['source_id'], 128), content=Text(body['content'], 65536),
                            type=Text(body['type'], 128), write_key='', evidence=(),
                            **{key: body[key] for key in ('subject', 'event_at', 'origin') if key in body})
        return 201, operations.Write(draft, body.get('request_key'))
    if route == "/memory/changes" and method == "POST":
        Fields(body, {"action", "source_id", "record_id", "expected_version", "patch"},
               {"action", "source_id", "record_id", "expected_version"})
        patch = None
        if "patch" in body:
            Fields(body["patch"], {"content", "type", "subject", "origin", "event_at"}, {"content"})
            patch = MemoryPatch(evidence=(), **body["patch"])
        return 202, operations.Request(Text(body["action"], 16), MemoryRef(Text(body["source_id"], 128), Text(body["record_id"], 128)),
                                       Text(body["expected_version"], 128), patch)
    match = re.fullmatch(r"/memory/challenges/([A-Za-z0-9_-]{1,128})/confirm", route)
    if match and method == "POST":
        Fields(body, {"arguments_digest", "approve"}, {"arguments_digest", "approve"})
        return 200, operations.Confirm(match[1], body["arguments_digest"], body["approve"])
    if route in {"/memory/search", "/memory/records", "/memory/sources", "/memory/changes"} or match:
        raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
    raise DomainError("NOT_FOUND", "Route not found")
