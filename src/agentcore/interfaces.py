from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .models import MemoryItem, ModelRequest, ModelResponse, TurnResult, UserInput


class ProviderTransport(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...

    def close(self) -> None: ...


class MemoryProvider(Protocol):
    def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]: ...


class ReviewScheduler(Protocol):
    def schedule(self, snapshot: "TurnSnapshot") -> None: ...


class TurnCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CancellationToken:
    _event: threading.Event

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TurnCancelled("turn was cancelled")


@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ToolContext:
    session_id: str
    turn_id: str
    tool_call_id: str
    cancellation: CancellationToken | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    requires_approval: bool


class ToolGuard(Protocol):
    def check(self, invocation: ToolInvocation) -> GuardDecision: ...


class Approver(Protocol):
    def approve(self, invocation: ToolInvocation) -> ApprovalDecision: ...


@dataclass(frozen=True, slots=True)
class TurnStartEvent:
    session_id: str
    turn_id: str
    context_version: int


@dataclass(frozen=True, slots=True)
class ModelCallEvent:
    session_id: str
    turn_id: str
    route_name: str
    iteration: int
    attempt: int


@dataclass(frozen=True, slots=True)
class ModelResponseEvent:
    session_id: str
    turn_id: str
    route_name: str
    iteration: int
    finish_reason: str | None
    tool_call_count: int


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    invocation: ToolInvocation
    success: bool
    result: str


@dataclass(frozen=True, slots=True)
class TurnEndEvent:
    session_id: str
    turn_id: str
    status: str


class LifecycleMiddleware(Protocol):
    """Optional synchronous, read-only lifecycle observer."""


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    result: TurnResult
    messages: tuple[dict[str, Any], ...]


class NoopMemoryProvider:
    def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]:
        return ()


class NoopReviewScheduler:
    def schedule(self, snapshot: TurnSnapshot) -> None:
        return None


class AllowAllGuard:
    def check(self, invocation: ToolInvocation) -> GuardDecision:
        return GuardDecision(allowed=True)
