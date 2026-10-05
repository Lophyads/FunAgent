"""One typed memory tool, shared dispatch for HTTP and model callers."""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from agentcore.interfaces import ToolContext
from tools.registry import ToolEntry, ToolExecutionPolicy
from .store import UNSET, MemoryDraft, MemoryKind, MemoryPatch, MemoryQuery, MemorySearch, MemoryStore


class MemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "search", "add", "replace", "remove"]
    kind: MemoryKind | None = None
    content: str = Field(default="", max_length=5000)
    subject: str = Field(default="", max_length=200)
    query: str = Field(default="", max_length=1000)
    id: str = Field(default="", max_length=500)
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    event_at: str | None = Field(default=None, max_length=100)
    cursor: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=20, ge=1, le=200, strict=True)

    @model_validator(mode="after")
    def required_fields(self):
        if self.action == "add" and self.kind is None:
            raise ValueError("add requires kind")
        if self.action in {"add", "replace"} and not self.content.strip():
            raise ValueError("content must be nonblank")
        if self.action in {"replace", "remove"} and (not self.id.strip() or self.expected_version is None):
            raise ValueError("replace/remove require id and expected_version")
        return self


class ReviewMemoryArgs(MemoryArgs):
    action: Literal["list", "search", "add"]


def execute_memory(store: MemoryStore, args: MemoryArgs, source: dict) -> dict:
    if args.action == "list":
        return asdict(store.list(MemoryQuery(args.kind, args.limit, args.cursor)))
    if args.action == "search":
        return {"entries": [asdict(entry) for entry in store.search(MemorySearch(args.query, args.kind, args.limit))]}
    if args.action == "add":
        return asdict(store.add(MemoryDraft(args.kind, args.content, args.subject, source, args.event_at)))
    if args.action == "replace":
        patch = MemoryPatch(args.content, args.subject if "subject" in args.model_fields_set else None, source,
                            args.event_at if "event_at" in args.model_fields_set else UNSET)
        return asdict(store.update(args.id, patch, expected_version=args.expected_version))
    return asdict(store.delete(args.id, expected_version=args.expected_version))


def _policy(args: MemoryArgs) -> ToolExecutionPolicy:
    return ToolExecutionPolicy(
        side_effects="none" if args.action in {"list", "search"} else "possible",
        approval_scopes=("memory:edit",) if args.action in {"replace", "remove"} else (),
    )


def memory_entry(store: MemoryStore, *, review: bool = False, source_context: dict | None = None):
    def handle(args: MemoryArgs, context: ToolContext):
        context.check_active()
        if review and args.action not in {"list", "search", "add"}:
            raise PermissionError("background review may only list, search or add memory")
        source = {"type": "review" if review else "tool", "session_id": context.session_id, "turn_id": context.turn_id}
        source.update(source_context or {})
        result = execute_memory(store, args, source)
        context.check_active()
        return result
    return ToolEntry(
        tool_id="memory.memory", implementation_version="2", name="memory", toolset="memory",
        description=(
            "Manage durable database memory. kind: user for confirmed stable preferences/facts, note for project knowledge, episode for historical events. "
            "List records or search using short keywords (not semantic search). Add only useful, nonduplicative facts; skip assistant guesses and conflicting current facts. "
            + ("Background review may only list/search/add." if review else
               "Explicit corrections use replace/remove with the original string id and expected_version. Latest tool records supersede older bootstrap versions.")
        ),
        args_model=ReviewMemoryArgs if review else MemoryArgs, handler=handle,
        side_effects="possible", policy_resolver=_policy,
    )
