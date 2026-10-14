"""Construct memory sources within their domain; borrowers do not close stores."""
from pathlib import Path

from ..frame.contracts import DomainError


def _sqlite(source_id, settings, conf_dir):
    from .sqlite import SQLiteMemoryStore
    path = Path(settings["path"])
    return SQLiteMemoryStore(path if path.is_absolute() else conf_dir / path, source_id, settings["kind"])


def _http(settings):
    from .http_query import HTTPMemoryQuery
    return HTTPMemoryQuery(settings)


QUERIES = {"http_json": _http}


def _callback(source_id, settings, conf_dir):
    from .callback import CallbackMemoryStore
    query = QUERIES.get(settings["callback_id"])
    if query is None:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected memory query callback is unavailable")
    return CallbackMemoryStore(source_id, query(settings), settings["kind"])


STORES = {"sqlite": _sqlite, "callback": _callback}


def _embedding_http(settings):
    from .embedding import HTTPEmbedder
    return HTTPEmbedder(settings)


EMBEDDERS = {"http": _embedding_http}


def _supabase(source_id, settings, conf_dir):
    from .supabase import SupabaseMemoryStore
    selected = settings["embedding"]
    factory = EMBEDDERS.get(selected["kind"])
    if factory is None:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected embedding driver is unavailable")
    embedder = factory(selected)
    try:
        return SupabaseMemoryStore(settings, source_id, embedder, owns_embedder=True)
    except BaseException:
        embedder.Close()
        raise


STORES["supabase"] = _supabase


def _cognee(source_id, settings, conf_dir):
    from .cognee import CogneeMemoryStore
    return CogneeMemoryStore(settings, source_id)


STORES["cognee"] = _cognee


def RequiredBackends(conf):
    return tuple({s["backend_id"] for s in conf.Memory().Sources().values()
                  if s["store"] == "callback" and s["callback_id"] == "mcp"})


def Create(conf, conf_dir, own, *, backends=None):
    from .memory_manager import MemoryManager
    stores = {}
    for name, settings in conf.Memory().Sources().items():
        if settings["store"] == "callback" and settings["callback_id"] == "mcp":
            from .mcp_query import MCPMemoryQuery
            from .callback import CallbackMemoryStore
            backend = (backends or {}).get(settings["backend_id"])
            if backend is None:
                raise DomainError("CONFIG_REFERENCE", "Knowledge MCP backend is unavailable")
            stores[name] = own(CallbackMemoryStore(name, MCPMemoryQuery(settings, backend), settings["kind"]))
            continue
        factory = STORES.get(settings["store"])
        if factory is None:
            raise DomainError("DRIVER_UNAVAILABLE", "Selected memory store is unavailable")
        stores[name] = own(factory(name, settings, conf_dir))
    return MemoryManager(stores, owns_stores=False)
