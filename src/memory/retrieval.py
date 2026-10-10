"""Adapt durable facts to the existing, turn-local retrieval pipeline."""
from __future__ import annotations

import json
from itertools import zip_longest
from typing import TYPE_CHECKING, Callable

from knowledge.models import KnowledgeItem, KnowledgeRequest, KnowledgeRetrievalError

from .store import MemoryError, MemorySearch, MemoryStore

if TYPE_CHECKING:
    from agentcore.turn_state import TurnRuntime


class MemoryKnowledgeRetriever:
    def __init__(self, store: MemoryStore):
        self.store = store

    def search(self, query: str, *, request: KnowledgeRequest, top_k: int,
               runtime: TurnRuntime, check_active: Callable[[], None]) -> tuple[KnowledgeItem, ...]:
        batches = []
        try:
            for kind in ("note", "episode"):
                check_active()
                batches.append(self.store.search(MemorySearch(query, kind, top_k)))
                check_active()
        except MemoryError as exc:
            raise KnowledgeRetrievalError(type(exc).__name__) from None
        selected, seen = [], set()
        for pair in zip_longest(*batches):
            for entry in pair:
                if entry is None or entry.id in seen:
                    continue
                seen.add(entry.id)
                date = f" event_at={entry.event_at}" if entry.event_at else ""
                selected.append(KnowledgeItem(
                    entry.id, f"memory:{entry.kind}",
                    f"{entry.kind} id={entry.id} v{entry.version}{date}: {entry.content}",
                    source=json.dumps(entry.source, ensure_ascii=False, sort_keys=True),
                    title=entry.subject,
                ))
                if len(selected) == top_k:
                    return tuple(selected)
        return tuple(selected)
