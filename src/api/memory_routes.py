"""Management routes parse values; the shared memory use case owns authority."""
import re
from ..frame.contracts import DomainError
from ..memory.memory_operations_mgr import MemoryOperationsMgr
from ..memory.contracts import MemoryQuery, MemoryListQuery, MemoryRef, MemoryPatch, MemoryDraft
from .request_validation import Fields, Text

FILTERS = {"type", "subject", "origin", "event_from", "event_to"}


def _query(query, *, search=False):
    allowed = {"source_id", "limit", *FILTERS} | ({"text", "kind"} if search else {"cursor", "topic_id", "unclassified"})
    if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
        raise DomainError("INVALID_REQUEST", "Invalid memory query")
    values = {key: Text(items[0], 8192 if key in {"text", "cursor"} else 1024) for key, items in query.items()}
    try:
        limit = int(values.pop("limit", "10" if search else "50"))
    except ValueError:
        raise DomainError("INVALID_REQUEST", "Invalid memory limit") from None
    if not 1 <= limit <= 100:
        raise DomainError("INVALID_REQUEST", "Invalid memory limit")
    source = values.pop("source_id", None)
    if source is None:
        raise DomainError("INVALID_REQUEST", "Memory query requires a source")
    if search:
        if "text" not in values:
            raise DomainError("INVALID_REQUEST", "Search requires text")
        types = (values.pop("type"),) if "type" in values else ()
        return MemoryQuery(source_ids=(source,) if source else (), types=types, limit=limit, **values)
    topic_id = values.pop("topic_id", None)
    unclassified = values.pop("unclassified", None)
    if (unclassified is not None and unclassified != "1") or (topic_id is not None and unclassified is not None):
        raise DomainError("INVALID_REQUEST", "Invalid directory filters")
    return MemoryListQuery(source, limit=limit, **values), topic_id, unclassified == "1"


def _revision(body):
    value = body["expected_revision"]
    if type(value) is not int or value < 0:
        raise DomainError("INVALID_REQUEST", "Expected revision must be a nonnegative integer")
    return value


def _parent(value):
    return None if value is None else Text(value, 128)


def Handle(method, route, query, body, *, settings=None):
    if route == "/memory/config":
        if query:
            raise DomainError("INVALID_REQUEST", "Unexpected query")
        if settings is None:
            raise DomainError("NOT_READY", "Memory settings are not initialized")
        if method == "GET":
            return 200, settings.Read()
        if method == "POST":
            Fields(body, {"content"}, {"content"})
            return 200, settings.Apply(Text(body["content"], 65536))
        raise DomainError("METHOD_NOT_ALLOWED", "Invalid memory config method")
    if route == '/memory/imports' or route.startswith('/memory/imports/'):
        from ..memory.memory_import_mgr import MemoryImportMgr
        service = MemoryImportMgr.GetInstance()
        if route == '/memory/imports' and method == 'POST':
            if query:
                raise DomainError('INVALID_REQUEST', 'Unexpected query')
            Fields(body, {'source_id', 'file'}, {'source_id', 'file'})
            if not isinstance(body['file'], bytes):
                raise DomainError('INVALID_REQUEST', 'Expected uploaded file')
            return 202, service.Submit(Text(body['source_id'], 128), body['file'])
        if route == '/memory/imports' and method == 'GET':
            if set(query) != {'source_id'} or len(query['source_id']) != 1:
                raise DomainError('INVALID_REQUEST', 'Expected one source')
            return 200, service.Latest(Text(query['source_id'][0], 128))
        if re.fullmatch(r'/memory/imports/[A-Za-z0-9_-]{1,128}', route) and method == 'GET' and not query:
            return 200, service.Get(route.rsplit('/', 1)[1])
        raise DomainError('METHOD_NOT_ALLOWED', 'Invalid import route')
    if route.startswith('/memory/cognee/'):
        from .memory_cognee_routes import Handle as Inspect
        return Inspect(method, route, query, body)
    operations = MemoryOperationsMgr.GetInstance()
    if route == "/memory/topics" and method == "GET":
        if set(query) != {"source_id"} or len(query["source_id"]) != 1:
            raise DomainError("INVALID_REQUEST", "Topic query requires one source")
        return 200, operations.Topics(Text(query["source_id"][0], 128))
    if route in {"/memory/search", "/memory/records"} and method == "GET":
        value = _query(query, search=route.endswith("search"))
        if route.endswith("search"):
            return 200, operations.Search(value)
        listing, topic_id, unclassified = value
        return 200, operations.List(listing, topic_id=topic_id, unclassified=unclassified)
    if query:
        raise DomainError("INVALID_REQUEST", "Unexpected query")
    if route == "/memory/sources" and method == "GET":
        return 200, {"sources": operations.Sources(), "default_source_id": operations.DefaultSource()}
    if route == "/memory/topics" and method == "POST":
        Fields(body, {"source_id", "parent_id", "name", "description"}, {"source_id", "parent_id", "name", "description"})
        return 201, operations.CreateTopic(Text(body["source_id"], 128), _parent(body["parent_id"]),
                                           Text(body["name"], 64), Text(body["description"], 240))
    topic_match = re.fullmatch(r"/memory/topics/([A-Za-z0-9_-]{1,128})", route)
    if topic_match and method in {"PATCH", "DELETE"}:
        allowed = {"source_id", "expected_revision"} | ({"name", "description", "parent_id"} if method == "PATCH" else set())
        Fields(body, allowed, {"source_id", "expected_revision"})
        source = Text(body["source_id"], 128)
        revision = _revision(body)
        if method == "DELETE":
            return 200, operations.DeleteTopic(source, topic_match[1], revision)
        changes = {key: body[key] for key in ("name", "description", "parent_id") if key in body}
        if not changes:
            raise DomainError("INVALID_REQUEST", "Topic update requires at least one field")
        for key, maximum in (("name", 64), ("description", 240)):
            if key in changes:
                changes[key] = Text(changes[key], maximum)
        if "parent_id" in changes:
            changes["parent_id"] = _parent(changes["parent_id"])
        return 200, operations.UpdateTopic(source, topic_match[1], revision, **changes)
    assignment_match = re.fullmatch(r"/memory/records/([A-Za-z0-9_.:-]{1,128})/topic", route)
    if assignment_match and method == "PATCH":
        Fields(body, {"source_id", "topic_id", "expected_revision"}, {"source_id", "topic_id", "expected_revision"})
        return 200, operations.AssignTopic(MemoryRef(Text(body["source_id"], 128), assignment_match[1]),
                                            Text(body["topic_id"], 128), _revision(body))
    if route == '/memory/records' and method == 'POST':
        Fields(body, {'source_id', 'content', 'type', 'subject', 'event_at', 'origin', 'request_key', 'topic_id'}, {'source_id', 'content', 'type', 'topic_id'})
        draft = MemoryDraft(source_id=Text(body['source_id'], 128), content=Text(body['content'], 65536),
                            type=Text(body['type'], 128), write_key='', evidence=(),
                            **{key: body[key] for key in ('subject', 'event_at', 'origin') if key in body})
        return 201, operations.Write(draft, body.get('request_key'), topic_id=Text(body['topic_id'], 128))
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
    if route in {"/memory/search", "/memory/records", "/memory/sources", "/memory/changes", "/memory/topics"} or match or topic_match or assignment_match:
        raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
    raise DomainError("NOT_FOUND", "Route not found")
