"""Pure transcript validation and interrupted tool-result construction."""

from __future__ import annotations

from typing import Sequence

from .models import Message, ToolCall
from tools.results import encode_tool_error


def pending_tool_calls(messages: Sequence[Message]) -> list[tuple[ToolCall, str]]:
    pending: dict[str, tuple[ToolCall, str]] = {}
    for message in messages:
        if message.role == "tool":
            expected = pending.pop(message.tool_call_id, None)
            if expected is None or expected[0].name != message.tool_name:
                raise ValueError("history contains an unmatched tool result")
        else:
            if pending:
                raise ValueError("history contains unresolved tool calls before another message")
            effects = message.tool_call_effects or {}
            pending = {
                call.id: (call, effects.get(call.id, "possible"))
                for call in message.tool_calls or ()
            }
    return list(pending.values())


def validate_complete_history(messages: Sequence[Message]) -> None:
    if pending_tool_calls(messages):
        raise ValueError("history contains unresolved tool calls")


def interrupted_tool_results(messages: Sequence[Message], max_chars: int) -> list[Message]:
    return [
        Message.tool_result(
            encode_tool_error("interrupted", "Tool result was not committed before interruption.", max_chars),
            tool_call_id=call.id, tool_name=call.name,
            tool_outcome_state="unknown", tool_error_type="interrupted",
            effect_disposition="none" if effect == "none" else "unknown",
        )
        for call, effect in pending_tool_calls(messages)
    ]


__all__ = ["pending_tool_calls", "validate_complete_history", "interrupted_tool_results"]
