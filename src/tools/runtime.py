"""Explicit process initialization and ownership of the tool system."""

from __future__ import annotations

import atexit
import inspect
import logging
import threading
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ToolSystemConfig
from .lifecycle import CleanupIssue, CleanupReport, remaining
from .model_tools import discover_builtin_tools
from .registry import ToolRegistry

if TYPE_CHECKING:
    from .services import ToolServices


class ToolSystemState(Enum):
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"


class ToolSystemNotInitialized(RuntimeError):
    pass


class ToolSystemConfigConflict(ValueError):
    pass


_lock = threading.RLock()
_shutdown_lock = threading.Lock()
_state: ToolSystemState = ToolSystemState.UNINITIALIZED
_config: ToolSystemConfig | None = None
_registry: ToolRegistry | None = None
_services: ToolServices | None = None
_exit_registered = False
_startup_cwd: Path | None = None


def initialize_tool_system(config: ToolSystemConfig) -> None:
    global _state, _config, _registry, _services, _exit_registered, _startup_cwd
    with _lock:
        startup = _startup_cwd or Path.cwd().resolve()
        normalized = config.normalized(startup_cwd=startup)
        if _state is ToolSystemState.READY:
            if normalized != _config:
                raise ToolSystemConfigConflict("tool system already uses a different configuration")
            return
        if _state is not ToolSystemState.UNINITIALIZED:
            raise ToolSystemNotInitialized(f"tool system is {_state.value}")
        _state = ToolSystemState.INITIALIZING
        services = None
        try:
            if config.approver is not None:
                callback = config.approver.approve
                if not callable(callback) or inspect.iscoroutinefunction(callback):
                    raise TypeError("approval decision maker must be synchronous")
            registry = ToolRegistry()
            discover_builtin_tools(registry)
            from .services import ToolServices
            services = ToolServices(normalized)
            if any(server.enabled for server in normalized.tools.mcp_servers.values()):
                client = services.mcp()
                from .mcp_adapter import build_mcp_entries
                entries = build_mcp_entries(
                    client, normalized.tools.mcp_servers, client.discover(),
                    existing_entries=registry.entries(),
                )
                registry.replace_all((*registry.entries(), *entries))
        except BaseException as exc:
            try:
                report = services.close(timeout=5) if services is not None else CleanupReport()
            except Exception as cleanup_error:
                report = CleanupReport((CleanupIssue(
                    "runtime", "process", "close", type(cleanup_error).__name__,
                ),))
            if report.completed:
                _state = ToolSystemState.UNINITIALIZED
            else:
                # Retain the owner of unfinished cleanup; another init must not orphan it.
                _config, _services, _startup_cwd = normalized, services, startup
                _state = ToolSystemState.CLOSING
                exc.add_note(f"tool initialization cleanup incomplete: {report.issues}")
                if not _exit_registered:
                    atexit.register(_shutdown_at_exit)
                    _exit_registered = True
            raise
        _config, _registry, _services = normalized, registry, services
        _startup_cwd = startup
        _state = ToolSystemState.READY
        if not _exit_registered:
            atexit.register(_shutdown_at_exit)
            _exit_registered = True


def get_tool_registry() -> ToolRegistry:
    with _lock:
        if _state is not ToolSystemState.READY or _registry is None:
            raise ToolSystemNotInitialized("initialize_tool_system() must run before AgentCore")
        return _registry


def get_tool_services() -> ToolServices:
    with _lock:
        if _state is not ToolSystemState.READY or _services is None:
            raise ToolSystemNotInitialized("initialize_tool_system() must run before AgentCore")
        return _services


def validate_tool_config(config: ToolSystemConfig) -> None:
    with _lock:
        actual = config.normalized(startup_cwd=_startup_cwd)
        if _state is not ToolSystemState.READY:
            raise ToolSystemNotInitialized("initialize_tool_system() must run before AgentCore")
        # The decision maker is owned by process initialization, not AgentConfig.
        if _config is None or (actual.tools, actual.workspace, actual.approval_store_path) != (
            _config.tools, _config.workspace, _config.approval_store_path,
        ):
            raise ToolSystemConfigConflict("Agent configuration differs from the initialized tool system")


def shutdown_tool_system(*, timeout: float) -> CleanupReport:
    global _state
    deadline = time.monotonic() + max(0, timeout)
    if not _shutdown_lock.acquire(timeout=remaining(deadline)):
        return CleanupReport((CleanupIssue("runtime", "process", "close", "close_busy", True),))
    try:
        with _lock:
            if _state in {ToolSystemState.UNINITIALIZED, ToolSystemState.CLOSED}:
                return CleanupReport()
            _state = ToolSystemState.CLOSING
            services = _services
        report = services.close(timeout=remaining(deadline))
        with _lock:
            if report.completed:
                _state = ToolSystemState.CLOSED
        return report
    finally:
        _shutdown_lock.release()


def _shutdown_at_exit() -> None:
    # A zero budget never starts a new cleanup task/thread during finalization.
    try:
        report = shutdown_tool_system(timeout=0)
        if not report.completed:
            logging.getLogger(__name__).warning("tool.shutdown_incomplete issues=%s", len(report.issues))
    except Exception:
        logging.getLogger(__name__).exception("tool.shutdown_failed")
