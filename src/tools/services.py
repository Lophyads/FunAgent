"""Process dependencies and bounded cleanup; backends own their session state."""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from agentcore.approval import ApprovalGate, ApprovalGrantStore
from agentcore.interaction import InteractionBroker
from agentcore.workers import DaemonWorkerPool
from .config import ToolSystemConfig
from .lifecycle import (
    CleanupIssue, CleanupReport, ResourceUnavailableError, SessionResourceKey,
    remaining,
)

if TYPE_CHECKING:
    from .lifecycle import ToolResourceProvider

logger = logging.getLogger(__name__)


class ToolServices:
    def __init__(
        self, config: ToolSystemConfig, *,
        provider_factories: Mapping[str, Callable[[], ToolResourceProvider]] | None = None,
    ):
        self.config = config
        self.interactions = InteractionBroker(config.tools.interaction_capacity)
        self.grants = ApprovalGrantStore(config.approval_store_path)
        self.approvals = ApprovalGate(self.grants, config.approver)
        self._lock = threading.RLock()
        self._providers: dict[str, Any] = {}
        self._protected_paths: set[Path] = set()
        self._provider_factories = {
            "browser": self._new_browser,
            "computer": self._new_computer,
        }
        self._provided_backends = frozenset(provider_factories or ())
        if provider_factories is not None:
            self._provider_factories.update(provider_factories)
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
        with self._lock:
            if self._closing:
                return False
            if name in self._providers or name in self._provided_backends:
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

    def check_open(self) -> None:
        with self._lock:
            if self._closing:
                raise ResourceUnavailableError("tool runtime is stopping")

    def protect_path(self, path: Path) -> None:
        with self._lock:
            self.check_open()
            self._protected_paths.add(Path(path).resolve())

    def unprotect_path(self, path: Path) -> None:
        with self._lock:
            self._protected_paths.discard(Path(path).resolve())

    def is_protected_path(self, path: Path) -> bool:
        candidate = Path(path).resolve()
        with self._lock:
            return any(candidate == root or candidate.is_relative_to(root)
                       for root in self._protected_paths)

    def _provider(self, name: str):
        with self._lock:
            self.check_open()
            provider = self._providers.get(name)
            if provider is None:
                provider = self._providers[name] = self._provider_factories[name]()
            return provider

    def browser(self):
        return self._provider("browser")

    def computer(self):
        return self._provider("computer")

    def search(self, query, limit, context):
        context.check_active()
        if self._search_backend is not None:
            return self._search_backend.search(query, limit, context)
        from .web_tool import _search_subprocess
        return _search_subprocess(query, limit, context, self.config.tools.search_timeout_seconds)

    def fetch(self, url, context):
        context.check_active()
        if self._http_backend is not None:
            return self._http_backend.get(url)
        from .web_tool import _fetch_http
        return _fetch_http(url, allow_private=self.config.tools.allow_private_web_addresses,
                           max_bytes=self.config.tools.artifact_max_bytes)

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
        with self._cleanup_lock:
            self._cleanups.pop(identity, None)
        return None

    def reset_session_resources(self, key: SessionResourceKey, *, timeout: float = 5) -> CleanupReport:
        """Close this key in initialized backends; caller quiesces session submissions."""
        deadline = time.monotonic() + max(0, timeout)
        if not self._session_cleanup_lock.acquire(timeout=remaining(deadline)):
            return CleanupReport((CleanupIssue("session", key.session_id, "close", "cleanup_busy", True),))
        try:
            with self._lock:
                self.check_open()
                providers = tuple(self._providers.items())
            self.interactions.cancel(key=key)
            issues = []
            for name, provider in providers:
                issue = self._cleanup(
                    (name, key.session_id, "close_session"),
                    lambda p=provider: p.close_session(key, deadline=deadline), deadline,
                )
                if issue:
                    issues.append(issue)
            if not issues:
                self.grants.clear_session(key)
            return CleanupReport(tuple(issues))
        finally:
            self._session_cleanup_lock.release()

    def close(self, *, timeout: float) -> CleanupReport:
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
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
                with self._lock:
                    self._closed = True
        return CleanupReport(tuple(issues))

    def reap_idle(self, *, now: float | None = None, timeout: float = 1) -> CleanupReport:
        """Ask initialized backends to reap their own idle resources."""
        now = time.monotonic() if now is None else now
        deadline = time.monotonic() + max(0, timeout)
        if not self._session_cleanup_lock.acquire(timeout=remaining(deadline)):
            return CleanupReport((CleanupIssue("backends", "process", "reap", "cleanup_busy", True),))
        try:
            with self._lock:
                if self._closing:
                    return CleanupReport()
                providers = tuple(self._providers.items())
            issues = []
            for name, provider in providers:
                issue = self._cleanup(
                    (name, "idle", "reap"),
                    lambda p=provider: p.reap_idle(now=now, deadline=deadline), deadline,
                )
                if issue:
                    issues.append(issue)
            return CleanupReport(tuple(issues))
        finally:
            self._session_cleanup_lock.release()

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
