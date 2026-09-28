from __future__ import annotations

import inspect
import json
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from .interfaces import (
    TurnHostContext, CancellationToken, ToolContext, ToolGuard, ToolInvocation,
    TurnCancelled,
)
from .models import AgentConfig, Message, ToolCall, ToolOutcome
from .turn_state import (
    LoopState, RunBudgetExceeded, TurnRuntime,
)
from .workers import WorkerQueueFullError
from tools.results import encode_tool_error, encode_tool_success
from tools.registry import ToolExecutionPolicy
from tools.context import ToolInteractions
from tools.lifecycle import ResourceUnavailableError
from .approval import ApprovalContext
from .interaction import InteractionUnavailable


@dataclass(frozen=True, slots=True)
class ToolExecution:
    message: Message
    outcome: ToolOutcome
    interrupt_reason: str | None = None


class ToolExecutor:
    """Validate, authorize, invoke, and normalize one tool call."""

    def __init__(
        self, *, config: AgentConfig,
        guard: ToolGuard, host_context: TurnHostContext,
    ) -> None:
        self._config = config
        self._guard = guard
        self._host_context = host_context

    def execute(
        self, runtime: TurnRuntime, state: LoopState, tool_call: ToolCall,
    ) -> ToolExecution:
        context = state.require_context("execute_tool")
        runtime.check_active()
        entry = context.entries_by_name.get(tool_call.name)
        if entry is None:
            return self._tool_result(
                tool_call,
                encode_tool_error(
                    "unknown_tool", f"Unknown tool: {tool_call.name}",
                    self._config.tool_result_max_chars,
                ),
                side_effects="possible", state_name="failed",
                effect="not_applied", error_type="unknown_tool",
            )

        # 先完成参数校验，再计算执行策略；两者成功也不等于已通过后面的 Guard/审批。
        try:
            # 模型给出的是 JSON 字符串；空参数按空对象解析，必填字段仍由参数模型检查。
            raw_args = json.loads(tool_call.arguments or "{}")
            # 合法 JSON 也可能是数组、字符串或 null，但工具参数必须是“字段名 -> 值”的对象。
            if not isinstance(raw_args, dict):
                raise ValueError("tool arguments must decode to an object")
            # 使用该工具注册的 Pydantic 模型检查字段、类型和约束，得到供策略及 handler 使用的实例。
            args_model = entry.args_model.model_validate(raw_args)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            # 参数错误转为有界 tool result，让模型有机会修正调用，而不是终止整个对话循环。
            # 此时未进入 handler：failed 表示调用失败，not_applied 表示本次操作未执行。
            return self._tool_result(
                tool_call,
                encode_tool_error(
                    "invalid_arguments", f"{type(exc).__name__}: {exc}",
                    self._config.tool_result_max_chars,
                ),
                side_effects=entry.side_effects, state_name="failed",
                effect="not_applied", error_type="invalid_arguments",
            )

        try:
            # 同一工具的不同动作可能需要不同审批，例如读取桌面与向应用输入内容。
            # resolver 接收校验后的参数，返回完整策略；存在时不再叠加下面的默认声明。
            policy = (
                entry.policy_resolver(args_model) if entry.policy_resolver
                else ToolExecutionPolicy(
                    side_effects=entry.side_effects,
                    # 无动态策略时，需要审批就以工具名作为默认 scope；逗号表示单元素元组。
                    approval_scopes=(entry.name,) if entry.requires_approval else (),
                )
            )
            if not isinstance(policy, ToolExecutionPolicy):
                raise TypeError("policy resolver must return ToolExecutionPolicy")
        except BaseException as exc:
            # 无法确定安全策略时按拒绝处理（fail closed），不能回退到默认放行或执行 handler。
            return self._tool_result(
                tool_call,
                encode_tool_error(
                    "policy_error", f"{type(exc).__name__}: {exc}",
                    self._config.tool_result_max_chars,
                ),
                side_effects=entry.side_effects, state_name="denied",
                effect="not_applied", error_type="policy_error",
            )
        invocation = self._invocation(
            state, tool_call, args_model.model_dump(mode="json"),
            policy.approval_scopes,
        )
        assert state.workspace is not None and state.resource_key is not None
        revoked = threading.Event()

        def check_active():
            if revoked.is_set():
                raise ResourceUnavailableError("tool call is no longer active")
            runtime.check_active()
            runtime.services.check_open()

        interactions = ToolInteractions(
            runtime.services, self._host_context, state.resource_key,
            runtime.owner_id, state.turn_id, tool_call.id,
            check_active, runtime.pause_budget, runtime.resume_budget,
        )
        approval_context = ApprovalContext(
            state.resource_key, interactions, check_active,
            lambda callback: runtime.call_operation(
                callback, timeout=self._config.timeouts.policy_seconds,
                cancel_event=runtime.cancel_event, deadline=runtime.deadline,
            ),
        )
        try:
            denied = self._authorize(runtime, invocation, approval_context)
        except BaseException:
            revoked.set()
            raise
        if denied is not None:
            revoked.set()
            return self._tool_result(
                tool_call,
                encode_tool_error(
                    denied[0], denied[1], self._config.tool_result_max_chars
                ),
                side_effects=policy.side_effects, state_name="denied",
                effect="not_applied", error_type=denied[0],
            )

        started = threading.Event()
        cancellation = CancellationToken(runtime.cancel_event)

        def invoke() -> Any:
            check_active()
            started.set()
            runtime.observability.publish(
                "tool.running", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round", payload={"tool_name": tool_call.name},
            )
            value = entry.handler(args_model, ToolContext(
                session_id=state.session_id, turn_id=state.turn_id,
                tool_call_id=tool_call.id, workspace=state.workspace,
                interactions=interactions, services=runtime.services, resource_key=state.resource_key,
                cancellation=cancellation, check_active=check_active,
            ))
            if inspect.isawaitable(value):
                close = getattr(value, "close", None)
                if close is not None:
                    close()
                raise TypeError("tool handler returned an awaitable")
            return value

        try:
            if entry.interactive_wait:
                value = invoke()
            else:
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
                tool_call, policy.side_effects,
                started=started.is_set(), reason="cancelled",
            )
        except RunBudgetExceeded:
            result = self._unknown_or_not_executed(
                tool_call, policy.side_effects,
                started=started.is_set(), reason="run_budget",
            )
            if not started.is_set():
                raise
            return result
        except TimeoutError:
            return self._unknown_or_not_executed(
                tool_call, policy.side_effects,
                started=started.is_set(), reason="tool_timeout",
            )
        except WorkerQueueFullError:
            return self._unknown_or_not_executed(
                tool_call, policy.side_effects,
                started=False, reason="executor_saturated",
            )
        except BaseException as exc:
            effect = (
                "none" if policy.side_effects == "none"
                else ("unknown" if started.is_set() else "not_applied")
            )
            return self._tool_result(
                tool_call,
                encode_tool_error(
                    "tool_execution_error", f"{type(exc).__name__}: {exc}",
                    self._config.tool_result_max_chars,
                ),
                side_effects=policy.side_effects,
                state_name="failed" if started.is_set() else "not_executed",
                effect=effect, error_type="tool_execution_error",
            )
        finally:
            # The turn may continue after a timeout; this call's context must expire.
            # Stateful backends keep their own operation locks until the handler exits.
            revoked.set()

        effect = "none" if policy.side_effects == "none" else "confirmed"
        return self._tool_result(
            tool_call,
            encode_tool_success(value, self._config.tool_result_max_chars),
            side_effects=policy.side_effects, state_name="succeeded",
            effect=effect, error_type=None,
        )

    def _authorize(
        self, runtime: TurnRuntime, invocation: ToolInvocation,
        approval_context: ApprovalContext,
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
        if not invocation.requires_approval:
            return None
        try:
            approval = runtime.services.approvals.authorize(invocation, approval_context)
        except InteractionUnavailable:
            return "approval_required", "tool requires an interaction host"
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
        self, call: ToolCall, side_effects: str,
        *, started: bool, reason: str,
    ) -> ToolExecution:
        if started:
            return self._tool_result(
                call,
                encode_tool_error(
                    "outcome_unknown",
                    "Tool execution stopped being observed; its outcome is unknown.",
                    self._config.tool_result_max_chars,
                ),
                side_effects=side_effects, state_name="unknown",
                effect="none" if side_effects == "none" else "unknown",
                error_type=reason,
                interrupt_reason=(
                    reason if reason in {"cancelled", "run_budget"} else None
                ),
            )
        return self._tool_result(
            call,
            encode_tool_error(
                "not_executed", "Tool handler was not entered.",
                self._config.tool_result_max_chars,
            ),
            side_effects=side_effects, state_name="not_executed",
            effect="not_applied", error_type=reason,
            interrupt_reason=(
                reason if reason in {"cancelled", "run_budget"} else None
            ),
        )

    @staticmethod
    def _invocation(
        state: LoopState, call: ToolCall, arguments: dict[str, Any],
        approval_scopes: tuple[str, ...],
    ) -> ToolInvocation:
        return ToolInvocation(
            session_id=state.session_id, turn_id=state.turn_id,
            tool_call_id=call.id, tool_name=call.name,
            arguments=MappingProxyType(arguments),
            requires_approval=bool(approval_scopes),
            approval_scopes=approval_scopes,
        )

    @staticmethod
    def _tool_result(
        call: ToolCall, content: str, *,
        side_effects: str, state_name: str,
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
                tool_outcome_state=state_name,
                tool_error_type=error_type,
            ),
            outcome=outcome,
            interrupt_reason=interrupt_reason,
        )


__all__ = ["ToolExecution", "ToolExecutor"]
