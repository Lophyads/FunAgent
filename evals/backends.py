"""Thin wrappers over existing stores; isolated namespaces and explicit lineage."""
import importlib.util
import os
from dataclasses import asdict
from typing import Protocol

from src.frame.contracts import DomainError
from src.memory.contracts import MemoryDraft, MemoryListQuery, MemoryQuery
from .dataset import EvalError


class MemoryBackend(Protocol):
    def Ingest(self, items, context): ...
    def WaitReady(self, context): ...
    def Search(self, text, limit, context): ...
    def Close(self): ...


def Preflight(config):
    for variant in config["variants"]:
        if variant["backend"] == "cognee":
            from src.memory.cognee import Headers
            name = variant["settings"].get("credential_env")
            if name and not os.environ.get(name):
                raise EvalError("Missing credential environment variable: " + name)
            Headers(variant["settings"])
            if importlib.util.find_spec("httpx") is None:
                raise EvalError("Cognee requires the cognee extra")
            continue
        if variant["backend"] != "supabase":
            continue
        settings = variant["settings"]
        names = [settings.get("credential_env", "EVAL_SUPABASE_TOKEN"),
                 settings.get("api_key_env", settings.get("credential_env", "EVAL_SUPABASE_TOKEN")),
                 settings["embedding"].get("credential_env", "EMBEDDING_API_KEY")]
        for name in names:
            if not os.environ.get(name):
                raise EvalError("Missing credential environment variable: " + name)
        if importlib.util.find_spec("httpx") is None:
            raise EvalError("Supabase requires the existing supabase extra")


class StoreBackend:
    def __init__(self, store, space):
        self.store, self.space = store, space
        self.receipts, self._mapping = [], {}

    def Ingest(self, items, context):
        if self.store.List(MemoryListQuery("eval", limit=1), context).items:
            raise DomainError("EVAL_NOT_EMPTY", "Evaluation space is not empty")
        for item in items:
            reference = "eval:" + self.space + "/" + item["id"]
            record = self.store.Write(MemoryDraft("eval", item["text"], reference, (reference,),
                                      event_at=item.get("event_at"), origin=reference), context)
            self._mapping[record.ref.record_id] = item["id"]
            self.receipts.append({"item_id": item["id"], "record": asdict(record)})
        return self.receipts

    def WaitReady(self, context):
        context.CheckCancelled()

    def Search(self, text, limit, context):
        result = self.store.Search(MemoryQuery(text=text, source_ids=("eval",), limit=limit), context)
        evidence = []
        for record in result.records:
            item = self._mapping.get(record.ref.record_id)
            evidence.append({"record_id": record.ref.record_id, "item_ids": [item] if item else [],
                             "text": record.content, "citation": record.citation,
                             "event_at": record.event_at, "unmapped": item is None, "truncated": record.truncated})
        return {"evidence": evidence, "raw": [asdict(r) for r in result.records], "warnings": list(result.warnings)}

    def Close(self):
        self.store.Close()


def _sqlite(settings, space, directory):
    from src.memory.sqlite import SQLiteMemoryStore
    return StoreBackend(SQLiteMemoryStore(directory / (space + ".sqlite3"), "eval"), space)


def _supabase(settings, space, directory):
    from src.memory.embedding import HTTPEmbedder
    from src.memory.supabase import SupabaseMemoryStore
    embedder = HTTPEmbedder(settings["embedding"])
    try:
        store = SupabaseMemoryStore(settings | {"namespace": space, "credential_env": settings.get("credential_env", "EVAL_SUPABASE_TOKEN")},
                                    "eval", embedder, owns_embedder=True)
    except BaseException:
        embedder.Close()
        raise
    return StoreBackend(store, space)


def _cognee(settings, space, directory):
    from .cognee import CogneeBackend
    return CogneeBackend(settings, space)


BACKENDS = {"sqlite": _sqlite, "supabase": _supabase, "cognee": _cognee}


def Create(name, settings, space, directory):
    return BACKENDS[name](settings, space, directory)
