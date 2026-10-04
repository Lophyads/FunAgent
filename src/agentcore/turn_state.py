from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .interfaces import TurnCancelled
from .models import (
    AgentConfig,
    CanonicalModelRequest,
    Message,
    NormalizedModelResponse,
    ToolOutcome,
    TurnError,
)
from .session_context import SessionRuntimeSnapshot

if TYPE_CHECKING:
    from knowledge.models import KnowledgeContext
    from .lease import LeaseScope
    from .turn_preparation import PreparedConversation
    from tools.services import ToolServices
    from tools.lifecycle import SessionResourceKey
    from .runtime_cwd import ResolvedWorkspace


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
    context: SessionRuntimeSnapshot | None = None
    workspace: ResolvedWorkspace | None = None
    resource_key: SessionResourceKey | None = None
    messages: list[Message] = field(default_factory=list)
    knowledge_context: KnowledgeContext | None = None
    tool_permissions: frozenset[str] = frozenset()
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
    submitted_message: Message | None = None
    recovery_claimed: bool = False
    user_driven_recovery: bool = False
    finalized: bool = False
    released: bool = False

    def accept(self, prepared: PreparedConversation) -> None:
        if self.admitted:
            raise RuntimeError("turn has already been admitted")
        self.context = prepared.context.copy()
        self.workspace = prepared.workspace
        self.resource_key = prepared.resource_key
        self.tool_permissions = prepared.tool_permissions
        self.messages = [m.model_copy(deep=True) for m in prepared.messages]
        self.submitted_message = self.messages[-1]
        self.user_driven_recovery = prepared.user_driven_recovery
        self.warnings.extend(prepared.warnings)
        self.admitted = True

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
    owner_id: str
    turn_id: str
    services: ToolServices
    observability: Any
    cancel_event: threading.Event
    deadline: float | None
    call_operation: Callable[..., Any]
    lease_scope: LeaseScope | None = None
    _closed: threading.Event = field(default_factory=threading.Event)
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

    def check_active(self) -> None:
        if self._closed.is_set():
            raise TurnCancelled("turn execution is closed")
        if self.cancel_event.is_set():
            raise TurnCancelled("turn was cancelled")
        with self._budget_lock:
            if self._paused_at is None and (
                self.deadline is not None and time.monotonic() >= self.deadline
            ):
                raise RunBudgetExceeded("turn run budget was exhausted")
        if self.lease_scope is not None:
            self.lease_scope.assert_current()

    def close(self) -> None:
        self._closed.set()


__all__ = [
    "LoopState", "OperationTimeoutError",
    "RunBudgetExceeded", "TurnRuntime",
]
