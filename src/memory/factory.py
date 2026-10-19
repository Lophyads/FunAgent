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


def SourceSemantics(settings):
    """Static configuration query; never instantiate a driver or perform IO."""
    from .sqlite import SQLiteMemoryStore
    from .supabase import SupabaseMemoryStore
    from .cognee import CogneeMemoryStore
    from .callback import CallbackMemoryStore
    driver = {"sqlite": SQLiteMemoryStore, "supabase": SupabaseMemoryStore,
              "cognee": CogneeMemoryStore, "callback": CallbackMemoryStore}.get(settings["store"])
    return driver.supported_types if driver else None


def ValidatePolicies(sources, policies):
    """Reject semantic disconnections before constructing clients or loading credentials."""
    for purpose, required_type in (("profile", "profile"), ("review", None)):
        policy = policies.get(purpose, {})
        if not policy.get("enabled", False):
            continue
        for name in policy.get("sources", ()):
            types = SourceSemantics(sources[name])
            if required_type is not None and types is not None and required_type not in types:
                raise DomainError("CONFIG_REFERENCE", f"Memory source {name} cannot serve {purpose}")
            if purpose == "review":
                source = sources[name]
                if source.get("kind", "episodic") != "episodic" or not {"search", "list", "write"} <= set(source.get("capabilities", ("search",))):
                    raise DomainError("CONFIG_REFERENCE", f"Review source {name} requires episodic search/list/write")
                if source["store"] == "cognee" and source.get("write_confirmation", "indexed") != "stored":
                    raise DomainError("CONFIG_REFERENCE", f"Cognee review source {name} requires write_confirmation=stored")
    review, facts = policies.get("review", {}), policies.get("facts", {})
    if review.get("enabled", False) and (not facts.get("enabled", False) or
            not set(review.get("sources", ())) <= set(facts.get("sources", ()))):
        raise DomainError("CONFIG_REFERENCE", "Review write sources must be enabled facts sources")


def CreateInspection(conf, own):
    from .inspection import MemoryInspection
    from .cognee_inspection import CogneeInspection
    from .cognee_datasets import CogneeDataset
    sources = conf.Memory().Sources()
    adapters = {name: own(CogneeInspection(settings, name)) for name, settings in sources.items()
                if settings.get('store') == 'cognee' and all(CogneeDataset.Find(n) for n in settings['datasets'])}
    return own(MemoryInspection(sources, adapters))


def RequiredBackends(conf):
    return tuple({s["backend_id"] for s in conf.Memory().Sources().values()
                  if s["store"] == "callback" and s["callback_id"] == "mcp"})


def CreateTopics(conf, conf_dir):
    from .topic_catalog import JsonTopicCatalog
    path = Path(conf.Memory().Topics().get("path", ".funagent/memory-topics.json"))
    return JsonTopicCatalog(path if path.is_absolute() else Path(conf_dir) / path)


def Create(conf, conf_dir, own, *, backends=None, topics=None):
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
    return MemoryManager(stores, topics=topics if topics is not None else CreateTopics(conf, conf_dir),
                         owns_stores=False, conf=conf.Memory())
