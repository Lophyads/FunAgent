"""Immutable knowledge inputs and results, independent of the conversation loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


class KnowledgePlanningError(RuntimeError):
    pass


class KnowledgeRetrievalError(RuntimeError):
    pass


class KnowledgePhaseTimeout(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeRequest:
    question: str
    history: tuple[tuple[str, str], ...] = ()
    session_id: str = ""
    turn_id: str = ""
    permissions: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    memory_enabled: bool = False
    knowledge_enabled: bool = True


class KnowledgePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    need_knowledge: StrictBool
    query: StrictStr = Field(max_length=512)
    query_variants: tuple[StrictStr, ...] = Field(max_length=1)
    reason: StrictStr = Field(max_length=240)
    need_memory: StrictBool = False
    memory_query: StrictStr = Field(default="", max_length=512)

    @field_validator("query", "reason", "memory_query")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("query_variants")
    @classmethod
    def _validate_variants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not query.strip() or len(query) > 512 for query in value):
            raise ValueError("query variants must contain nonblank text of at most 512 characters")
        return tuple(query.strip() for query in value)

    @model_validator(mode="after")
    def _consistent_decision(self):
        if self.need_knowledge and not self.query:
            raise ValueError("retrieval requires a query")
        if not self.need_knowledge and (self.query or self.query_variants):
            raise ValueError("skip requires an empty query and no variants")
        if self.need_memory and not self.memory_query:
            raise ValueError("memory retrieval requires a query")
        if not self.need_memory and self.memory_query:
            raise ValueError("skipping memory requires an empty query")
        return self


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    id: str
    collection: str
    content: str
    source: str = ""
    title: str = ""
    page: int | str | None = None
    snippet_only: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    status: Literal["skipped", "retrieved", "empty", "failed"] = "skipped"
    plan: KnowledgePlan | None = None
    queries: tuple[str, ...] = ()
    items: tuple[KnowledgeItem, ...] = ()
    warnings: tuple[str, ...] = ()
    memory_status: Literal["skipped", "retrieved", "empty", "failed"] = "skipped"
    memory_items: tuple[KnowledgeItem, ...] = ()
