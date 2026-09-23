from __future__ import annotations

import inspect
import json
import logging
import random
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from .interfaces import (
    Approver, CancellationToken, MemoryUpdater, ReviewScheduler, ToolContext,
    ToolGuard, ToolInvocation, TurnCancelled, TurnSnapshot,
)
from .lease import LeaseAcquireTimeout, LeaseHeartbeatService, TurnLeaseStore
from log.logging_system import log_context
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
from tools.tools import encode_tool_error, encode_tool_success
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
        recovery: bool = False, delivery_destination: str | None = None,
        max_recovery_attempts: int = 3,
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
        self.recovery = recovery
        self.delivery_destination = delivery_destination
        self.max_recovery_attempts = max_recovery_attempts

    def run(self) -> TurnResult:
        with log_context(self.config, self.session_id):
            return self._run()

    def _run(self) -> TurnResult:
        budget = self.config.timeouts.run_budget_seconds
        deadline = time.monotonic() + budget if budget is not None else None
        state = LoopState(
            session_id=self.session_id, turn_id=self.turn_id,
            trace_id=self.turn_id,
        )
        runtime = TurnRuntime(
            config=self.config, store=self.store, lease_store=self.lease_store,
            observability=self.observability, cancel_event=self.cancel_event,
            lease_lost_event=threading.Event(), deadline=deadline,
            call_operation=self.call_operation,
        )
        logger.info('turn.started')
        if not self.recovery:
            logger.debug(
                'turn.user_preview %s',
                self.user_input.text[:80].replace('\r', ' ').replace('\n', ' '),
            )
        try:
            for phase in (
                self._acquire_lease_phase(),
                self._recover_history_phase(),
                self._resolve_context_phase(),
                self._establish_turn_phase(),
                self._load_messages_phase(),
            ):
                self._run_fallthrough_phase(phase, runtime, state)
            self._run_iterations(runtime, state)
        except SessionSnapshotConflict as exc:
            self._set_error(state, "failed", "session_snapshot_conflict", exc, False)
        except ToolsetRebindError as exc:
            logger.warning('session.rebind_failed error_type=%s', type(exc).__name__)
            state.warnings.extend(runtime.observability.publish(
                "session.rebind_failed", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"error_type": type(exc).__name__},
            ))
            self._set_error(state, "failed", "toolset_rebind_failed", exc, False)
        except LeaseAcquireTimeout as exc:
            self._set_error(state, "failed", "lease_timeout", exc, True)
        except LeaseLostError as exc:
            logger.error('lease.fenced_write_rejected error_type=%s', type(exc).__name__)
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
            logger.error(
                'turn.unhandled_error error_type=%s validation_errors=%s',
                type(exc).__name__,
                self._validation_error_details(exc),
                exc_info=True,
            )
            state.warnings.extend(runtime.observability.publish(
                "turn.unhandled_error", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"error_type": type(exc).__name__},
            ))
            self._set_error(state, "failed", "turn_failed", exc, False)
        finally:
            if state.error is not None and not state.finalized:
                try:
                    self._run_fallthrough_phase(
                        self._handle_error_phase(), runtime, state
                    )
                except BaseException as exc:
                    state.warnings.append(
                        "handle_turn_error phase failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            self._cleanup(runtime, state)
            end_logger = logger.info if state.status == "completed" else logger.error
            end_logger(
                'turn.ended status=%s completion_reason=%s duration_ms=%s iteration=%s provider_attempts=%s tool_call_count=%s',
                state.status,
                state.completion_reason,
                int((time.monotonic() - state.started_at_monotonic) * 1000),
                state.iteration,
                state.provider_attempts,
                len(state.tool_outcomes),
            )
        return self._result(state)

    @staticmethod
    def _run_fallthrough_phase(
        phase: FunctionPhase, runtime: TurnRuntime, state: LoopState,
    ) -> None:
        action = run_phase(phase, runtime, state).action
        if action is not PhaseAction.FALLTHROUGH:
            raise RuntimeError(
                f"{phase.name} returned unexpected action {action.value}; "
                "expected fallthrough"
            )

    def _run_iterations(self, runtime: TurnRuntime, state: LoopState) -> None:
        for iteration in range(1, self.config.max_iterations + 1):
            state.iteration = iteration
            state.response = None
            state.assistant_message = None
            self._run_fallthrough_phase(self._assemble_phase(), runtime, state)
            self._run_fallthrough_phase(self._provider_phase(), runtime, state)
            response = state.require_response("response_dispatch")
            phase = (
                self._tool_round_phase()
                if response.tool_calls else self._finish_text_phase()
            )
            action = run_phase(phase, runtime, state).action
            if action is PhaseAction.CONTINUE:
                continue
            if action is PhaseAction.BREAK:
                break
            if action is PhaseAction.RETURN:
                return
            raise RuntimeError(
                f"{phase.name} returned unexpected action {action.value}; "
                "expected break, return, or continue"
            )
        else:
            state.status = "failed"
            state.completion_reason = "max_iterations"
            state.error = TurnError(
                code="max_iterations",
                message=f"agent exceeded {self.config.max_iterations} model iterations",
                retryable=False,
            )

    def _handle_error_phase(self) -> FunctionPhase:
        def handle(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.status not in {"failed", "interrupted"} or state.error is None:
                raise RuntimeError("error phase requires a non-completed structured error")

        return FunctionPhase(
            "handle_turn_error", handle, validate, check_active=False
        )

    def _acquire_lease_phase(self) -> FunctionPhase:
        def acquire(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            logger.debug('lease.wait_started')
            state.warnings.extend(runtime.observability.publish(
                "lease.wait_started", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
            ))
            lease = self._acquire_lease(runtime, state)
            state.lease = lease
            self.heartbeat.register(lease, runtime.lease_lost_event)
            logger.info('lease.acquired fencing_token=%s', lease.fencing_token)
            state.warnings.extend(runtime.observability.publish(
                "lease.acquired", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"fencing_token": lease.fencing_token},
            ))
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.lease is None:
                raise RuntimeError("lease phase did not acquire a session lease")

        return FunctionPhase(
            "acquire_lease", acquire, validate, check_active=False
        )

    def _recover_history_phase(self) -> FunctionPhase:
        def recover(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            lease = state.require_lease('recover_history')
            if self.recovery:
                state.recovery_claimed = self.store.claim_recovery(
                    state.session_id, fence=lease.fence,
                    max_attempts=self.max_recovery_attempts,
                )
                if not state.recovery_claimed:
                    raise StorageError(
                        "session is not eligible for internal recovery"
                    )
                logger.info('recovery.started')
                state.warnings.extend(runtime.observability.publish(
                    "recovery.started", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                ))
            else:
                lifecycle = self.store.get_session_lifecycle(state.session_id)
                state.user_driven_recovery = bool(
                    lifecycle and lifecycle["resume_pending"]
                )
            repaired = self.store.recover_incomplete_turns(
                state.session_id, fence=lease.fence
            )
            state.warnings.extend(repaired.warnings)
            if repaired.warnings:
                logger.warning('recovery.incomplete_turn_found warning_count=%s', len(repaired.warnings))
                state.warnings.extend(runtime.observability.publish(
                    "recovery.incomplete_turn_found", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    payload={"warning_count": len(repaired.warnings)},
                ))
            if repaired.messages:
                logger.info('recovery.tool_pair_repaired repair_count=%s', len(repaired.messages))
                state.warnings.extend(runtime.observability.publish(
                    "recovery.tool_pair_repaired", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    payload={"repair_count": len(repaired.messages)},
                ))
            if repaired.warnings or repaired.messages:
                logger.info('recovery.history_repaired')
                state.warnings.extend(runtime.observability.publish(
                    "recovery.history_repaired", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                ))
            state.history_recovered = True
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if not state.history_recovered:
                raise RuntimeError("prior turn history was not repaired")

        return FunctionPhase(
            "recover_history", recover, validate, check_active=False
        )

    def _resolve_context_phase(self) -> FunctionPhase:
        def resolve(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            lease = state.require_lease('resolve_session_context')
            resolution = (
                self.session_contexts.load_existing(
                    state.session_id, lease
                )
                if self.recovery else
                self.session_contexts.create_or_load(
                    state.session_id, self.user_input, self.session_request,
                    lease,
                )
            )
            state.context = resolution.snapshot
            state.session_created = resolution.created
            # A new Session's context, turn, and user message were committed
            # together by SessionContextFactory.
            if resolution.created:
                state.admitted = True
            state.warnings.extend(resolution.warnings)
            if any("defaults differ" in item for item in resolution.warnings):
                logger.warning('session.config_mismatch_deferred')
                state.warnings.extend(runtime.observability.publish(
                    "session.config_mismatch_deferred",
                    trace_id=state.trace_id, session_id=state.session_id,
                    turn_id=state.turn_id,
                ))
            session_event = "session.created" if resolution.created else "session.loaded"
            logger.info('%s context_version=%s', session_event, resolution.snapshot.context_version)
            state.warnings.extend(runtime.observability.publish(
                "session.created" if resolution.created else "session.loaded",
                trace_id=state.trace_id, session_id=state.session_id,
                turn_id=state.turn_id,
                payload={
                    "context_version": resolution.snapshot.context_version
                },
            ))
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if state.context is None:
                raise RuntimeError("session context was not resolved")

        return FunctionPhase(
            "resolve_session_context", resolve, validate,
            check_active=False,
        )

    def _establish_turn_phase(self) -> FunctionPhase:
        def begin(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            lease = state.require_lease('establish_turn')
            context = state.require_context('establish_turn')
            if self.recovery:
                message = self.store.begin_recovery_turn(
                    turn_id=state.turn_id, session_id=state.session_id,
                    context_version=context.context_version,
                    fence=lease.fence,
                )
            else:
                message = Message.user(
                    self.user_input.text, metadata=self.user_input.metadata
                )
                if not state.session_created:
                    self.store.begin_turn(
                        turn_id=state.turn_id, session_id=state.session_id,
                        context_version=context.context_version,
                        user_message=message, fence=lease.fence,
                    )
            state.submitted_message = message
            state.new_messages.append(message)
            state.admitted = True
            logger.info('turn.admitted context_version=%s', context.context_version)
            state.warnings.extend(runtime.observability.publish(
                "turn.admitted", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"context_version": context.context_version},
            ))
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if not state.admitted or state.submitted_message is None:
                raise RuntimeError("current turn was not admitted")

        return FunctionPhase(
            "establish_turn", begin, validate, check_active=False
        )

    def _load_messages_phase(self) -> FunctionPhase:
        def load(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            state.messages = self.store.load_messages(state.session_id)
            return PhaseResult()

        def validate(state: LoopState) -> None:
            if (
                state.submitted_message is None or not state.messages
                or state.messages[-1] != state.submitted_message
            ):
                raise RuntimeError(
                    "loaded history does not end with the submitted message"
                )

        return FunctionPhase(
            "load_messages", load, validate, check_active=False
        )

    def _assemble_phase(self) -> FunctionPhase:
        def assemble(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            context = state.require_context('assemble_request')
            state.request = CanonicalModelRequest.build(
                system_prompt=context.prompt.text,
                messages=tuple(state.messages),
                tool_schema_bytes=context.toolset.schema_bytes,
            )
            logger.debug(
                'turn.request_assembled phase=%s iteration=%s message_count=%s tool_count=%s context_version=%s',
                'assemble_request',
                state.iteration,
                len(state.request.messages),
                len(context.toolset.bindings),
                context.context_version,
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
            response = state.require_response('tool_round')
            if not response.tool_calls:
                raise RuntimeError('tool_round requires response with tool_calls')
            self._run_tool_round(runtime, state, response)
            return PhaseResult(PhaseAction.CONTINUE)

        def validate(state: LoopState) -> None:
            response = state.require_response('tool_round')
            wanted = {call.id for call in response.tool_calls}
            actual = {
                message.tool_call_id for message in state.new_messages
                if message.role == "tool"
            }
            if not wanted.issubset(actual):
                raise RuntimeError("tool round left unresolved tool calls")

        return FunctionPhase("tool_round", execute, validate)

    def _finish_text_phase(self) -> FunctionPhase:
        def finish(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            response = state.require_response('finish_text')
            lease = state.require_lease('finish_text')
            if response.tool_calls:
                raise RuntimeError('finish_text requires a text response')
            text = response.content or ""
            assistant = Message.assistant_text(
                text, finish_reason=response.finish_reason
            )
            if response.finish_reason in {"length", "content_filter"}:
                state.status = "failed"
                state.completion_reason = "incomplete_response"
                state.error = TurnError(
                    code="incomplete_response",
                    message=f"model stopped with finish_reason={response.finish_reason}",
                    retryable=response.finish_reason == "length",
                )
            else:
                state.status = "completed"
                state.completion_reason = "model_final"
                state.error = None
            committed = self.store.finish_turn_with_message(
                session_id=state.session_id, turn_id=state.turn_id,
                message=assistant, status=state.status, error=state.error,
                route_name=state.route_used, iterations=state.iteration,
                usage=state.usage, completion_reason=state.completion_reason,
                provider_attempts=state.provider_attempts,
                route_attempts=state.route_attempts, fence=lease.fence,
                delivery_destination=self.delivery_destination,
                recovery=self.recovery or state.user_driven_recovery,
            )
            if not committed:
                raise StorageError("terminal turn was already finalized")
            state.assistant_message = assistant
            state.final_text = text
            state.messages.append(assistant)
            state.new_messages.append(assistant)
            state.finalized = True
            return PhaseResult(PhaseAction.BREAK)

        def validate(state: LoopState) -> None:
            if state.response is None or state.response.tool_calls:
                raise RuntimeError("finish_text requires a text response")
            if not state.finalized:
                raise RuntimeError("terminal assistant was not atomically finalized")

        return FunctionPhase("finish_text", finish, validate)

    def _run_provider_attempts(
        self, runtime: TurnRuntime, state: LoopState,
    ) -> tuple[NormalizedModelResponse, int]:
        request = state.require_request('provider_attempts')
        lease = state.require_lease('provider_attempts')
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
                    request_fingerprint=request.fingerprint,
                    started_at=time.time(),
                )
                state.provider_attempts += 1
                state.route_attempts[route.name] = (
                    state.route_attempts.get(route.name, 0) + 1
                )
                self.store.begin_model_attempt(
                    attempt_id=attempt_id, turn_id=state.turn_id,
                    session_id=state.session_id, iteration=state.iteration,
                    route_index=route_index, route_name=route.name,
                    attempt_number=attempt_number,
                    request_fingerprint=request.fingerprint,
                    fence=lease.fence,
                )
                logger.debug(
                    'provider.attempt_started phase=%s route=%s iteration=%s attempt=%s',
                    'provider_attempts',
                    route.name,
                    state.iteration,
                    attempt_number,
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
                attempt_started = time.monotonic()
                try:
                    wire = adapter.encode(request, binding)
                    request_messages = self._provider_request_message_details(
                        wire.body
                    )
                    request_payload = {
                        "route": route.name,
                        "provider": binding.provider,
                        "model": binding.model,
                        "api_mode": binding.api_mode,
                        "iteration": state.iteration,
                        "attempt": attempt_number,
                        "request_fingerprint": request.fingerprint,
                        "timeout_seconds": wire.timeout_seconds,
                        "message_count": len(request_messages),
                        "messages": [
                            {
                                key: value for key, value in item.items()
                                if key != "content_preview"
                            }
                            for item in request_messages
                        ],
                        "tool_count": len(wire.body.get("tools") or []),
                    }
                    logger.info(
                        'provider.request phase=%s payload=%s',
                        'provider_attempts',
                        request_payload,
                    )
                    logger.debug(
                        'provider.request_messages phase=%s payload=%s',
                        'provider_attempts',
                        request_messages,
                    )
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
                    logger.debug(
                        'provider.response raw_response=%s',
                        raw_response.raw,
                    )
                    normalized = adapter.decode(raw_response, binding)
                except (TurnCancelled, RunBudgetExceeded, LeaseLostError):
                    self.store.finish_model_attempt(
                        attempt_id, state="ambiguous",
                        error_type="interrupted", status_code=None,
                        fence=lease.fence,
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
                        fence=lease.fence,
                    )
                    failure_payload = {
                        "route": route.name, "attempt": attempt_number,
                        "error_type": error.error_type,
                        "status_code": error.status_code,
                        "duration_ms": int(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    }
                    logger.warning(
                        'provider.attempt_failed phase=%s payload=%s',
                        'provider_attempts',
                        failure_payload,
                    )
                    state.warnings.extend(runtime.observability.publish(
                        "provider.attempt_failed", trace_id=state.trace_id,
                        session_id=state.session_id, turn_id=state.turn_id,
                        phase="provider_attempts",
                        payload={
                            "route": route.name, "attempt": attempt_number,
                            "error_type": error.error_type,
                            "status_code": error.status_code,
                            "duration_ms": int(
                                (time.monotonic() - attempt_started) * 1000
                            ),
                        },
                    ))
                    failures.append({
                        "route": route.name,
                        "attempt": attempt_number,
                        "error_type": error.error_type,
                        "status_code": error.status_code,
                    })
                    if error.ambiguous:
                        logger.warning(
                            'provider.ambiguous_failure phase=%s route=%s error_type=%s',
                            'provider_attempts',
                            route.name,
                            error.error_type,
                        )
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
                        logger.warning(
                            'provider.retry_scheduled phase=%s route=%s attempt=%s delay=%s',
                            'provider_attempts',
                            route.name,
                            attempt_number,
                            delay,
                        )
                        state.warnings.extend(runtime.observability.publish(
                            "provider.retry_scheduled", trace_id=state.trace_id,
                            session_id=state.session_id, turn_id=state.turn_id,
                            phase="provider_attempts",
                            payload={"route": route.name, "attempt": attempt_number, "delay": delay},
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
                        fence=lease.fence,
                    )
                    route_state.clear()
                    response_content = normalized.content
                    response_content_preview = (
                        None if response_content is None else
                        response_content[:1000]
                        .replace('\r', '\\r')
                        .replace('\n', '\\n')
                    )
                    response_payload = {
                        "route": route.name,
                        "tool_call_count": len(normalized.tool_calls),
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments_length": len(call.arguments),
                            }
                            for call in normalized.tool_calls
                        ],
                        "content_type": type(response_content).__name__,
                        "content_length": (
                            len(response_content)
                            if response_content is not None else 0
                        ),
                        "content_truncated": bool(
                            response_content is not None
                            and len(response_content) > 1000
                        ),
                        "content_preview": response_content_preview,
                        "finish_reason": (
                            normalized.finish_reason
                            if normalized.finish_reason in {
                                "stop", "length", "tool_calls", "content_filter"
                            } else "other"
                        ),
                        "duration_ms": int(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    }
                    logger.info(
                        'provider.response phase=%s payload=%s',
                        'provider_attempts',
                        response_payload,
                    )
                    state.warnings.extend(runtime.observability.publish(
                        "provider.response", trace_id=state.trace_id,
                        session_id=state.session_id, turn_id=state.turn_id,
                        phase="provider_attempts",
                        payload={
                            "route": route.name,
                            "tool_call_count": len(normalized.tool_calls),
                            "finish_reason": (
                                normalized.finish_reason
                                if normalized.finish_reason in {
                                    "stop", "length", "tool_calls", "content_filter"
                                } else "other"
                            ),
                            "duration_ms": int(
                                (time.monotonic() - attempt_started) * 1000
                            ),
                        },
                    ))
                    return normalized, route_index
            if route_index + 1 < len(self.config.routes):
                logger.warning(
                    'provider.fallback phase=%s from_route=%s to_route=%s',
                    'provider_attempts',
                    route.name,
                    self.config.routes[route_index + 1].name,
                )
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
        context = state.require_context('tool_round')
        lease = state.require_lease('tool_round')
        call_ids = [call.id for call in response.tool_calls]
        logger.info(
            'tool.round_preparing phase=%s iteration=%s tool_call_count=%s '
            'tool_names=%s finish_reason=%s response_content_present=%s '
            'response_content_length=%s duplicate_call_id_count=%s',
            'tool_round',
            state.iteration,
            len(response.tool_calls),
            [call.name for call in response.tool_calls],
            response.finish_reason,
            response.content is not None,
            len(response.content) if response.content is not None else 0,
            len(call_ids) - len(set(call_ids)),
        )
        try:
            assistant = Message.assistant_tools(
                response.tool_calls, content=response.content,
                finish_reason=response.finish_reason,
            )
        except ValidationError as exc:
            logger.error(
                'tool.message_validation_failed phase=%s iteration=%s '
                'tool_call_count=%s response_content_present=%s '
                'response_content_length=%s validation_errors=%s',
                'tool_round',
                state.iteration,
                len(response.tool_calls),
                response.content is not None,
                len(response.content) if response.content is not None else 0,
                self._validation_error_details(exc),
            )
            raise
        staged = []
        for call in response.tool_calls:
            binding = context.toolset.get(call.name)
            staged.append(
                (call, binding.spec.side_effects if binding else "possible")
            )
        try:
            self.store.stage_tool_calls(
                state.session_id, state.turn_id, assistant, staged,
                fence=lease.fence,
            )
        except BaseException as exc:
            logger.error(
                'tool.stage_failed phase=%s iteration=%s tool_call_count=%s '
                'error_type=%s',
                'tool_round',
                state.iteration,
                len(response.tool_calls),
                type(exc).__name__,
            )
            raise
        round_started = time.monotonic()
        outcome_start = len(state.tool_outcomes)
        logger.info(
            'tool.round_started phase=%s iteration=%s tool_call_count=%s',
            'tool_round',
            state.iteration,
            len(response.tool_calls),
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
            logger.debug(
                'tool.arguments_preview tool_name=%s preview=%s',
                tool_call.name,
                tool_call.arguments[:200].replace('\r', ' ').replace('\n', ' '),
            )
            tool_started = time.monotonic()
            executed = self._execute_tool(runtime, state, tool_call)
            tool_duration_ms = int((time.monotonic() - tool_started) * 1000)
            persisted = self.store.complete_tool_call(
                session_id=state.session_id, turn_id=state.turn_id,
                message=executed.message, state=executed.outcome.state,
                effect_disposition=executed.outcome.effect_disposition,
                error_type=executed.outcome.error_type,
                may_still_be_running=executed.outcome.may_still_be_running,
                fence=lease.fence,
            )
            if not persisted:
                raise StorageError(f"tool outcome already terminal: {tool_call.id}")
            state.messages.append(executed.message)
            state.new_messages.append(executed.message)
            state.tool_outcomes.append(executed.outcome)
            tool_event = f"tool.{executed.outcome.state}"
            tool_logger = (
                logger.info if executed.outcome.state == "succeeded"
                else logger.warning
            )
            tool_logger(
                '%s phase=%s tool_name=%s effect=%s duration_ms=%s error_type=%s',
                tool_event,
                'tool_round',
                tool_call.name,
                executed.outcome.effect_disposition,
                tool_duration_ms,
                executed.outcome.error_type,
            )
            state.warnings.extend(runtime.observability.publish(
                f"tool.{executed.outcome.state}", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round",
                payload={
                    "tool_name": tool_call.name,
                    "effect": executed.outcome.effect_disposition,
                    "duration_ms": tool_duration_ms,
                    "error_type": executed.outcome.error_type,
                },
            ))
            if executed.interrupt_reason == "cancelled":
                raise TurnCancelled("turn cancelled during tool execution")
            if executed.interrupt_reason == "run_budget":
                raise RunBudgetExceeded("turn budget expired during tool execution")
        round_outcomes = state.tool_outcomes[outcome_start:]
        failed_count = sum(
            outcome.state != "succeeded" for outcome in round_outcomes
        )
        round_logger = logger.warning if failed_count else logger.info
        round_logger(
            'tool.round_completed phase=%s iteration=%s tool_call_count=%s failed_count=%s duration_ms=%s',
            'tool_round',
            state.iteration,
            len(round_outcomes),
            failed_count,
            int((time.monotonic() - round_started) * 1000),
        )

    def _execute_tool(
        self, runtime: TurnRuntime, state: LoopState, tool_call: ToolCall,
    ) -> _ToolExecution:
        context = state.require_context('execute_tool')
        lease = state.require_lease('execute_tool')
        binding = context.toolset.get(tool_call.name)
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
            if not self.store.mark_tool_running(
                state.turn_id, tool_call.id, fence=lease.fence
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
            FunctionPhase(
                "clear_active_turn", self._clear_active_turn, check_active=False
            ),
            FunctionPhase(
                "record_recovery_failure", self._record_recovery_failure,
                check_active=False,
            ),
            FunctionPhase("end_turn", self._end_turn, check_active=False),
            FunctionPhase(
                "release_lease", self._release,
                self._validate_released, check_active=False,
            ),
        )
        for phase in phases:
            try:
                self._run_fallthrough_phase(phase, runtime, state)
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
            logger.info(
                'recovery.tool_pair_repaired phase=%s repair_count=%s',
                'repair_unresolved_tools',
                len(recovery.messages),
            )
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

    def _clear_active_turn(
        self, runtime: TurnRuntime, state: LoopState
    ) -> PhaseResult:
        if (
            not self.recovery and state.lease is not None
            and state.admitted and state.finalized
        ):
            self.store.clear_active_turn(
                state.session_id, state.turn_id, fence=state.lease.fence
            )
        return PhaseResult()

    def _record_recovery_failure(
        self, runtime: TurnRuntime, state: LoopState
    ) -> PhaseResult:
        if (
            self.recovery and state.recovery_claimed
            and state.status != "completed" and state.lease is not None
        ):
            self.store.finish_recovery(
                state.session_id, completed=False,
                max_attempts=self.max_recovery_attempts,
                fence=state.lease.fence,
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
        if self.recovery:
            recovery_event = (
                "recovery.completed" if state.status == "completed"
                else "recovery.failed"
            )
            recovery_logger = (
                logger.info if state.status == "completed" else logger.error
            )
            recovery_logger('%s status=%s', recovery_event, state.status)
            state.warnings.extend(runtime.observability.publish(
                "recovery.completed" if state.status == "completed"
                else "recovery.failed",
                trace_id=state.trace_id, session_id=state.session_id,
                turn_id=state.turn_id,
                payload={"status": state.status},
            ))
        final_logger = logger.info if state.status == "completed" else logger.error
        final_logger(
            'turn.finalized status=%s completion_reason=%s duration_ms=%s',
            state.status,
            state.completion_reason,
            int((time.monotonic() - state.started_at_monotonic) * 1000),
        )
        state.warnings.extend(runtime.observability.publish(
            "turn.finalized", trace_id=state.trace_id,
            session_id=state.session_id, turn_id=state.turn_id,
            payload={
                "status": state.status,
                "completion_reason": state.completion_reason,
                "duration_ms": int(
                    (time.monotonic() - state.started_at_monotonic) * 1000
                ),
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
        logger.debug('lease.released fencing_token=%s', state.lease.fencing_token)
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
    def _provider_request_message_details(
        body: Mapping[str, Any], *, preview_chars: int = 500,
    ) -> list[dict[str, Any]]:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return []
        details: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                details.append({
                    "index": index,
                    "role": "unknown",
                    "content_type": type(message).__name__,
                    "content_length": None,
                    "content_preview": None,
                    "tool_call_count": 0,
                    "tool_names": [],
                })
                continue
            content = message.get("content")
            if isinstance(content, str):
                content_length: int | None = len(content)
                content_preview: str | None = (
                    content[:preview_chars]
                    .replace('\r', '\\r')
                    .replace('\n', '\\n')
                )
            elif content is None:
                content_length = 0
                content_preview = None
            else:
                rendered = repr(content)
                content_length = len(rendered)
                content_preview = (
                    rendered[:preview_chars]
                    .replace('\r', '\\r')
                    .replace('\n', '\\n')
                )
            raw_tool_calls = message.get("tool_calls")
            tool_calls = (
                raw_tool_calls if isinstance(raw_tool_calls, list) else []
            )
            tool_names = []
            for call in tool_calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                if isinstance(function, Mapping):
                    tool_names.append(function.get("name"))
            details.append({
                "index": index,
                "role": message.get("role"),
                "content_type": type(content).__name__,
                "content_length": content_length,
                "content_preview": content_preview,
                "tool_call_count": len(tool_calls),
                "tool_names": tool_names,
                "tool_call_id": message.get("tool_call_id"),
            })
        return details

    @staticmethod
    def _validation_error_details(
        exc: BaseException,
    ) -> list[dict[str, Any]] | None:
        if not isinstance(exc, ValidationError):
            return None
        return [
            {
                "type": error.get("type"),
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg"),
            }
            for error in exc.errors(include_url=False, include_input=False)
        ]

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
