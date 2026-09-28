"""Read-only tool availability checks and per-call host interactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from agentcore.interaction import (
    ApprovalPayload, ClarifyPayload, InteractionUnavailable, Question, immutable,
)
from agentcore.interfaces import HostCapabilities, TurnHostContext
from agentcore.runtime_cwd import ResolvedWorkspace
from .config import ToolServiceConfig
from .lifecycle import SessionResourceKey

if TYPE_CHECKING:
    from .services import ToolServices


@dataclass(frozen=True, slots=True)
class BackendProbes:
    _available: Callable

    def available(self, name: str) -> bool:
        return self._available(name)


@dataclass(frozen=True, slots=True)
class ToolAvailabilityContext:
    config: ToolServiceConfig
    workspace: ResolvedWorkspace
    capabilities: HostCapabilities
    backends: BackendProbes


@dataclass(frozen=True, slots=True)
class ToolInteractions:
    services: ToolServices
    host: TurnHostContext
    resource_key: SessionResourceKey
    owner_id: str
    turn_id: str
    tool_call_id: str
    check_active: Callable[[], None]
    pause: Callable[[], None]
    resume: Callable[[], None]

    def _request(self, kind, payload, timeout):
        self.check_active()
        if not getattr(self.host.capabilities, kind):
            raise InteractionUnavailable(f"{kind} is unavailable for this turn")
        self.pause()
        try:
            response = self.services.interactions.request(
                kind, payload, key=self.resource_key, owner_id=self.owner_id,
                turn_id=self.turn_id, tool_call_id=self.tool_call_id,
                port=self.host.interaction_port, timeout=timeout,
                check_active=self.check_active,
            )
            self.check_active()
            return response
        finally:
            self.resume()

    def ask(self, questions: tuple[Question, ...]):
        return self._request(
            "clarify", ClarifyPayload(tuple(questions)),
            self.services.config.tools.clarify_timeout_seconds,
        )

    def approve(self, tool_name, scope, arguments, *, permanent: bool):
        choices = ("once", "session", "always", "deny") if permanent else ("once", "session", "deny")
        return self._request(
            "approval", ApprovalPayload(tool_name, scope, immutable(arguments), choices),
            self.services.config.tools.approval_timeout_seconds,
        )
