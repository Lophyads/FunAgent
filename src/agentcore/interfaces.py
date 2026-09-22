from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .models import MemoryItem, TurnResult, UserInput


class MemoryProvider(Protocol):
    def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]: ...


class MemoryUpdater(Protocol):
    def update(self, snapshot: "TurnSnapshot") -> None: ...


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
class TurnSnapshot:
    result: TurnResult
    messages: tuple[dict[str, Any], ...]


class NoopMemoryProvider:
    def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]:
        return ()


class NoopMemoryUpdater:
    def update(self, snapshot: TurnSnapshot) -> None:
        return None


class NoopReviewScheduler:
    def schedule(self, snapshot: TurnSnapshot) -> None:
        return None


class AllowAllGuard:
    def check(self, invocation: ToolInvocation) -> GuardDecision:
        return GuardDecision(allowed=True)


__all__ = [
    "AllowAllGuard",
    "ApprovalDecision",
    "Approver",
    "CancellationToken",
    "GuardDecision",
    "MemoryProvider",
    "MemoryUpdater",
    "NoopMemoryProvider",
    "NoopMemoryUpdater",
    "NoopReviewScheduler",
    "ReviewScheduler",
    "ToolContext",
    "ToolGuard",
    "ToolInvocation",
    "TurnCancelled",
    "TurnSnapshot",
]
