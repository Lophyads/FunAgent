from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .models import MemoryItem, ModelRequest, ModelResponse, TurnResult, UserInput


class ProviderTransport(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def aclose(self) -> None: ...


class MemoryProvider(Protocol):
    async def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]: ...


class ReviewScheduler(Protocol):
    async def schedule(self, snapshot: "TurnSnapshot") -> None: ...


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


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    requires_approval: bool


class ToolGuard(Protocol):
    async def check(self, invocation: ToolInvocation) -> GuardDecision: ...


class Approver(Protocol):
    async def approve(self, invocation: ToolInvocation) -> ApprovalDecision: ...


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
    """Optional, read-only lifecycle observer.

    Implementations may define any subset of the named async methods. Return values
    are ignored and hook failures are reported as warnings instead of changing policy.
    """


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    result: TurnResult
    messages: tuple[dict[str, Any], ...]


class NoopMemoryProvider:
    async def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]:
        return ()


class NoopReviewScheduler:
    async def schedule(self, snapshot: TurnSnapshot) -> None:
        return None


class AllowAllGuard:
    async def check(self, invocation: ToolInvocation) -> GuardDecision:
        return GuardDecision(allowed=True)

