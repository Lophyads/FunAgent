"""Process-owned services; all mutable resources are partitioned by Session key."""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentcore.approval import ApprovalGate, ApprovalGrantStore
from agentcore.interaction import InteractionBroker
from agentcore.runtime_cwd import ResolvedWorkspace, SessionWorkspaceConflict, resolve_workspace
from agentcore.workers import DaemonWorkerPool
from .config import ToolSystemConfig
from .lifecycle import (
    CleanupIssue, CleanupReport, ResourceUnavailableError, SessionResourceKey,
    SessionResourcesBusyError, remaining,
)

if TYPE_CHECKING:
    from .context import ToolUse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Session:
    workspace: ResolvedWorkspace
    generation: int = 0
    turns: set[tuple[str, str]] = field(default_factory=set)
    uses: int = 0
    resources: set[str] = field(default_factory=set)
    closing: bool = False
    last_used: float = field(default_factory=time.monotonic)


class ToolServices:
    def __init__(self, config: ToolSystemConfig):
        self.config = config
        self.interactions = InteractionBroker(config.tools.interaction_capacity)
        self.grants = ApprovalGrantStore(config.approval_store_path)
        self.approvals = ApprovalGate(self.grants, config.approver)
        self._condition = threading.Condition(threading.RLock())
        self._sessions: dict[SessionResourceKey, _Session] = {}
        self._cancelled_owners: set[str] = set()
        self._providers: dict[str, Any] = {}
        self._provider_factories = {
            "browser": self._new_browser,
            "computer": self._new_computer,
        }
        self._search_backend = None
        self._http_backend = None
        self._closing = False
        self._closed = False
        self._cleanup_lock = threading.RLock()
        self._session_cleanup_lock = threading.Lock()
        self._cleanup_pool = DaemonWorkerPool(2, 128, name="tool-cleanup")
        self._cleanups: dict[tuple, Any] = {}
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._reap_loop, name="tool-janitor", daemon=True)
        self._janitor.start()

    def _new_browser(self):
        from .browser_backend import AgentBrowserBackend
        return AgentBrowserBackend(
            allow_private=self.config.tools.allow_private_browser_addresses,
            idle_seconds=self.config.tools.browser_idle_seconds,
        )

    def _new_computer(self):
        from .computer_backend import CuaCliBackend
        return CuaCliBackend(idle_seconds=self.config.tools.browser_idle_seconds)

    def available(self, name: str, *, refresh: bool = False) -> bool:
        with self._condition:
            if self._closing:
                return False
            if name in self._providers:
                return True
        if name == "browser":
            from .browser_backend import AgentBrowserBackend
            return AgentBrowserBackend.available()
        if name == "computer":
            from .computer_backend import CuaCliBackend
            return CuaCliBackend.available(
                refresh=refresh, ttl=self.config.tools.availability_ttl_seconds,
            )
        if name == "search":
            return self._search_backend is not None or importlib.util.find_spec("ddgs") is not None
        return name == "web_extract"

    def begin_turn(self, key: SessionResourceKey, owner_id: str, turn_id: str,
                   workspace_request=None) -> tuple[ResolvedWorkspace, int]:
        with self._condition:
            if self._closing or owner_id in self._cancelled_owners:
                raise ResourceUnavailableError("tool runtime is stopping")
            state = self._sessions.get(key)
            if state is not None and state.closing:
                raise ResourceUnavailableError("session resources are closing")
            workspace = resolve_workspace(
                workspace_request, state.workspace if state else None, self.config.workspace,
            )
            if state is None:
                state = self._sessions[key] = _Session(workspace)
            elif workspace != state.workspace:
                if state.turns or state.uses or state.resources:
                    raise SessionWorkspaceConflict("reset session resources before changing workspace")
                state.workspace = workspace
                state.generation += 1
            state.turns.add((owner_id, turn_id))
            state.last_used = time.monotonic()
            return workspace, state.generation

    def end_turn(self, owner_id: str, turn_id: str) -> None:
        self.interactions.cancel(owner_id=owner_id, turn_id=turn_id)
        with self._condition:
            for state in self._sessions.values():
                state.turns.discard((owner_id, turn_id))
            self._condition.notify_all()

    def cancel_owner(self, owner_id: str) -> None:
        with self._condition:
            self._cancelled_owners.add(owner_id)
        self.interactions.cancel(owner_id=owner_id)

    def check_use(self, use: ToolUse) -> None:
        with self._condition:
            state = self._sessions.get(use.key)
            if (self._closing or not use.active or state is None or state.closing
                    or state.generation != use.generation
                    or use.owner_id in self._cancelled_owners
                    or (use.owner_id, use.turn_id) not in state.turns):
                raise ResourceUnavailableError("tool context is no longer active")

    @contextmanager
    def tool_use(self, use: ToolUse):
        with self._condition:
            self.check_use(use)
            self._sessions[use.key].uses += 1
        try:
            yield
        finally:
            with self._condition:
                use.active = False
                state = self._sessions[use.key]
                state.uses -= 1
                state.last_used = time.monotonic()
                self._condition.notify_all()

    def _provider(self, name: str, use: ToolUse):
        use.check()
        with self._condition:
            self.check_use(use)
            provider = self._providers.get(name)
            if provider is None:
                provider = self._providers[name] = self._provider_factories[name]()
            self._sessions[use.key].resources.add(name)
            return provider

    def _cleanup(self, identity: tuple, callback, deadline: float) -> CleanupIssue | None:
        # Keep timed-out calls: another close joins the same operation instead
        # of concurrently closing a resource a second time.
        with self._cleanup_lock:
            future = self._cleanups.get(identity)
            if future is None:
                if remaining(deadline) == 0:
                    return CleanupIssue(*identity[:3], "budget_exhausted", True)
                future = self._cleanup_pool.submit(callback)
                self._cleanups[identity] = future
        try:
            future.result(timeout=remaining(deadline))
        except FutureTimeout:
            if future.done():
                with self._cleanup_lock:
                    self._cleanups.pop(identity, None)
            return CleanupIssue(*identity[:3], "cleanup_timeout", True)
        except Exception as exc:
            with self._cleanup_lock:
                self._cleanups.pop(identity, None)
            return CleanupIssue(*identity[:3], type(exc).__name__, isinstance(exc, TimeoutError))
        return None

    def reset_session_resources(self, key: SessionResourceKey, *, timeout: float = 5) -> CleanupReport:
        deadline = time.monotonic() + max(0, timeout)
        return self._close_session_resources(key, deadline=deadline, reset_binding=True)

    def _close_session_resources(self, key, *, deadline, reset_binding):
        if not self._session_cleanup_lock.acquire(timeout=remaining(deadline)):
            return CleanupReport((CleanupIssue("session", key.backend_id, "close", "cleanup_busy", True),))
        try:
            return self._close_session_resources_locked(key, deadline=deadline, reset_binding=reset_binding)
        finally:
            self._session_cleanup_lock.release()

    def _close_session_resources_locked(self, key, *, deadline, reset_binding):
        with self._condition:
            if self._closing:
                raise ResourceUnavailableError("tool runtime is stopping")
            state = self._sessions.get(key)
            if state is None:
                return CleanupReport()
            if state.turns or state.uses:
                raise SessionResourcesBusyError("session still has active turns or tool calls")
            state.closing = True
            providers = [(name, self._providers[name]) for name in sorted(state.resources)]
        self.interactions.cancel(key=key)
        issues = []
        for name, provider in providers:
            issue = self._cleanup(
                (name, key.backend_id, "close_session", state.generation),
                lambda p=provider: p.close_session(key, deadline=deadline),
                deadline,
            )
            if issue:
                issues.append(issue)
        if not issues:
            if reset_binding:
                self.grants.clear_session(key)
            with self._condition:
                # Keep generation monotonically increasing across resets.
                with self._cleanup_lock:
                    for name, _ in providers:
                        self._cleanups.pop((name, key.backend_id, "close_session", state.generation), None)
                state.generation += 1
                state.resources.clear()
                if reset_binding:
                    state.workspace = resolve_workspace(None, None, self.config.workspace)
                state.closing = False
                state.last_used = time.monotonic()
        return CleanupReport(tuple(issues))

    def close(self, *, timeout: float) -> CleanupReport:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            if self._closed:
                return CleanupReport()
            self._closing = True
            self._stop.set()
        issues = []
        if not self.interactions.close(remaining(deadline)):
            issues.append(CleanupIssue("broker", "process", "close", "notification_busy", True))
        if threading.current_thread() is not self._janitor:
            self._janitor.join(remaining(deadline))
            if self._janitor.is_alive():
                issues.append(CleanupIssue("janitor", "process", "close", "worker_busy", True))
        with self._condition:
            while any(s.turns or s.uses for s in self._sessions.values()) and remaining(deadline):
                self._condition.wait(remaining(deadline))
            busy = any(s.turns or s.uses for s in self._sessions.values())
        if busy:
            issues.append(CleanupIssue("sessions", "process", "close", "active_uses", True))
            return CleanupReport(tuple(issues))
        if not self._session_cleanup_lock.acquire(timeout=remaining(deadline)):
            issues.append(CleanupIssue("sessions", "process", "close", "cleanup_busy", True))
            return CleanupReport(tuple(issues))
        try:
            for name, provider in tuple(self._providers.items()):
                issue = self._cleanup(
                    (name, "process", "close"), lambda p=provider: p.close(deadline=deadline),
                    deadline,
                )
                if issue:
                    issues.append(issue)
        finally:
            self._session_cleanup_lock.release()
        if not issues:
            if not self._cleanup_pool.close(cancel_pending=False, timeout=remaining(deadline)):
                issues.append(CleanupIssue("cleanup", "process", "close", "worker_busy", True))
            else:
                with self._condition:
                    self._closed = True
        return CleanupReport(tuple(issues))

    def reap_idle(self, *, now: float | None = None, timeout: float = 1) -> CleanupReport:
        """Reclaim idle backends while retaining grants and workspace bindings."""
        now = time.monotonic() if now is None else now
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            if self._closing:
                return CleanupReport()
            keys = [
                key for key, state in self._sessions.items()
                if state.resources and not state.turns and not state.uses
                and now - state.last_used >= self.config.tools.browser_idle_seconds
            ]
        issues = []
        for key in keys:
            try:
                report = self._close_session_resources(
                    key, deadline=deadline, reset_binding=False,
                )
                issues.extend(report.issues)
            except SessionResourcesBusyError:
                continue
            except ResourceUnavailableError:
                break
        return CleanupReport(tuple(issues))

    def _reap_loop(self) -> None:
        interval = min(60.0, max(0.1, self.config.tools.browser_idle_seconds / 2))
        while not self._stop.wait(interval):
            try:
                report = self.reap_idle()
                for issue in report.issues:
                    logger.warning(
                        "tool.cleanup_failed resource=%s error_code=%s",
                        issue.resource, issue.error_code,
                    )
            except Exception as exc:
                logger.warning("tool.reap_failed error_type=%s", type(exc).__name__)
