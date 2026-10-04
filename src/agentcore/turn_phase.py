from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Protocol

from pydantic import ValidationError
from knowledge.models import KnowledgeContext
from knowledge.planner import request_from_messages
from knowledge.provider import KnowledgeProvider, NoopKnowledgeProvider, render_knowledge
from tools.toolsets import ToolsetRequest

from .interfaces import (
    TurnHostContext, MemoryUpdater, ReviewScheduler, ToolGuard,
    TurnCancelled, TurnSnapshot,
)
from .lease import LeaseAcquireTimeout, LeaseScope
from .message_history import interrupted_tool_results
from .turn_persistence import TurnPersistence
from .turn_preparation import PreparedConversation, RecoveryNotEligible
from .models import (
    AgentConfig, CanonicalModelRequest, Message,
    ToolOutcome, TurnError, TurnResult, UserInput,
)
from .provider_runtime import (
    ApiModeRegistry, ProviderResolver,
)
from .provider_runner import (
    ProviderRunner, ProviderExhausted, ProviderRequestAborted, RouteState,
)
from .session_context import RecoverySessionMissing
from .runtime_cwd import WorkspaceValidationError
from .storage import LeaseLostError
from .tool_execution import ToolExecutor
from .tool_round import ToolRoundRunner
from .turn_state import (
    LoopState, RunBudgetExceeded,
    TurnRuntime,
)



logger = logging.getLogger(__name__)


class PhaseAction(str, Enum):
    FALLTHROUGH = "fallthrough"
    BREAK = "break"
    RETURN = "return"
    CONTINUE = "continue"


@dataclass(frozen=True, slots=True)
class PhaseResult:
    action: PhaseAction = PhaseAction.FALLTHROUGH


class TurnPhase(Protocol):
    name: str

    def __call__(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult: ...

    def validate_postconditions(
        self, state: LoopState, result: PhaseResult,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FunctionPhase:
    name: str
    function: Callable[[TurnRuntime, LoopState], PhaseResult]
    validator: Callable[[LoopState, PhaseResult], None] = lambda state, result: None
    check_active: bool = True
    expected_errors: tuple[type[BaseException], ...] = ()
    on_expected_error: Callable[
        [TurnRuntime, LoopState, BaseException], PhaseResult
    ] | None = None
    cleanup_phase: bool = False

    def __call__(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        try:
            if self.check_active:
                runtime.check_active()
            return self.function(runtime, state)
        except self.expected_errors as exc:
            if self.on_expected_error is None:
                raise
            return self.on_expected_error(runtime, state, exc)

    def validate_postconditions(
        self, state: LoopState, result: PhaseResult,
    ) -> None:
        if result.action is PhaseAction.BREAK:
            if self.cleanup_phase:
                return
            if state.error is not None:
                if state.status not in {"failed", "interrupted"}:
                    raise RuntimeError("terminal phase error requires failed or interrupted status")
                return
            if state.status != "completed":
                raise RuntimeError("BREAK requires a structured terminal outcome")
        self.validator(state, result)


def run_phase(
    phase: TurnPhase, runtime: TurnRuntime, state: LoopState,
) -> PhaseResult:
    logger.debug('turn.phase_started phase=%s iteration=%s', phase.name, state.iteration)
    state.warnings.extend(runtime.observability.publish(
        "turn.phase_started", trace_id=state.trace_id,
        session_id=state.session_id, turn_id=state.turn_id,
        phase=phase.name, payload={"iteration": state.iteration},
    ))
    try:
        result = phase(runtime, state)
        phase.validate_postconditions(state, result)
    except BaseException as exc:
        logger.error('turn.phase_failed phase=%s error_type=%s', phase.name, type(exc).__name__)
        state.warnings.extend(runtime.observability.publish(
            "turn.phase_failed", trace_id=state.trace_id,
            session_id=state.session_id, turn_id=state.turn_id,
            phase=phase.name,
            payload={"error_type": type(exc).__name__, "iteration": state.iteration},
        ))
        if type(exc).__name__ in {
            "StorageError", "DatabaseError", "OperationalError", "IntegrityError",
        }:
            logger.error('storage.failure phase=%s error_type=%s', phase.name, type(exc).__name__)
            state.warnings.extend(runtime.observability.publish(
                "storage.failure", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase=phase.name,
                payload={"error_type": type(exc).__name__},
            ))
        raise
    logger.debug('turn.phase_finished phase=%s action=%s', phase.name, result.action.value)
    state.warnings.extend(runtime.observability.publish(
        "turn.phase_finished", trace_id=state.trace_id,
        session_id=state.session_id, turn_id=state.turn_id,
        phase=phase.name, payload={"action": result.action.value, "iteration": state.iteration},
    ))
    return result


_INTERRUPTION_ERRORS = (TurnCancelled, RunBudgetExceeded, LeaseLostError)


class TurnPhases:
    """Concrete synchronous turn phases and their execution helpers."""

    def __init__(
        self, *, config: AgentConfig, user_input: UserInput,
        session_id: str, turn_id: str,
        cancel_event: threading.Event, persistence: TurnPersistence,
        lease_scope: LeaseScope | None,
        prepare: Callable[[TurnRuntime], PreparedConversation],
        recovery: bool, delivery_destination: str | None, max_recovery_attempts: int,
        provider_resolver: ProviderResolver,
        api_modes: ApiModeRegistry, route_states: tuple[RouteState, ...],
        guard: ToolGuard, host_context: TurnHostContext,
        memory_updater: MemoryUpdater, review_scheduler: ReviewScheduler,
        observability: Any, call_operation: Any,
        knowledge_provider: KnowledgeProvider | None = None,
    ) -> None:
        self.config = config
        self.on_output = host_context.on_output
        self.user_input = user_input
        self.session_id = session_id
        self.turn_id = turn_id
        self.cancel_event = cancel_event
        self.persistence = persistence
        self.lease_scope = lease_scope
        self.prepare = prepare
        self.delivery_destination = delivery_destination
        self.max_recovery_attempts = max_recovery_attempts
        self.provider_runner = ProviderRunner(
            config=config, provider_resolver=provider_resolver,
            api_modes=api_modes, route_states=route_states,
        )
        self.memory_updater = memory_updater
        self.review_scheduler = review_scheduler
        self.observability = observability
        self.call_operation = call_operation
        self.recovery = recovery
        self.knowledge_provider = knowledge_provider if knowledge_provider is not None else NoopKnowledgeProvider()
        self.host_context = host_context
        self.tool_round_runner = ToolRoundRunner(
            persistence=persistence,
            executor=ToolExecutor(
                config=config, guard=guard,
                host_context=host_context,
            ),
        )

    def turn_started(self) -> None:
        logger.info('turn.started')
        if not self.recovery:
            logger.debug(
                'turn.user_preview %s',
                '[redacted]',
            )

    def turn_ended(self, state: LoopState) -> None:
        end_logger = logger.info if state.status == "completed" else logger.error
        end_logger(
            'turn.ended status=%s completion_reason=%s duration_ms=%s iteration=%s provider_attempts=%s tool_call_count=%s',
            state.status, state.completion_reason,
            int((time.monotonic() - state.started_at_monotonic) * 1000),
            state.iteration, state.provider_attempts, len(state.tool_outcomes),
        )

    def setup_phases(self) -> tuple[FunctionPhase, ...]:
        return (self._acquire_execution_phase(), self._prepare_conversation_phase(), self._prepare_knowledge_phase())

    def assemble_phase(self) -> FunctionPhase:
        return self._assemble_phase()

    def provider_phase(self) -> FunctionPhase:
        return self._provider_phase()

    def response_phase(self, state: LoopState) -> FunctionPhase:
        response = state.require_response("response_dispatch")
        return self._tool_round_phase() if response.tool_calls else self._finish_text_phase()

    def max_iterations(self, state: LoopState) -> PhaseResult:
        state.status = "failed"
        state.completion_reason = "max_iterations"
        state.error = TurnError(
            code="max_iterations",
            message=f"agent exceeded {self.config.max_iterations} model iterations",
            retryable=False,
        )
        return PhaseResult(PhaseAction.BREAK)

    def handle_outer_loop_error(
        self, runtime: TurnRuntime, state: LoopState, exc: BaseException,
    ) -> PhaseResult:
        if state.finalized and state.status == "completed":
            state.warnings.append(
                f"post-commit error: {type(exc).__name__}: {exc}"
            )
            return PhaseResult(PhaseAction.BREAK)
        if state.error is not None:
            state.warnings.append(
                f"subsequent outer-loop error: {type(exc).__name__}: {exc}"
            )
            return PhaseResult(PhaseAction.BREAK)
        logger.error(
            'turn.unhandled_error error_type=%s validation_errors=%s',
            type(exc).__name__, self._validation_error_details(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            state.warnings.extend(runtime.observability.publish(
                "turn.unhandled_error", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"error_type": type(exc).__name__},
            ))
        except BaseException as publish_exc:
            state.warnings.append(
                f"outer-loop error observation failed: {type(publish_exc).__name__}"
            )
        self._set_error(state, "failed", "turn_failed", exc, False)
        return PhaseResult(PhaseAction.BREAK)

    def cleanup_all(self, runtime: TurnRuntime, state: LoopState) -> None:
        try:
            if state.error is not None and not state.finalized:
                try:
                    result = run_phase(self._handle_error_phase(), runtime, state)
                    if result.action is not PhaseAction.FALLTHROUGH:
                        raise RuntimeError(
                            f"handle_turn_error returned unexpected action {result.action.value}"
                        )
                except BaseException as exc:
                    state.warnings.append(
                        f"handle_turn_error phase failed: {type(exc).__name__}: {exc}"
                    )
            self._cleanup(runtime, state)
        finally:
            # Phase observation can fail before its action; resource release
            # must still run, owned solely by this loop's cleanup.
            if not state.released:
                try:
                    self._release(runtime, state)
                except BaseException as exc:
                    state.warnings.append(f"execution release failed: {type(exc).__name__}: {exc}")
            try:
                self.turn_ended(state)
            except BaseException as exc:
                state.warnings.append(
                    f"turn end logging failed: {type(exc).__name__}: {exc}"
                )

    def to_result(self, state: LoopState) -> TurnResult:
        return self._result(state)

    def _expected_break(
        self, runtime: TurnRuntime, state: LoopState, exc: BaseException,
    ) -> PhaseResult:
        if isinstance(exc, WorkspaceValidationError):
            self._set_error(state, "failed", "invalid_workspace", exc, False)
        elif isinstance(exc, LeaseAcquireTimeout):
            self._set_error(state, "failed", "lease_timeout", exc, True)
        elif isinstance(exc, LeaseLostError):
            logger.error('lease.fenced_write_rejected error_type=%s', type(exc).__name__)
            state.warnings.extend(runtime.observability.publish(
                "lease.fenced_write_rejected", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                payload={"error_type": type(exc).__name__},
            ))
            self._set_error(state, "interrupted", "lease_lost", exc, True)
        elif isinstance(exc, TurnCancelled):
            self._set_error(state, "interrupted", "cancelled", exc, True)
        elif isinstance(exc, RunBudgetExceeded):
            self._set_error(state, "interrupted", "run_budget_exhausted", exc, True)
        elif isinstance(exc, ProviderExhausted):
            state.status = "failed"
            state.completion_reason = "providers_exhausted"
            state.error = TurnError(
                code="providers_exhausted", message=str(exc), retryable=True,
                details={"failures": exc.failures},
            )
        elif isinstance(exc, ProviderRequestAborted):
            state.route_used = exc.route_name
            state.status = "failed"
            state.completion_reason = "provider_request_aborted"
            state.error = TurnError(
                code=exc.error.error_type, message=str(exc.error),
                retryable=exc.error.retryable,
                details={"status_code": exc.error.status_code},
            )
        elif isinstance(exc, RecoveryNotEligible):
            self._set_error(state, "failed", "recovery_not_eligible", exc, False)
        elif isinstance(exc, RecoverySessionMissing):
            self._set_error(state, "failed", "recovery_session_missing", exc, False)
        else:
            raise TypeError(f"unmapped expected phase error: {type(exc).__name__}")
        return PhaseResult(PhaseAction.BREAK)

    def _handle_error_phase(self) -> FunctionPhase:
        def handle(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            return PhaseResult()

        def validate(state: LoopState, result: PhaseResult) -> None:
            if state.status not in {"failed", "interrupted"} or state.error is None:
                raise RuntimeError("error phase requires a non-completed structured error")

        return FunctionPhase(
            "handle_turn_error", handle, validate, check_active=False
        )

    def _acquire_execution_phase(self) -> FunctionPhase:
        def acquire(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            if self.lease_scope is not None:
                self.lease_scope.acquire(runtime.check_active)
                fence = self.lease_scope.fence
                self.persistence.bind_fence(fence)
                logger.info('lease.acquired fencing_token=%s', fence.fencing_token)
                state.warnings.extend(runtime.observability.publish(
                    "lease.acquired", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    payload={"fencing_token": fence.fencing_token},
                ))
            return PhaseResult()

        return FunctionPhase(
            "acquire_execution", acquire,
            expected_errors=_INTERRUPTION_ERRORS + (LeaseAcquireTimeout,),
            on_expected_error=self._expected_break,
        )

    def _prepare_conversation_phase(self) -> FunctionPhase:
        def prepare(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            if self.recovery:
                state.recovery_claimed = self.persistence.save_recovery_claim(
                    max_attempts=self.max_recovery_attempts,
                )
                if not state.recovery_claimed:
                    raise RecoveryNotEligible("session is not eligible for internal recovery")
                logger.info("recovery.started")
                state.warnings.extend(runtime.observability.publish(
                    "recovery.started", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                ))
            prepared = self.prepare(runtime)
            if self.knowledge_provider.hidden_model_tools:
                policy = ToolsetRequest(
                    enabled_toolsets=(),
                    disabled_tools=tuple(sorted(self.knowledge_provider.hidden_model_tools)),
                )
                prepared = replace(prepared, context=prepared.context.with_tool_policy(policy))
            state.accept(prepared)
            if prepared.session_created is not None:
                event = "session.created" if prepared.session_created else "session.loaded"
                logger.info("%s", event)
                state.warnings.extend(runtime.observability.publish(
                    event,
                    trace_id=state.trace_id, session_id=state.session_id, turn_id=state.turn_id,
                ))
            logger.info("turn.admitted")
            state.warnings.extend(runtime.observability.publish(
                "turn.admitted", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
            ))
            return PhaseResult()

        def validate(state: LoopState, result: PhaseResult) -> None:
            state.require_context("prepare_conversation")
            if not state.admitted or state.submitted_message is None:
                raise RuntimeError("current turn was not admitted")
            if not state.messages or state.messages[-1] != state.submitted_message:
                raise RuntimeError("history does not end with the submitted message")

        return FunctionPhase(
            "prepare_conversation", prepare, validate,
            expected_errors=_INTERRUPTION_ERRORS + (
                RecoveryNotEligible, RecoverySessionMissing,
                WorkspaceValidationError,
            ),
            on_expected_error=self._expected_break,
        )

    def _prepare_knowledge_phase(self) -> FunctionPhase:
        def prepare(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            state.require_context("prepare_knowledge")
            if type(self.knowledge_provider) is NoopKnowledgeProvider:
                state.knowledge_context = KnowledgeContext()
                return PhaseResult()
            request = request_from_messages(
                tuple(state.messages), session_id=state.session_id, turn_id=state.turn_id,
                permissions=state.tool_permissions,
                capabilities=frozenset(name for name in ("clarify", "approval")
                                       if getattr(self.host_context.capabilities, name)),
                history_max_chars=self.config.knowledge.history_max_chars,
            )
            knowledge = self.knowledge_provider.prepare(request, runtime)
            runtime.check_active()
            if not isinstance(knowledge, KnowledgeContext):
                raise TypeError("KnowledgeProvider.prepare must return KnowledgeContext")
            state.knowledge_context = knowledge
            state.warnings.extend(knowledge.warnings)
            return PhaseResult()

        def validate(state: LoopState, result: PhaseResult) -> None:
            if state.knowledge_context is None:
                raise RuntimeError("knowledge preparation produced no context")

        return FunctionPhase(
            "prepare_knowledge", prepare, validate, expected_errors=_INTERRUPTION_ERRORS,
            on_expected_error=self._expected_break,
        )

    def _assemble_phase(self) -> FunctionPhase:
        def assemble(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            context = state.require_context('assemble_request')
            knowledge_block = render_knowledge(state.knowledge_context)
            state.request = CanonicalModelRequest.build(
                system_prompt="\n\n".join(filter(None, (context.prompt.text, knowledge_block))),
                messages=tuple(state.messages),
                tool_schema_bytes=context.tool_schema_bytes,
            )
            logger.debug(
                'turn.request_assembled phase=%s iteration=%s message_count=%s tool_count=%s',
                'assemble_request',
                state.iteration,
                len(state.request.messages),
                len(context.entries_by_name),
            )
            return PhaseResult()

        def validate(state: LoopState, result: PhaseResult) -> None:
            if state.request is None or not state.request.fingerprint:
                raise RuntimeError("canonical request was not assembled")

        return FunctionPhase(
            "assemble_request", assemble, validate,
            expected_errors=_INTERRUPTION_ERRORS,
            on_expected_error=self._expected_break,
        )

    def _provider_phase(self) -> FunctionPhase:
        def provider(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            if self.on_output is None:
                response, route_index = self.provider_runner.run(runtime, state)
            else:
                response, route_index = self.provider_runner.run(runtime, state, on_output=self.on_output)
            state.response = response
            state.active_route_index = route_index
            state.route_used = self.config.routes[route_index].name
            for key, value in response.usage.items():
                if isinstance(value, int):
                    state.usage[key] = state.usage.get(key, 0) + value
            return PhaseResult()

        def validate(state: LoopState, result: PhaseResult) -> None:
            if state.response is None:
                raise RuntimeError("provider phase produced no normalized response")

        return FunctionPhase(
            "provider_attempts", provider, validate,
            expected_errors=_INTERRUPTION_ERRORS + (
                ProviderExhausted, ProviderRequestAborted,
            ),
            on_expected_error=self._expected_break,
        )

    def _tool_round_phase(self) -> FunctionPhase:
        def execute(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            response = state.require_response('tool_round')
            if not response.tool_calls:
                raise RuntimeError('tool_round requires response with tool_calls')
            self.tool_round_runner.run(runtime, state, response)
            return PhaseResult(PhaseAction.CONTINUE)

        def validate(state: LoopState, result: PhaseResult) -> None:
            response = state.require_response('tool_round')
            round_messages = state.messages[-(len(response.tool_calls) + 1):]
            if (
                len(round_messages) != len(response.tool_calls) + 1
                or round_messages[0] is not state.assistant_message
                or [
                    message.tool_call_id if message.role == "tool" else None
                    for message in round_messages[1:]
                ] != [call.id for call in response.tool_calls]
            ):
                raise RuntimeError("tool round left unresolved tool calls")

        return FunctionPhase(
            "tool_round", execute, validate,
            expected_errors=_INTERRUPTION_ERRORS,
            on_expected_error=self._expected_break,
        )

    def _finish_text_phase(self) -> FunctionPhase:
        def finish(runtime: TurnRuntime, state: LoopState) -> PhaseResult:
            response = state.require_response('finish_text')
            if response.tool_calls:
                raise RuntimeError('finish_text requires a text response')
            text = response.content or ""
            if response.finish_reason in {"length", "content_filter"}:
                state.status = "failed"
                state.completion_reason = "incomplete_response"
                state.error = TurnError(
                    code="incomplete_response",
                    message=f"model stopped with finish_reason={response.finish_reason}",
                    retryable=response.finish_reason == "length",
                )
                state.finalized = True
                return PhaseResult(PhaseAction.BREAK)
            else:
                state.status = "completed"
                state.completion_reason = "model_final"
                state.error = None
            assistant = Message.assistant_text(text)
            self.persistence.save_final_response(
                assistant, recovery=self.recovery or state.user_driven_recovery,
                delivery_destination=self.delivery_destination,
            )
            state.assistant_message = assistant
            state.final_text = text
            state.messages.append(assistant)
            state.finalized = True
            return PhaseResult(PhaseAction.BREAK)

        def validate(state: LoopState, result: PhaseResult) -> None:
            if state.response is None or state.response.tool_calls:
                raise RuntimeError("finish_text requires a text response")
            if not state.finalized:
                raise RuntimeError("terminal assistant was not atomically finalized")

        return FunctionPhase(
            "finish_text", finish, validate,
            expected_errors=_INTERRUPTION_ERRORS,
            on_expected_error=self._expected_break,
        )

    def _cleanup(self, runtime: TurnRuntime, state: LoopState) -> None:
        phases = (
            FunctionPhase(
                "repair_unresolved_tools", self._repair, check_active=False,
                expected_errors=(LeaseLostError,),
                on_expected_error=lambda rt, st, exc: self._cleanup_break(
                    st, "repair_unresolved_tools", exc
                ), cleanup_phase=True,
            ),
            FunctionPhase(
                "finalize_turn", self._finalize, check_active=False,
                cleanup_phase=True,
            ),
            FunctionPhase(
                "record_recovery_failure", self._record_recovery_failure,
                check_active=False, expected_errors=(LeaseLostError,),
                on_expected_error=lambda rt, st, exc: self._cleanup_break(
                    st, "record_recovery_failure", exc
                ), cleanup_phase=True,
            ),
            FunctionPhase(
                "end_turn", self._end_turn, check_active=False,
                cleanup_phase=True,
            ),
            FunctionPhase(
                "release_execution", self._release,
                self._validate_released, check_active=False,
                expected_errors=(LeaseLostError,),
                on_expected_error=lambda rt, st, exc: self._cleanup_break(
                    st, "release_execution", exc
                ), cleanup_phase=True,
            ),
        )
        for phase in phases:
            try:
                action = run_phase(phase, runtime, state).action
                if action not in {PhaseAction.FALLTHROUGH, PhaseAction.BREAK}:
                    raise RuntimeError(
                        f"{phase.name} returned unexpected action {action.value}"
                    )
                if action is PhaseAction.BREAK:
                    state.warnings.append(
                        f"cleanup phase {phase.name} ended early"
                    )
            except BaseException as exc:
                state.warnings.append(
                    f"cleanup phase {phase.name} failed: {type(exc).__name__}: {exc}"
                )

    @staticmethod
    def _cleanup_break(
        state: LoopState, phase_name: str, exc: BaseException,
    ) -> PhaseResult:
        state.warnings.append(
            f"cleanup phase {phase_name} was interrupted: {type(exc).__name__}: {exc}"
        )
        return PhaseResult(PhaseAction.BREAK)

    def _repair(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if not state.admitted:
            return PhaseResult()
        repaired = interrupted_tool_results(state.messages, self.config.tool_result_max_chars)
        for message in repaired:
            self.persistence.save_tool_result(message)
            state.messages.append(message)
            side_effects = (
                "none" if message.effect_disposition == "none" else "possible"
            )
            state.tool_outcomes.append(ToolOutcome(
                tool_call_id=message.tool_call_id or "",
                tool_name=message.tool_name or "",
                side_effects=side_effects, state="unknown",
                effect_disposition=message.effect_disposition or "unknown",
                error_type=message.tool_error_type,
            ))
        if repaired:
            state.warnings.append(f"repaired {len(repaired)} incomplete tool replies")
            logger.info(
                'recovery.tool_pair_repaired phase=%s repair_count=%s',
                'repair_unresolved_tools',
                len(repaired),
            )
            state.warnings.extend(runtime.observability.publish(
                "recovery.tool_pair_repaired", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="repair_unresolved_tools",
                payload={"repair_count": len(repaired)},
            ))
        return PhaseResult()

    def _finalize(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if not state.admitted or state.finalized:
            return PhaseResult()
        if state.error is None:
            state.error = TurnError(
                code=state.completion_reason,
                message=state.completion_reason.replace("_", " "),
                retryable=False,
            )
        state.finalized = True
        return PhaseResult()

    def _record_recovery_failure(
        self, runtime: TurnRuntime, state: LoopState
    ) -> PhaseResult:
        if self.recovery and state.recovery_claimed and state.status != "completed":
            self.persistence.save_recovery_failure(max_attempts=self.max_recovery_attempts)
        return PhaseResult()

    def _end_turn(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        if not state.finalized:
            return PhaseResult()
        snapshot = TurnSnapshot(
            result=self._result(state),
            messages=tuple(
                message.model_dump(mode="json") for message in state.messages
            ),
            context=state.require_context("end_turn"),
            workspace=state.workspace, trace_id=state.trace_id,
            trigger_kind="internal_recovery" if self.recovery else "user",
        )
        operations = (
            (
                "memory updater",
                lambda: self.memory_updater.update(snapshot.copy()), # NoopMemoryUpdater 目前没有执行记忆更新
                self.config.timeouts.memory_seconds,
            ), 
            (
                "review scheduler",
                lambda: self.review_scheduler.schedule(snapshot.copy()),
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
        runtime.close()
        try:
            if self.lease_scope is not None:
                self.lease_scope.close()
        finally:
            try:
                runtime.services.interactions.cancel(owner_id=runtime.owner_id, turn_id=state.turn_id)
            finally:
                state.released = True
        logger.debug('execution.released')
        state.warnings.extend(runtime.observability.publish(
            "execution.released", trace_id=state.trace_id,
            session_id=state.session_id, turn_id=state.turn_id,
        ))
        return PhaseResult()

    @staticmethod
    def _validate_released(state: LoopState, result: PhaseResult) -> None:
        if not state.released:
            raise RuntimeError("execution was not released")

    def _result(self, state: LoopState) -> TurnResult:
        if state.status != "completed" and state.error is None:
            state.error = TurnError(
                code=state.completion_reason,
                message=state.completion_reason.replace("_", " "),
                retryable=False,
            )
        return TurnResult(
            status=state.status, final_text=state.final_text,
            session_id=state.session_id, turn_id=state.turn_id,
            iterations=state.iteration,
            route_used=state.route_used, usage=state.usage,
            warnings=state.warnings, error=state.error,
            completion_reason=state.completion_reason,
            tool_outcomes=state.tool_outcomes,
            provider_attempts=state.provider_attempts,
            route_attempts=state.route_attempts,
        )

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
    "FunctionPhase", "PhaseAction", "PhaseResult", "ProviderExhausted",
    "ProviderRequestAborted", "RecoveryNotEligible", "RouteState",
    "TurnPhase", "TurnPhases", "run_phase",
]
