from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from .interfaces import TurnCancelled
from .models import (
    AgentConfig,
    CanonicalModelRequest,
    LeaseHandle,
    Message,
    NormalizedModelResponse,
    ToolOutcome,
    TurnError,
)
from .session_context import SessionRuntimeSnapshot


logger = logging.getLogger(__name__)


class OperationTimeoutError(TimeoutError):
    pass


class RunBudgetExceeded(TimeoutError):
    pass


class PhaseAction(str, Enum):
    PROCEED = "proceed"
    NEXT_ITERATION = "next_iteration"
    STOP_TURN = "stop_turn"


@dataclass(frozen=True, slots=True)
class PhaseResult:
    action: PhaseAction = PhaseAction.PROCEED


@dataclass(slots=True)
class AttemptState:
    attempt_id: str
    route_index: int
    route_name: str
    attempt_number: int
    request_fingerprint: str
    started_at: float
    provider_error: BaseException | None = None


@dataclass(slots=True)
class LoopState:
    session_id: str
    turn_id: str
    trace_id: str
    started_at_monotonic: float = field(default_factory=time.monotonic)
    lease: LeaseHandle | None = None
    context: SessionRuntimeSnapshot | None = None
    messages: list[Message] = field(default_factory=list)
    new_messages: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    iteration: int = 0
    active_route_index: int = 0
    request: CanonicalModelRequest | None = None
    response: NormalizedModelResponse | None = None
    assistant_message: Message | None = None
    attempt: AttemptState | None = None
    status: str = "failed"
    completion_reason: str = "turn_failed"
    final_text: str | None = None
    error: TurnError | None = None
    route_used: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    provider_attempts: int = 0
    route_attempts: dict[str, int] = field(default_factory=dict)
    admitted: bool = False
    session_created: bool = False
    history_recovered: bool = False
    submitted_message: Message | None = None
    recovery_claimed: bool = False
    user_driven_recovery: bool = False
    finalized: bool = False
    released: bool = False
    business_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class TurnRuntime:
    config: AgentConfig
    store: Any
    lease_store: Any
    observability: Any
    cancel_event: threading.Event
    lease_lost_event: threading.Event
    deadline: float | None
    call_operation: Callable[..., Any]

    def check_active(self, state: LoopState) -> None:
        if self.cancel_event.is_set():
            raise TurnCancelled("turn was cancelled")
        if self.lease_lost_event.is_set():
            raise TurnCancelled("session lease was lost")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RunBudgetExceeded("turn run budget was exhausted")
        if state.lease is not None:
            self.lease_store.assert_current(state.lease)


class TurnPhase(Protocol):
    name: str
    check_active: bool

    def __call__(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult: ...

    def validate_postconditions(self, state: LoopState) -> None: ...


@dataclass(frozen=True, slots=True)
class FunctionPhase:
    name: str
    function: Callable[[TurnRuntime, LoopState], PhaseResult]
    validator: Callable[[LoopState], None] = lambda state: None
    check_active: bool = True

    def __call__(self, runtime: TurnRuntime, state: LoopState) -> PhaseResult:
        return self.function(runtime, state)

    def validate_postconditions(self, state: LoopState) -> None:
        self.validator(state)


def run_phase(
    phase: TurnPhase, runtime: TurnRuntime, state: LoopState,
) -> PhaseResult:
    if phase.check_active:
        runtime.check_active(state)
    logger.debug('turn.phase_started phase=%s iteration=%s', phase.name, state.iteration)
    state.warnings.extend(runtime.observability.publish(
        "turn.phase_started", trace_id=state.trace_id,
        session_id=state.session_id, turn_id=state.turn_id,
        phase=phase.name, payload={"iteration": state.iteration},
    ))
    try:
        result = phase(runtime, state)
        phase.validate_postconditions(state)
    except BaseException as exc:
        logger.error('turn.phase_failed phase=%s error_type=%s', phase.name, type(exc).__name__)
        state.warnings.extend(runtime.observability.publish(
            "turn.phase_failed", trace_id=state.trace_id,
            session_id=state.session_id, turn_id=state.turn_id,
            phase=phase.name,
            payload={"error_type": type(exc).__name__},
        ))
        if type(exc).__name__ in {
            "StorageError", "DatabaseError", "OperationalError",
            "IntegrityError",
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
        phase=phase.name, payload={"action": result.action.value},
    ))
    return result


__all__ = [
    "AttemptState",
    "FunctionPhase",
    "LoopState",
    "OperationTimeoutError",
    "PhaseAction",
    "PhaseResult",
    "RunBudgetExceeded",
    "TurnPhase",
    "TurnRuntime",
    "run_phase",
]
