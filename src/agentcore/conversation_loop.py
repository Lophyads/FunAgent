from __future__ import annotations

import inspect
import json
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from .interfaces import (
    Approver, CancellationToken, MemoryUpdater, ReviewScheduler, ToolContext,
    ToolGuard, ToolInvocation, TurnCancelled, TurnSnapshot,
)
from .lease import LeaseAcquireTimeout, LeaseHeartbeatService, TurnLeaseStore
from .models import (
    AgentConfig, CanonicalModelRequest, Message, NormalizedModelResponse,
    ToolCall, ToolOutcome, TurnError, TurnResult, UserInput,
)
from .provider_runtime import (
    ApiModeRegistry, AttemptContext, NormalizedProviderError, ProviderResolver,
)
from .session_context import (
    SessionBuildRequest, SessionContextFactory, SessionSnapshotConflict,
    ToolsetRebindError,
)
from .storage import LeaseLostError, SQLiteStore, StorageError
from .tools import encode_tool_error, encode_tool_success
from .turn_state import (
    AttemptState, FunctionPhase, LoopState, OperationTimeoutError, PhaseAction,
    PhaseResult, RunBudgetExceeded, TurnRuntime, run_phase,
)
from .workers import WorkerQueueFullError


logger = logging.getLogger(__name__)


class ProviderExhausted(RuntimeError):
    def __init__(self, failures: list[dict[str, Any]]) -> None:
        super().__init__("all provider routes were exhausted")
        self.failures = failures


class ProviderRequestAborted(RuntimeError):
    def __init__(self, route_name: str, error: NormalizedProviderError) -> None:
        super().__init__(str(error))
        self.route_name = route_name
        self.error = error


@dataclass(slots=True)
class RouteState:
    cooldown_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> float:
        with self.lock:
            return self.cooldown_until - time.monotonic()

    def set_cooldown(self, seconds: float) -> None:
        with self.lock:
            self.cooldown_until = time.monotonic() + max(0.0, seconds)

    def clear(self) -> None:
        with self.lock:
            self.cooldown_until = 0.0


@dataclass(frozen=True, slots=True)
class _ToolExecution:
    message: Message
    outcome: ToolOutcome
    invocation: ToolInvocation
    interrupt_reason: str | None = None


class ConversationLoop:
    """Synchronous phase orchestrator; one instance executes one submitted turn."""

    def __init__(
        self, *, config: AgentConfig, user_input: UserInput,
        session_id: str, turn_id: str, owner_id: str,
        session_request: SessionBuildRequest | None,
        cancel_event: threading.Event, store: SQLiteStore,
        session_contexts: SessionContextFactory, lease_store: TurnLeaseStore,
        heartbeat: LeaseHeartbeatService, provider_resolver: ProviderResolver,
        api_modes: ApiModeRegistry, route_states: tuple[RouteState, ...],
        guard: ToolGuard, approver: Approver | None,
        memory_updater: MemoryUpdater, review_scheduler: ReviewScheduler,
        observability: Any, call_operation: Any,
    ) -> None:
        self.config = config
        self.user_input = user_input
        self.session_id = session_id
        self.turn_id = turn_id
        self.owner_id = owner_id
        self.session_request = session_request
        self.cancel_event = cancel_event
        self.store = store
        self.session_contexts = session_contexts
        self.lease_store = lease_store
        self.heartbeat = heartbeat
        self.provider_resolver = provider_resolver
        self.api_modes = api_modes
        self.route_states = route_states
        self.guard = guard
        self.approver = approver
        self.memory_updater = memory_updater
        self.review_scheduler = review_scheduler
        self.observability = observability
        self.call_operation = call_operation

    def run(self) -> TurnResult:
        budget = self.config.timeouts.run_budget_seconds
        deadline = time.monotonic() + budget if budget is not None else None
        state = LoopState(
            session_id=self.session_id, turn_id=self.turn_id,
            trace_id=self.turn_id,
        )
        runtime = TurnRuntime(
            store=self.store, lease_store=self.lease_store,
            observability=self.observability, cancel_event=self.cancel_event,
            lease_lost_event=threading.Event(), deadline=deadline,
            call_operation=self.call_operation,
        )
        try:
            run_phase(self._admission_phase(), runtime, state)
            for iteration in range(1, self.config.max_iterations + 1):
                state.iteration = iteration
                state.response = None
                state.assistant_message = None
                run_phase(self._assemble_phase(), runtime, state)
                run_phase(self._provider_phase(), runtime, state)
                assert state.response is not None
                phase = (
                    self._tool_round_phase()
                    if state.response.tool_calls else self._finish_text_phase()
                )
                result = run_phase(phase, runtime, state)
                if result.action is PhaseAction.NEXT_ITERATION:
                    continue
                break
            else:
                state.status = "failed"
                state.completion_reason = "max_iterations"
                state.error = TurnError(
                    code="max_iterations",
                    message=f"agent exceeded {self.config.max_iterations} model iterations",
                    retryable=False,
                )
        except SessionSnapshotConflict as exc:
            self._set_error(state, "failed", "session_snapshot_conflict", exc, False)
        except ToolsetRebindError as exc:
            state.warnings.extend(runtime.observability.publish(
                "session.rebind_failed", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"error_type": type(exc).__name__},
            ))
            self._set_error(state, "failed", "toolset_rebind_failed", exc, False)
        except LeaseAcquireTimeout as exc:
            self._set_error(state, "failed", "lease_timeout", exc, True)
        except LeaseLostError as exc:
            state.warnings.extend(runtime.observability.publish(
                "lease.fenced_write_rejected", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"error_type": type(exc).__name__},
            ))
            self._set_error(state, "interrupted", "lease_lost", exc, True)
        except TurnCancelled as exc:
            reason = "lease_lost" if runtime.lease_lost_event.is_set() else "cancelled"
            self._set_error(state, "interrupted", reason, exc, True)
        except RunBudgetExceeded as exc:
            self._set_error(state, "interrupted", "run_budget_exhausted", exc, True)
        except ProviderExhausted as exc:
            state.status = "failed"
            state.completion_reason = "providers_exhausted"
            state.error = TurnError(
                code="providers_exhausted", message=str(exc), retryable=True,
                details={"failures": exc.failures},
            )
        except ProviderRequestAborted as exc:
            state.route_used = exc.route_name
            state.status = "failed"
            state.completion_reason = "provider_request_aborted"
            state.error = TurnError(
                code=exc.error.error_type, message=str(exc.error),
                retryable=exc.error.retryable,
                details={"status_code": exc.error.status_code},
            )
        except BaseException as exc:
            logger.exception("conversation loop failed")
            self._set_error(state, "failed", "turn_failed", exc, False)
        finally:
            if state.error is not None and not state.finalized:
                try:
                    run_phase(self._handle_error_phase(), runtime, state)
                except BaseException as exc:
                    state.warnings.append(
                        "handle_turn_error phase failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            self._cleanup(runtime, state)
        return self._result(state)

    def _handle_error_phase(self) -> FunctionPhase:
        def handle(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.status not in {"failed", "interrupted"} or state.error is None:
                raise RuntimeError("error phase requires a non-completed structured error")

        return FunctionPhase(
            "handle_turn_error", handle, validate, check_active=False
        )

    def _admission_phase(self) -> FunctionPhase:
        def admission(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            runtime.observability.publish(
                "lease.wait_started", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
            )
            lease = self._acquire_lease(runtime, state)
            state.lease = lease
            self.heartbeat.register(lease, runtime.lease_lost_event)
            state.warnings.extend(runtime.observability.publish(
                "lease.acquired", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"fencing_token": lease.fencing_token},
            ))
            recovery = self.store.recover_incomplete_turns(
                state.session_id, fence=lease.fence
            )
            state.warnings.extend(recovery.warnings)
            if recovery.warnings:
                runtime.observability.publish(
                    "recovery.incomplete_turn_found", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    payload={"warning_count": len(recovery.warnings)},
                )
            if recovery.messages:
                runtime.observability.publish(
                    "recovery.tool_pair_repaired", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    payload={"repair_count": len(recovery.messages)},
                )
            if recovery.warnings or recovery.messages:
                runtime.observability.publish(
                    "recovery.recovery_completed", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                )
            resolution = self.session_contexts.create_or_load(
                state.session_id, self.user_input, self.session_request, lease
            )
            state.context = resolution.snapshot
            state.warnings.extend(resolution.warnings)
            if any("defaults differ" in item for item in resolution.warnings):
                state.warnings.extend(runtime.observability.publish(
                    "session.config_mismatch_deferred", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                ))
            user_message = Message.user(
                self.user_input.text, metadata=self.user_input.metadata
            )
            if not resolution.created:
                self.store.begin_turn(
                    turn_id=state.turn_id, session_id=state.session_id,
                    context_version=resolution.snapshot.context_version,
                    user_message=user_message, fence=lease.fence,
                )
            state.messages = self.store.load_messages(state.session_id)
            state.new_messages.append(user_message)
            state.admitted = True
            event = "session.created" if resolution.created else "session.loaded"
            state.warnings.extend(runtime.observability.publish(
                event, trace_id=state.trace_id, session_id=state.session_id,
                turn_id=state.turn_id,
                payload={"context_version": resolution.snapshot.context_version},
            ))
            state.warnings.extend(runtime.observability.publish(
                "turn.admitted", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"context_version": resolution.snapshot.context_version},
            ))
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.lease is None or state.context is None or not state.admitted:
                raise RuntimeError("admission did not establish lease, context, and turn")

        return FunctionPhase("admission", admission, validate, check_active=False)

    def _assemble_phase(self) -> FunctionPhase:
        def assemble(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            assert state.context is not None
            state.request = CanonicalModelRequest.build(
                system_prompt=state.context.prompt.text,
                messages=tuple(state.messages),
                tool_schema_bytes=state.context.toolset.schema_bytes,
            )
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.request is None or not state.request.fingerprint:
                raise RuntimeError("canonical request was not assembled")

        return FunctionPhase("assemble_request", assemble, validate)

    def _provider_phase(self) -> FunctionPhase:
        def provider(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            response, route_index = self._run_provider_attempts(runtime, state)
            state.response = response
            state.active_route_index = route_index
            state.route_used = self.config.routes[route_index].name
            for key, value in response.usage.items():
                if isinstance(value, int):
                    state.usage[key] = state.usage.get(key, 0) + value
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.response is None:
                raise RuntimeError("provider phase produced no normalized response")

        return FunctionPhase("provider_attempts", provider, validate)

    def _tool_round_phase(self) -> FunctionPhase:
        def execute(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            assert state.response is not None and state.response.tool_calls
            self._run_tool_round(runtime, state, state.response)
            return PhaseResult(PhaseAction.NEXT_ITERATION)

        def validate(state: LoopState) -> None:
            assert state.response is not None
            wanted = {call.id for call in state.response.tool_calls}
            actual = {
                message.tool_call_id for message in state.new_messages
                if message.role == "tool"
            }
            if not wanted.issubset(actual):
                raise RuntimeError("tool round left unresolved tool calls")

        return FunctionPhase("tool_round", execute, validate)

    def _finish_text_phase(self) -> FunctionPhase:
        def finish(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            assert state.response is not None and not state.response.tool_calls
            text = state.response.content or ""
            assistant = Message.assistant_text(
                text, finish_reason=state.response.finish_reason
            )
            if state.response.finish_reason in {"length", "content_filter"}:
                state.status = "failed"
                state.completion_reason = "incomplete_response"
                state.error = TurnError(
                    code="incomplete_response",
                    message=f"model stopped with finish_reason={state.response.finish_reason}",
                    retryable=state.response.finish_reason == "length",
                )
            else:
                state.status = "completed"
                state.completion_reason = "model_stop"
                state.error = None
            assert state.lease is not None
            committed = self.store.finish_turn_with_message(
                session_id=state.session_id, turn_id=state.turn_id,
                message=assistant, status=state.status, error=state.error,
                route_name=state.route_used, iterations=state.iteration,
                usage=state.usage, completion_reason=state.completion_reason,
                provider_attempts=state.provider_attempts,
                route_attempts=state.route_attempts, fence=state.lease.fence,
            )
            if not committed:
                raise StorageError("terminal turn was already finalized")
            state.assistant_message = assistant
            state.final_text = text
            state.messages.append(assistant)
            state.new_messages.append(assistant)
            state.finalized = True
            return PhaseResult(PhaseAction.STOP_TURN)

        def validate(state: LoopState) -> None:
            if state.response is None or state.response.tool_calls:
                raise RuntimeError("finish_text requires a text response")
            if not state.finalized:
                raise RuntimeError("terminal assistant was not atomically finalized")

        return FunctionPhase("finish_text", finish, validate)

    def _run_provider_attempts(
        self, runtime: TurnRuntime, state: LoopState,
    ) -> tuple[NormalizedModelResponse, int]:
        assert state.request is not None
        failures: list[dict[str, Any]] = []
        for route_index in range(state.active_route_index, len(self.config.routes)):
            route = self.config.routes[route_index]
            route_state = self.route_states[route_index]
            binding = self.provider_resolver.resolve(route)
            adapter = self.api_modes.get(route.api_mode)
            if route.api_mode not in binding.capabilities:
                raise ProviderRequestAborted(
                    route.name,
                    NormalizedProviderError(
                        f"provider does not support {route.api_mode}",
                        error_type="capability_mismatch",
                    ),
                )
            max_attempts = route.max_attempts or self.config.retry.max_attempts
            for attempt_number in range(1, max_attempts + 1):
                runtime.check_active(state)
                cooldown = route_state.remaining()
                if cooldown > 0:
                    self._wait(runtime, state, cooldown)
                attempt_id = str(uuid.uuid4())
                state.attempt = AttemptState(
                    attempt_id=attempt_id, route_index=route_index,
                    route_name=route.name, attempt_number=attempt_number,
                    request_fingerprint=state.request.fingerprint,
                    started_at=time.time(),
                )
                state.provider_attempts += 1
                state.route_attempts[route.name] = (
                    state.route_attempts.get(route.name, 0) + 1
                )
                assert state.lease is not None
                self.store.begin_model_attempt(
                    attempt_id=attempt_id, turn_id=state.turn_id,
                    session_id=state.session_id, iteration=state.iteration,
                    route_index=route_index, route_name=route.name,
                    attempt_number=attempt_number,
                    request_fingerprint=state.request.fingerprint,
                    fence=state.lease.fence,
                )
                state.warnings.extend(runtime.observability.publish(
                    "provider.attempt_started", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    phase="provider_attempts",
                    payload={
                        "route": route.name, "iteration": state.iteration,
                        "attempt": attempt_number,
                    },
                ))
                try:
                    wire = adapter.encode(state.request, binding)
                    raw_response = binding.transport.send(
                        wire,
                        AttemptContext(
                            attempt_id=attempt_id, session_id=state.session_id,
                            turn_id=state.turn_id, iteration=state.iteration,
                            route_name=route.name,
                            attempt_number=attempt_number,
                            deadline=runtime.deadline,
                            cancellation=CancellationToken(runtime.cancel_event),
                        ),
                    )
                    runtime.check_active(state)
                    normalized = adapter.decode(raw_response, binding)
                except (TurnCancelled, RunBudgetExceeded, LeaseLostError):
                    self.store.finish_model_attempt(
                        attempt_id, state="ambiguous",
                        error_type="interrupted", status_code=None,
                        fence=state.lease.fence,
                    )
                    raise
                except BaseException as exc:
                    error = adapter.normalize_error(exc)
                    state.attempt.provider_error = error
                    self.store.finish_model_attempt(
                        attempt_id,
                        state="ambiguous" if error.ambiguous else "failed",
                        error_type=error.error_type,
                        status_code=error.status_code,
                        fence=state.lease.fence,
                    )
                    failures.append({
                        "route": route.name,
                        "attempt": attempt_number,
                        "error_type": error.error_type,
                        "status_code": error.status_code,
                    })
                    if error.ambiguous:
                        state.warnings.extend(runtime.observability.publish(
                            "provider.ambiguous_failure",
                            trace_id=state.trace_id,
                            session_id=state.session_id,
                            turn_id=state.turn_id,
                            phase="provider_attempts",
                            payload={
                                "route": route.name,
                                "error_type": error.error_type,
                            },
                        ))
                    if error.retry_after is not None or error.status_code == 429:
                        route_state.set_cooldown(min(
                            error.retry_after or self.config.retry.base_delay_seconds,
                            self.config.retry.max_retry_after_seconds,
                        ))
                    if error.retryable and attempt_number < max_attempts:
                        delay = self._retry_delay(error, attempt_number - 1)
                        state.warnings.extend(runtime.observability.publish(
                            "provider.retry_scheduled", trace_id=state.trace_id,
                            session_id=state.session_id, turn_id=state.turn_id,
                            phase="provider_attempts",
                            payload={"route": route.name, "delay": delay},
                        ))
                        self._wait(runtime, state, delay)
                        continue
                    if error.allow_fallback:
                        break
                    raise ProviderRequestAborted(route.name, error) from exc
                else:
                    self.store.finish_model_attempt(
                        attempt_id, state="succeeded",
                        error_type=None, status_code=None,
                        fence=state.lease.fence,
                    )
                    route_state.clear()
                    state.warnings.extend(runtime.observability.publish(
                        "provider.response", trace_id=state.trace_id,
                        session_id=state.session_id, turn_id=state.turn_id,
                        phase="provider_attempts",
                        payload={
                            "route": route.name,
                            "tool_call_count": len(normalized.tool_calls),
                        },
                    ))
                    return normalized, route_index
            if route_index + 1 < len(self.config.routes):
                state.warnings.extend(runtime.observability.publish(
                    "provider.fallback", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    phase="provider_attempts",
                    payload={
                        "from_route": route.name,
                        "to_route": self.config.routes[route_index + 1].name,
                    },
                ))
        raise ProviderExhausted(failures)

    def _run_tool_round(
        self, runtime: TurnRuntime, state: LoopState,
        response: NormalizedModelResponse,
    ) -> None:
        assert state.context is not None and state.lease is not None
        assistant = Message.assistant_tools(
            response.tool_calls, content=response.content,
            finish_reason=response.finish_reason,
        )
        staged = []
        for call in response.tool_calls:
            binding = state.context.toolset.get(call.name)
            staged.append(
                (call, binding.spec.side_effects if binding else "possible")
            )
        self.store.stage_tool_calls(
            state.session_id, state.turn_id, assistant, staged,
            fence=state.lease.fence,
        )
        for call in response.tool_calls:
            runtime.observability.publish(
                "tool.pending", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round", payload={"tool_name": call.name},
            )
        state.messages.append(assistant)
        state.new_messages.append(assistant)
        for tool_call in response.tool_calls:
            runtime.check_active(state)
            executed = self._execute_tool(runtime, state, tool_call)
            persisted = self.store.complete_tool_call(
                session_id=state.session_id, turn_id=state.turn_id,
                message=executed.message, state=executed.outcome.state,
                effect_disposition=executed.outcome.effect_disposition,
                error_type=executed.outcome.error_type,
                may_still_be_running=executed.outcome.may_still_be_running,
                fence=state.lease.fence,
            )
            if not persisted:
                raise StorageError(f"tool outcome already terminal: {tool_call.id}")
            state.messages.append(executed.message)
            state.new_messages.append(executed.message)
            state.tool_outcomes.append(executed.outcome)
            state.warnings.extend(runtime.observability.publish(
                f"tool.{executed.outcome.state}", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round",
                payload={
                    "tool_name": tool_call.name,
                    "effect": executed.outcome.effect_disposition,
                },
            ))
            if executed.interrupt_reason == "cancelled":
                raise TurnCancelled("turn cancelled during tool execution")
            if executed.interrupt_reason == "run_budget":
                raise RunBudgetExceeded("turn budget expired during tool execution")

    def _execute_tool(
        self, runtime: TurnRuntime, state: LoopState, tool_call: ToolCall,
    ) -> _ToolExecution:
        assert state.context is not None
        binding = state.context.toolset.get(tool_call.name)
        if binding is None:
            invocation = self._invocation(state, tool_call, {}, False)
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    "unknown_tool", f"Unknown tool: {tool_call.name}",
                    self.config.tool_result_max_chars,
                ),
                side_effects="possible", state_name="failed",
                effect="not_applied", error_type="unknown_tool",
            )
        spec = binding.spec
        try:
            raw_args = json.loads(tool_call.arguments or "{}")
            if not isinstance(raw_args, dict):
                raise ValueError("tool arguments must decode to an object")
            args_model = spec.args_model.model_validate(raw_args)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            invocation = self._invocation(
                state, tool_call, {}, spec.requires_approval
            )
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    "invalid_arguments", f"{type(exc).__name__}: {exc}",
                    self.config.tool_result_max_chars,
                ),
                side_effects=spec.side_effects, state_name="failed",
                effect="not_applied", error_type="invalid_arguments",
            )
        invocation = self._invocation(
            state, tool_call, args_model.model_dump(mode="json"),
            spec.requires_approval,
        )
        denied = self._authorize_tool(runtime, invocation, spec.requires_approval)
        if denied is not None:
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    denied[0], denied[1], self.config.tool_result_max_chars
                ),
                side_effects=spec.side_effects, state_name="denied",
                effect="not_applied", error_type=denied[0],
            )
        started = threading.Event()
        cancellation = CancellationToken(runtime.cancel_event)

        def invoke() -> Any:
            cancellation.raise_if_cancelled()
            assert state.lease is not None
            if not self.store.mark_tool_running(
                state.turn_id, tool_call.id, fence=state.lease.fence
            ):
                raise StorageError("tool execution was no longer pending")
            started.set()
            runtime.observability.publish(
                "tool.running", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round",
                payload={"tool_name": tool_call.name},
            )
            value = binding.handler(
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
            value = self.call_operation(
                invoke,
                timeout=spec.timeout_seconds or self.config.timeouts.tool_seconds,
                cancel_event=runtime.cancel_event, deadline=runtime.deadline,
            )
        except TurnCancelled:
            return self._unknown_or_not_executed(
                tool_call, invocation, spec.side_effects,
                started=started.is_set(), reason="cancelled",
            )
        except RunBudgetExceeded:
            result = self._unknown_or_not_executed(
                tool_call, invocation, spec.side_effects,
                started=started.is_set(), reason="run_budget",
            )
            if not started.is_set():
                raise
            return result
        except OperationTimeoutError:
            return self._unknown_or_not_executed(
                tool_call, invocation, spec.side_effects,
                started=started.is_set(), reason="tool_timeout",
            )
        except WorkerQueueFullError:
            return self._unknown_or_not_executed(
                tool_call, invocation, spec.side_effects,
                started=False, reason="executor_saturated",
            )
        except BaseException as exc:
            effect = (
                "none" if spec.side_effects == "none"
                else ("unknown" if started.is_set() else "not_applied")
            )
            return self._tool_result(
                tool_call, invocation,
                encode_tool_error(
                    "tool_execution_error", f"{type(exc).__name__}: {exc}",
                    self.config.tool_result_max_chars,
                ),
                side_effects=spec.side_effects,
                state_name="failed" if started.is_set() else "not_executed",
                effect=effect, error_type="tool_execution_error",
            )
        effect = "none" if spec.side_effects == "none" else "confirmed"
        return self._tool_result(
            tool_call, invocation,
            encode_tool_success(value, self.config.tool_result_max_chars),
            side_effects=spec.side_effects, state_name="succeeded",
            effect=effect, error_type=None,
        )

    def _authorize_tool(
        self, runtime: TurnRuntime, invocation: ToolInvocation,
        requires_approval: bool,
    ) -> tuple[str, str] | None:
        try:
            decision = self.call_operation(
                lambda: self.guard.check(invocation),
                timeout=self.config.timeouts.policy_seconds,
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
        if self.approver is None:
            return (
                "approval_required",
                "tool requires approval but no Approver is configured",
            )
        try:
            approval = self.call_operation(
                lambda: self.approver.approve(invocation),
                timeout=self.config.timeouts.policy_seconds,
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

    def _cleanup(self, runtime: TurnRuntime, state: LoopState) -> None:
        phases = (
            FunctionPhase(
                "repair_unresolved_tools", self._repair, check_active=False
            ),
            FunctionPhase("finalize_turn", self._finalize, check_active=False),
            FunctionPhase("end_turn", self._end_turn, check_active=False),
            FunctionPhase(
                "release_lease", self._release,
                self._validate_released, check_active=False,
            ),
        )
        for phase in phases:
            try:
                run_phase(phase, runtime, state)
            except BaseException as exc:
                state.warnings.append(
                    f"cleanup phase {phase.name} failed: {type(exc).__name__}: {exc}"
                )

    def _repair(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if state.lease is None or not state.admitted:
            return PhaseResult()
        recovery = self.store.repair_turn_tool_calls(
            state.session_id, state.turn_id, fence=state.lease.fence
        )
        state.warnings.extend(recovery.warnings)
        state.messages.extend(recovery.messages)
        state.new_messages.extend(recovery.messages)
        if recovery.messages:
            state.warnings.extend(runtime.observability.publish(
                "recovery.tool_pair_repaired", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="repair_unresolved_tools",
                payload={"repair_count": len(recovery.messages)},
            ))
        return PhaseResult()

    def _finalize(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if state.lease is None or not state.admitted or state.finalized:
            return PhaseResult()
        if state.error is None:
            state.error = TurnError(
                code=state.completion_reason,
                message=state.completion_reason.replace("_", " "),
                retryable=False,
            )
        committed = self.store.finish_turn(
            session_id=state.session_id, turn_id=state.turn_id,
            status=state.status, error=state.error,
            route_name=state.route_used, iterations=state.iteration,
            usage=state.usage, completion_reason=state.completion_reason,
            provider_attempts=state.provider_attempts,
            route_attempts=state.route_attempts,
            fence=state.lease.fence,
        )
        persisted = self.store.get_turn(state.turn_id)
        state.finalized = committed or bool(
            persisted and persisted["status"] != "running"
        )
        return PhaseResult()

    def _end_turn(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if not state.finalized:
            return PhaseResult()
        snapshot = TurnSnapshot(
            result=self._result(state),
            messages=tuple(
                message.model_dump(mode="json") for message in state.messages
            ),
        )
        operations = (
            (
                "memory updater",
                lambda: self.memory_updater.update(snapshot),
                self.config.timeouts.memory_seconds,
            ),
            (
                "review scheduler",
                lambda: self.review_scheduler.schedule(snapshot),
                self.config.timeouts.review_schedule_seconds,
            ),
        )
        for label, callback, timeout in operations:
            try:
                self.call_operation(
                    callback, timeout=timeout, cancel_event=None, deadline=None
                )
            except BaseException as exc:
                state.warnings.append(
                    f"{label} failed: {type(exc).__name__}: {exc}"
                )
        state.warnings.extend(runtime.observability.publish(
            "turn.finalized", trace_id=state.trace_id,
            session_id=state.session_id, turn_id=state.turn_id,
            payload={
                "status": state.status,
                "completion_reason": state.completion_reason,
            },
        ))
        return PhaseResult()

    def _release(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if state.lease is None:
            state.released = True
            return PhaseResult()
        self.heartbeat.unregister(state.lease)
        self.lease_store.release(state.lease)
        state.released = True
        state.warnings.extend(runtime.observability.publish(
            "lease.released", trace_id=state.trace_id,
            session_id=state.session_id, turn_id=state.turn_id,
            payload={"fencing_token": state.lease.fencing_token},
        ))
        return PhaseResult()

    @staticmethod
    def _validate_released(state: LoopState) -> None:
        if state.lease is not None and not state.released:
            raise RuntimeError("lease was not released")

    def _result(self, state: LoopState) -> TurnResult:
        if state.status != "completed" and state.error is None:
            state.error = TurnError(
                code=state.completion_reason,
                message=state.completion_reason.replace("_", " "),
                retryable=False,
            )
        outcomes = (
            self.store.get_tool_outcomes(state.turn_id)
            if state.admitted else state.tool_outcomes
        )
        return TurnResult(
            status=state.status, final_text=state.final_text,
            session_id=state.session_id, turn_id=state.turn_id,
            new_messages=state.new_messages, iterations=state.iteration,
            route_used=state.route_used, usage=state.usage,
            warnings=state.warnings, error=state.error,
            context_version=(
                state.context.context_version
                if state.admitted and state.context is not None else None
            ),
            completion_reason=state.completion_reason,
            tool_outcomes=outcomes,
            provider_attempts=state.provider_attempts,
            route_attempts=state.route_attempts,
        )

    def _wait(
        self, runtime: TurnRuntime, state: LoopState, delay: float
    ) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            runtime.check_active(state)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            runtime.cancel_event.wait(min(0.05, remaining))

    def _acquire_lease(
        self, runtime: TurnRuntime, state: LoopState,
    ):
        deadline = (
            time.monotonic() + self.config.lease.wait_timeout_seconds
        )
        first_attempt = True
        while True:
            runtime.check_active(state)
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not first_attempt:
                raise LeaseAcquireTimeout(
                    f"timed out waiting for session lease: {state.session_id}"
                )
            try:
                return self.lease_store.acquire(
                    state.session_id, self.owner_id, state.turn_id,
                    self.config.lease.ttl_seconds,
                    max(0.0, min(0.1, remaining)),
                )
            except LeaseAcquireTimeout:
                first_attempt = False
                continue

    def _retry_delay(
        self, error: NormalizedProviderError, attempt_index: int
    ) -> float:
        policy = self.config.retry
        backoff = min(
            policy.max_delay_seconds,
            policy.base_delay_seconds * (2**attempt_index),
        )
        if backoff and policy.jitter_ratio:
            spread = backoff * policy.jitter_ratio
            backoff = max(0.0, backoff + random.uniform(-spread, spread))
        if error.retry_after is not None:
            backoff = max(
                backoff,
                min(error.retry_after, policy.max_retry_after_seconds),
            )
        return min(
            backoff,
            max(policy.max_delay_seconds, policy.max_retry_after_seconds),
        )

    def _unknown_or_not_executed(
        self, call: ToolCall, invocation: ToolInvocation, side_effects: str,
        *, started: bool, reason: str,
    ) -> _ToolExecution:
        if started:
            return self._tool_result(
                call, invocation,
                encode_tool_error(
                    "outcome_unknown",
                    "Tool execution stopped being observed; its outcome is unknown.",
                    self.config.tool_result_max_chars,
                    may_still_be_running=True,
                ),
                side_effects=side_effects, state_name="unknown",
                effect="none" if side_effects == "none" else "unknown",
                error_type=reason, may_still_be_running=True,
                interrupt_reason=(
                    reason if reason in {"cancelled", "run_budget"} else None
                ),
            )
        return self._tool_result(
            call, invocation,
            encode_tool_error(
                "not_executed", "Tool handler was not entered.",
                self.config.tool_result_max_chars,
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
        side_effects: str, state_name: str, effect: str,
        error_type: str | None, may_still_be_running: bool = False,
        interrupt_reason: str | None = None,
    ) -> _ToolExecution:
        outcome = ToolOutcome(
            tool_call_id=call.id, tool_name=call.name,
            side_effects=side_effects, state=state_name,
            effect_disposition=effect, error_type=error_type,
            may_still_be_running=may_still_be_running,
        )
        return _ToolExecution(
            message=Message.tool_result(
                content, tool_call_id=call.id, tool_name=call.name,
                effect_disposition=effect,
                metadata={
                    "execution_state": state_name,
                    "may_still_be_running": may_still_be_running,
                },
            ),
            outcome=outcome, invocation=invocation,
            interrupt_reason=interrupt_reason,
        )

    @staticmethod
    def _set_error(
        state: LoopState, status: str, reason: str,
        exc: BaseException, retryable: bool,
    ) -> None:
        state.status = status
        state.completion_reason = reason
        state.error = TurnError(
            code=reason, message=f"{type(exc).__name__}: {exc}",
            retryable=retryable,
        )


__all__ = [
    "ConversationLoop",
    "ProviderExhausted",
    "ProviderRequestAborted",
    "RouteState",
]
