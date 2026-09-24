from __future__ import annotations

import inspect
import json
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from .interfaces import (
    Approver, CancellationToken, ToolContext, ToolGuard, ToolInvocation,
    TurnCancelled,
)
from .models import AgentConfig, Message, ToolCall, ToolOutcome
from .storage import SQLiteStore
from .turn_state import (
    LoopState, OperationTimeoutError, RunBudgetExceeded, TurnRuntime,
)
from .workers import WorkerQueueFullError
from tools.results import encode_tool_error, encode_tool_success


@dataclass(frozen=True, slots=True)
class ToolExecution:
    message: Message
    outcome: ToolOutcome
    invocation: ToolInvocation
    interrupt_reason: str | None = None


class ToolExecutor:
    """Validate, authorize, invoke, and normalize one tool call."""

    def __init__(
        self, *, config: AgentConfig, store: SQLiteStore,
        guard: ToolGuard, approver: Approver | None,
    ) -> None:
        self._config = config
        self._store = store
        self._guard = guard
        self._approver = approver

    def execute(
        self, runtime: TurnRuntime, state: LoopState, tool_call: ToolCall,
        *, parent_message_id: int,
    ) -> ToolExecution:
        context = state.require_context("execute_tool")
        lease = state.require_lease("execute_tool")
        entry = context.entries_by_name.get(tool_call.name)
        if entry is None:
            invocation = self._invocation(state, tool_call, {}, False)
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    "unknown_tool", f"Unknown tool: {tool_call.name}",
                    self._config.tool_result_max_chars,
                ),
                parent_message_id=parent_message_id,
                side_effects="possible", state_name="failed",
                effect="not_applied", error_type="unknown_tool",
            )

        try:
            raw_args = json.loads(tool_call.arguments or "{}")
            if not isinstance(raw_args, dict):
                raise ValueError("tool arguments must decode to an object")
            args_model = entry.args_model.model_validate(raw_args)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            invocation = self._invocation(
                state, tool_call, {}, entry.requires_approval
            )
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    "invalid_arguments", f"{type(exc).__name__}: {exc}",
                    self._config.tool_result_max_chars,
                ),
                parent_message_id=parent_message_id,
                side_effects=entry.side_effects, state_name="failed",
                effect="not_applied", error_type="invalid_arguments",
            )

        invocation = self._invocation(
            state, tool_call, args_model.model_dump(mode="json"),
            entry.requires_approval,
        )
        denied = self._authorize(runtime, invocation, entry.requires_approval)
        if denied is not None:
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    denied[0], denied[1], self._config.tool_result_max_chars
                ),
                parent_message_id=parent_message_id,
                side_effects=entry.side_effects, state_name="denied",
                effect="not_applied", error_type=denied[0],
            )

        started = threading.Event()
        cancellation = CancellationToken(runtime.cancel_event)

        def invoke() -> Any:
            cancellation.raise_if_cancelled()
            runtime.check_active(state)
            self._store.assert_lease(lease)
            started.set()
            runtime.observability.publish(
                "tool.running", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round", payload={"tool_name": tool_call.name},
            )
            value = entry.handler(
                args_model,
                ToolContext(
                    state.session_id, state.turn_id, tool_call.id, cancellation
                ),
            )
            if inspect.isawaitable(value):
                close = getattr(value, "close", None)
                if close is not None:
                    close()
                raise TypeError("tool handler returned an awaitable")
            return value

        try:
            value = runtime.call_operation(
                invoke,
                timeout=(
                    entry.timeout_seconds or self._config.timeouts.tool_seconds
                ),
                cancel_event=runtime.cancel_event,
                deadline=runtime.deadline,
            )
        except TurnCancelled:
            return self._unknown_or_not_executed(
                tool_call, invocation, entry.side_effects,
                parent_message_id=parent_message_id,
                started=started.is_set(), reason="cancelled",
            )
        except RunBudgetExceeded:
            result = self._unknown_or_not_executed(
                tool_call, invocation, entry.side_effects,
                parent_message_id=parent_message_id,
                started=started.is_set(), reason="run_budget",
            )
            if not started.is_set():
                raise
            return result
        except OperationTimeoutError:
            return self._unknown_or_not_executed(
                tool_call, invocation, entry.side_effects,
                parent_message_id=parent_message_id,
                started=started.is_set(), reason="tool_timeout",
            )
        except WorkerQueueFullError:
            return self._unknown_or_not_executed(
                tool_call, invocation, entry.side_effects,
                parent_message_id=parent_message_id,
                started=False, reason="executor_saturated",
            )
        except BaseException as exc:
            effect = (
                "none" if entry.side_effects == "none"
                else ("unknown" if started.is_set() else "not_applied")
            )
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    "tool_execution_error", f"{type(exc).__name__}: {exc}",
                    self._config.tool_result_max_chars,
                ),
                parent_message_id=parent_message_id,
                side_effects=entry.side_effects,
                state_name="failed" if started.is_set() else "not_executed",
                effect=effect, error_type="tool_execution_error",
            )

        effect = "none" if entry.side_effects == "none" else "confirmed"
        return self._tool_result(
            tool_call, invocation,
            encode_tool_success(value, self._config.tool_result_max_chars),
            parent_message_id=parent_message_id,
            side_effects=entry.side_effects, state_name="succeeded",
            effect=effect, error_type=None,
        )

    def _authorize(
        self, runtime: TurnRuntime, invocation: ToolInvocation,
        requires_approval: bool,
    ) -> tuple[str, str] | None:
        try:
            decision = runtime.call_operation(
                lambda: self._guard.check(invocation),
                timeout=self._config.timeouts.policy_seconds,
                cancel_event=runtime.cancel_event, deadline=runtime.deadline,
            )
        except (TurnCancelled, RunBudgetExceeded):
            raise
        except BaseException as exc:
            return (
                "guard_error",
                f"tool guard failed closed: {type(exc).__name__}: {exc}",
            )
        if not decision.allowed:
            return "policy_denied", decision.reason or "tool call denied by policy"
        if not requires_approval:
            return None
        if self._approver is None:
            return (
                "approval_required",
                "tool requires approval but no Approver is configured",
            )
        try:
            approval = runtime.call_operation(
                lambda: self._approver.approve(invocation),
                timeout=self._config.timeouts.policy_seconds,
                cancel_event=runtime.cancel_event, deadline=runtime.deadline,
            )
        except (TurnCancelled, RunBudgetExceeded):
            raise
        except BaseException as exc:
            return (
                "approval_error",
                f"approval failed closed: {type(exc).__name__}: {exc}",
            )
        if not approval.approved:
            return "approval_denied", approval.reason or "tool call was denied"
        return None

    def _unknown_or_not_executed(
        self, call: ToolCall, invocation: ToolInvocation, side_effects: str,
        *, parent_message_id: int, started: bool, reason: str,
    ) -> ToolExecution:
        if started:
            return self._tool_result(
                call, invocation,
                encode_tool_error(
                    "outcome_unknown",
                    "Tool execution stopped being observed; its outcome is unknown.",
                    self._config.tool_result_max_chars,
                ),
                parent_message_id=parent_message_id,
                side_effects=side_effects, state_name="unknown",
                effect="none" if side_effects == "none" else "unknown",
                error_type=reason,
                interrupt_reason=(
                    reason if reason in {"cancelled", "run_budget"} else None
                ),
            )
        return self._tool_result(
            call, invocation,
            encode_tool_error(
                "not_executed", "Tool handler was not entered.",
                self._config.tool_result_max_chars,
            ),
            parent_message_id=parent_message_id,
            side_effects=side_effects, state_name="not_executed",
            effect="not_applied", error_type=reason,
            interrupt_reason=(
                reason if reason in {"cancelled", "run_budget"} else None
            ),
        )

    @staticmethod
    def _invocation(
        state: LoopState, call: ToolCall, arguments: dict[str, Any],
        requires_approval: bool,
    ) -> ToolInvocation:
        return ToolInvocation(
            session_id=state.session_id, turn_id=state.turn_id,
            tool_call_id=call.id, tool_name=call.name,
            arguments=MappingProxyType(arguments),
            requires_approval=requires_approval,
        )

    @staticmethod
    def _tool_result(
        call: ToolCall, invocation: ToolInvocation, content: str, *,
        parent_message_id: int, side_effects: str, state_name: str,
        effect: str, error_type: str | None,
        interrupt_reason: str | None = None,
    ) -> ToolExecution:
        outcome = ToolOutcome(
            tool_call_id=call.id, tool_name=call.name,
            side_effects=side_effects, state=state_name,
            effect_disposition=effect, error_type=error_type,
        )
        return ToolExecution(
            message=Message.tool_result(
                content, tool_call_id=call.id, tool_name=call.name,
                effect_disposition=effect,
                reply_to_message_id=parent_message_id,
                tool_outcome_state=state_name,
                tool_error_type=error_type,
            ),
            outcome=outcome, invocation=invocation,
            interrupt_reason=interrupt_reason,
        )


__all__ = ["ToolExecution", "ToolExecutor"]
