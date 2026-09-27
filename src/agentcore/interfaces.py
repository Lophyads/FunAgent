from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    from .interaction import InteractionPort
    from .runtime_cwd import ResolvedWorkspace
    from tools.context import ToolInteractions, ToolResources

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
    scope: Literal["once", "session", "always"] = "once"


@dataclass(frozen=True, slots=True)
class HumanApprovalRequired:
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    """本轮宿主能否接收人工交互；approval=True 表示能请求审批，不表示已经授权。"""

    clarify: bool = False
    approval: bool = False


@dataclass(frozen=True, slots=True)
class TurnHostContext:
    """提交时固定的宿主能力和通知出口，不保存历史、不拥有共享工具资源。

    同一 Session 的不同 Turn 可以来自不同宿主，因此不把交互出口绑定为 Session 永久属性。
    Gateway 通常提供可撤销的本轮端口；直接调用 AgentCore 时默认没有人工交互能力。
    """

    capabilities: HostCapabilities = HostCapabilities()
    interaction_port: InteractionPort | None = None

    def __post_init__(self) -> None:
        interactive = self.capabilities.clarify or self.capabilities.approval
        if interactive and self.interaction_port is None:
            raise ValueError("interactive capabilities require an InteractionPort")


@dataclass(frozen=True, slots=True)
class ToolContext:
    """单次工具调用借用的能力；Handler 使用这些接口，不需要持有 Agent 或 Gateway。

    interactions 从本轮 TurnHostContext 接线，resources 访问按 Session 复用的后端；
    取消/超时后应检查 check_active，不能把本次上下文留给后续调用继续使用。
    """

    session_id: str
    turn_id: str
    tool_call_id: str
    workspace: ResolvedWorkspace
    interactions: ToolInteractions
    resources: ToolResources
    cancellation: CancellationToken
    check_active: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    requires_approval: bool
    approval_scopes: tuple[str, ...] = ()


class ToolGuard(Protocol):
    def check(self, invocation: ToolInvocation) -> GuardDecision: ...


class Approver(Protocol):
    def approve(self, invocation: ToolInvocation) -> ApprovalDecision | HumanApprovalRequired: ...


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
    "HostCapabilities", "TurnHostContext", "HumanApprovalRequired",
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
