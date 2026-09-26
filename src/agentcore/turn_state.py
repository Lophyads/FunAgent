from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

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


class OperationTimeoutError(TimeoutError):
    pass


class RunBudgetExceeded(TimeoutError):
    pass


@dataclass(slots=True)
class LoopState:
    session_id: str
    turn_id: str
    trace_id: str
    started_at_monotonic: float = field(default_factory=time.monotonic)
    lease: LeaseHandle | None = None
    context: SessionRuntimeSnapshot | None = None
    messages: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    iteration: int = 0
    active_route_index: int = 0
    request: CanonicalModelRequest | None = None
    response: NormalizedModelResponse | None = None
    assistant_message: Message | None = None
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

    def require_lease(self, phase: str) -> LeaseHandle:
        lease = self.lease
        if lease is None:
            raise RuntimeError(f"{phase} requires lease")
        return lease

    def require_context(self, phase: str) -> SessionRuntimeSnapshot:
        context = self.context
        if context is None:
            raise RuntimeError(f"{phase} requires context")
        return context

    def require_request(self, phase: str) -> CanonicalModelRequest:
        request = self.request
        if request is None:
            raise RuntimeError(f"{phase} requires request")
        return request

    def require_response(self, phase: str) -> NormalizedModelResponse:
        response = self.response
        if response is None:
            raise RuntimeError(f"{phase} requires response")
        return response


@dataclass(slots=True)
class TurnRuntime:
    config: AgentConfig
    store: Any
    lease_store: Any
    observability: Any
    cancel_event: threading.Event
    lease_lost_event: threading.Event
    deadline: float | None
    call_operation: Callable[..., Any]
    _budget_lock: threading.RLock = field(default_factory=threading.RLock)
    _paused_at: float | None = None

    def pause_budget(self) -> None:
        with self._budget_lock:
            if self._paused_at is not None:
                raise RuntimeError("run budget is already paused")
            self._paused_at = time.monotonic()

    def resume_budget(self) -> None:
        with self._budget_lock:
            if self._paused_at is None:
                return
            if self.deadline is not None:
                self.deadline += time.monotonic() - self._paused_at
            self._paused_at = None

    def check_active(self, state: LoopState) -> None:
        if self.cancel_event.is_set():
            raise TurnCancelled("turn was cancelled")
        if self.lease_lost_event.is_set():
            raise TurnCancelled("session lease was lost")
        with self._budget_lock:
            if self._paused_at is None and (
                self.deadline is not None and time.monotonic() >= self.deadline
            ):
                raise RunBudgetExceeded("turn run budget was exhausted")
        if state.lease is not None:
            self.lease_store.assert_current(state.lease)


__all__ = [
    "LoopState", "OperationTimeoutError",
    "RunBudgetExceeded", "TurnRuntime",
]
