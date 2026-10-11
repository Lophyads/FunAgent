"""Only the optional table-specific persistence port is exposed to the browser."""
import re
from ..frame.contracts import DomainError
from ..persistence import DbMgr
from ..persistence.protocols import DataReadStore


def Handle(method, route, query):
    if method != "GET":
        raise DomainError("METHOD_NOT_ALLOWED", "Data is read-only")
    if any(len(items) != 1 for items in query.values()):
        raise DomainError("INVALID_REQUEST", "Repeated query parameter")
    store = DbMgr.GetInstance()
    if not isinstance(store, DataReadStore):
        raise DomainError("UNSUPPORTED", "Data browsing unavailable for this store")
    if route == "/data/schema":
        if query:
            raise DomainError("INVALID_REQUEST", "Unexpected schema query")
        return 200, store.DataSchema()
    match = re.fullmatch(r"/data/tables/([a-z_]+)(/row)?", route)
    if match:
        return 200, store.ReadTable(match[1], {key: values[0] for key, values in query.items()}, detail=bool(match[2]))
    raise DomainError("NOT_FOUND", "Data route not found")
