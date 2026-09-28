"""One memory tool with a restricted handler for background reviews."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentcore.interfaces import ToolContext
from tools.registry import ToolEntry, ToolExecutionPolicy

from .store import FileMemoryStore, MemoryCapacityError, MemoryOperation

logger = logging.getLogger(__name__)


class MemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "add", "replace", "remove"]
    target: Literal["memory", "user"]
    content: str = Field(default="", max_length=5000)
    old_text: str = Field(default="", max_length=5000)


class ReviewMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "add"]
    target: Literal["memory", "user"]
    content: str = Field(default="", max_length=5000)


def _policy(args: MemoryArgs) -> ToolExecutionPolicy:
    return ToolExecutionPolicy(
        side_effects="none" if args.action == "list" else "possible",
        approval_scopes=("memory:edit",) if args.action in {"replace", "remove"} else (),
    )


def _review_policy(args: MemoryArgs) -> ToolExecutionPolicy:
    return ToolExecutionPolicy(side_effects="none" if args.action == "list" else "possible")


def memory_entry(store: FileMemoryStore, *, review: bool = False) -> ToolEntry[MemoryArgs | ReviewMemoryArgs]:
    def handle(args: MemoryArgs | ReviewMemoryArgs, context: ToolContext) -> dict:
        context.check_active()
        if review and args.action not in {"list", "add"}:
            raise PermissionError("background review may only list or add memory")
        if args.action == "list":
            doc = store.read(args.target)
            return {"target": doc.target, "entries": doc.entries,
                    "version": doc.version, "chars": doc.chars}
        try:
            result = store.apply(MemoryOperation(
                action=args.action, target=args.target, content=args.content,
                old_text=getattr(args, "old_text", ""),
            ))
        except MemoryCapacityError:
            if not review:
                raise
            logger.info("memory.review_skipped target=%s review_id=%s reason=capacity",
                        args.target, context.turn_id)
            return {"target": args.target, "action": args.action,
                    "changed": False, "skipped": "capacity"}
        context.check_active()
        logger.info("memory.tool_write target=%s action=%s version=%s turn_id=%s",
                    result.target, result.action, result.version[:12], context.turn_id)
        return asdict(result)

    return ToolEntry(
        tool_id="file_memory.memory", implementation_version="1", name="memory",
        description=(
            "List or add durable memory entries; targets are memory for long-term notes "
            "and user for stable user facts and preferences."
            if review else
            "Manage durable memory. Targets: memory for long-term notes, user for stable user facts and preferences. List, add, replace, or remove one entry. Replace/remove require old_text identifying one entry."
        ),
        args_model=ReviewMemoryArgs if review else MemoryArgs, handler=handle, toolset="memory",
        side_effects="possible",
        policy_resolver=_review_policy if review else _policy,
    )
