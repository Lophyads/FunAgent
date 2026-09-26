"""Capabilities borrowed by one tool call; no global lifecycle operations."""

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


@dataclass(slots=True)
class ToolUse:
    services: ToolServices
    key: SessionResourceKey
    generation: int
    owner_id: str
    turn_id: str
    call_id: str
    check_turn: Callable[[], None]
    active: bool = True

    def check(self) -> None:
        self.services.check_use(self)
        self.check_turn()


class ToolInteractions:
    def __init__(self, use: ToolUse, host: TurnHostContext, pause, resume):
        self._use = use
        self._host = host
        self._pause = pause
        self._resume = resume

    def _request(self, kind, payload, timeout):
        use = self._use
        use.check()
        if not getattr(self._host.capabilities, kind):
            raise InteractionUnavailable(f"{kind} is unavailable for this turn")
        self._pause()
        try:
            response = use.services.interactions.request(
                kind, payload, key=use.key, owner_id=use.owner_id,
                turn_id=use.turn_id, tool_call_id=use.call_id,
                port=self._host.interaction_port, timeout=timeout,
                check_active=use.check,
            )
            use.check()
            return response
        finally:
            self._resume()

    def ask(self, questions: tuple[Question, ...]):
        return self._request(
            "clarify", ClarifyPayload(tuple(questions)),
            self._use.services.config.tools.clarify_timeout_seconds,
        )

    def approve(self, tool_name, scope, arguments, *, permanent: bool):
        choices = ("once", "session", "always", "deny") if permanent else ("once", "session", "deny")
        return self._request(
            "approval", ApprovalPayload(tool_name, scope, immutable(arguments), choices),
            self._use.services.config.tools.approval_timeout_seconds,
        )


class _Browser:
    def __init__(self, use: ToolUse):
        self._use = use

    def action(self, command, args, context, **kwargs):
        use = self._use
        backend = use.services._provider("browser", use)
        use.check()
        return backend.action(use.key, command, args, context, **kwargs)


class _Computer:
    def __init__(self, use: ToolUse):
        self._use = use

    def execute(self, args, context):
        use = self._use
        backend = use.services._provider("computer", use)
        use.check()
        return backend.execute(use.key, args, context)


class ToolResources:
    def __init__(self, use: ToolUse):
        self._use = use

    @property
    def config(self) -> ToolServiceConfig:
        self._use.check()
        return self._use.services.config.tools

    def browser(self) -> _Browser:
        self._use.check()
        return _Browser(self._use)

    def computer(self) -> _Computer:
        self._use.check()
        return _Computer(self._use)

    def search(self, query, limit, context):
        self._use.check()
        backend = self._use.services._search_backend
        if backend is not None:
            return backend.search(query, limit, context)
        from .web_tool import _search_subprocess
        return _search_subprocess(query, limit, context, self.config.search_timeout_seconds)

    def fetch(self, url):
        self._use.check()
        backend = self._use.services._http_backend
        if backend is not None:
            return backend.get(url)
        from .web_tool import _fetch_http
        return _fetch_http(url, allow_private=self.config.allow_private_web_addresses,
                           max_bytes=self.config.artifact_max_bytes)
