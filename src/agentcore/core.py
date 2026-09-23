from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextvars import copy_context
from typing import Any, Callable, Sequence, TypeVar

from .builtins import default_tool_catalog
from .conversation_loop import ConversationLoop, RouteState
from .interfaces import (
    AllowAllGuard, Approver, MemoryProvider, MemoryUpdater,
    NoopMemoryProvider, NoopMemoryUpdater, NoopReviewScheduler,
    ReviewScheduler, ToolGuard, TurnCancelled,
)
from .lease import LeaseHeartbeatService, SQLiteTurnLeaseStore, TurnLeaseStore
from log.logging_system import log_context
from .models import AgentConfig, MemoryItem, TurnResult, TurnTrigger, UserInput
from .observability import EventSink, LifecycleObserver, ObservabilityHub
from .prompt_builder import StaticSystemPromptBuilder, SystemPromptBuilder
from .provider_runtime import (
    ApiModeRegistry, OpenAICompatibleProviderResolver, ProviderResolver,
    default_api_mode_registry,
)
from .session_context import SessionBuildRequest, SessionContextFactory
from .storage import SQLiteStore
from tools.tools import ToolCatalog, ToolSpec
from tools.toolsets import DefaultToolsetResolver, ToolsetResolver
from .turn_state import OperationTimeoutError, RunBudgetExceeded
from .workers import (
    DaemonWorkerPool, TurnDispatcher, TurnQueueFullError, TurnReservation,
    WorkerQueueFullError, submit_daemon_call,
)

T = TypeVar("T")


def _is_async_callable(callback: Any) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    )


class _BoundedMemoryProvider:
    def __init__(self, agent: "AgentCore", target: MemoryProvider) -> None:
        self._agent = agent
        self._target = target

    def recall(self, user_input: UserInput, session_id: str) -> Sequence[MemoryItem]:
        return self._agent.call_operation(
            lambda: self._target.recall(user_input, session_id),
            timeout=self._agent._config.timeouts.memory_seconds,
            cancel_event=None, deadline=None,
        )


logger = logging.getLogger(__name__)


class AgentCore:
    """Immutable facade over dispatcher workers and synchronous ConversationLoop."""

    def __init__(
        self,
        config: AgentConfig,
        tool_catalog: ToolCatalog | None = None,
        prompt_builder: SystemPromptBuilder | None = None,
        toolset_resolver: ToolsetResolver | None = None,
        provider_resolver: ProviderResolver | None = None,
        api_modes: ApiModeRegistry | None = None,
        memory: MemoryProvider | None = None,
        memory_updater: MemoryUpdater | None = None,
        guard: ToolGuard | None = None,
        approver: Approver | None = None,
        observers: Sequence[LifecycleObserver] = (),
        event_sinks: Sequence[EventSink] = (),
        review_scheduler: ReviewScheduler | None = None,
        store: SQLiteStore | None = None,
        lease_store: TurnLeaseStore | None = None,
        delivery_destination: str | None = None,
        max_recovery_attempts: int = 3,
    ) -> None:
        if max_recovery_attempts < 1:
            raise ValueError("max_recovery_attempts must be positive")
        self._delivery_destination = delivery_destination
        self._max_recovery_attempts = max_recovery_attempts
        self._config = config
        self._catalog = tool_catalog or default_tool_catalog()
        self._catalog.freeze()
        self._prompt_builder = prompt_builder or StaticSystemPromptBuilder(
            config.system_prompt
        )
        self._toolset_resolver = toolset_resolver or DefaultToolsetResolver()
        self._provider_resolver = (
            provider_resolver or OpenAICompatibleProviderResolver()
        )
        self._api_modes = api_modes or default_api_mode_registry()
        self._api_modes.freeze()
        self._memory = memory or NoopMemoryProvider()
        self._memory_updater = memory_updater or NoopMemoryUpdater()
        self._guard = guard or AllowAllGuard()
        self._approver = approver
        self._review_scheduler = review_scheduler or NoopReviewScheduler()
        self._store = store or SQLiteStore(
            config.database_path,
            tool_result_max_chars=config.tool_result_max_chars,
        )
        self._lease_store = lease_store or SQLiteTurnLeaseStore(self._store)
        self._validate_sync_extensions()
        self._validate_provider_routes()
        self._operation_pool = DaemonWorkerPool(
            config.workers.operation_workers,
            config.workers.queue_capacity,
            name="agentcore-operation",
        )
        self._observability = ObservabilityHub(
            observers=tuple(observers), sinks=tuple(event_sinks),
            capacity=config.workers.queue_capacity,
            observer_timeout=config.timeouts.observer_seconds,
        )
        self._bounded_memory = _BoundedMemoryProvider(self, self._memory)
        self._session_contexts = SessionContextFactory(
            store=self._store, prompt_builder=self._prompt_builder,
            toolset_resolver=self._toolset_resolver, catalog=self._catalog,
            memory=self._bounded_memory,
            defaults=SessionBuildRequest.from_defaults(config.session_defaults),
            memory_max_chars=config.memory_max_chars,
        )
        self._owner_id = str(uuid.uuid4())

        def on_lease_renewed(lease) -> None:
            with log_context(config, lease.session_id):
                logger.debug('lease.renewed fencing_token=%s', lease.fencing_token)
            self._observability.publish(
                "lease.renewed", trace_id=lease.turn_id,
                session_id=lease.session_id, turn_id=lease.turn_id,
                payload={"fencing_token": lease.fencing_token},
            )

        def on_lease_lost(lease, exc) -> None:
            with log_context(config, lease.session_id):
                logger.error('lease.lost error_type=%s', type(exc).__name__)
            self._observability.publish(
                "lease.lost", trace_id=lease.turn_id,
                session_id=lease.session_id, turn_id=lease.turn_id,
                payload={"error_type": type(exc).__name__},
            )

        self._heartbeat = LeaseHeartbeatService(
            self._lease_store, ttl_seconds=config.lease.ttl_seconds,
            interval_seconds=config.lease.renew_interval_seconds,
            on_renewed=on_lease_renewed, on_lost=on_lease_lost,
        )
        self._route_states = tuple(RouteState() for _ in config.routes)
        self._dispatcher = TurnDispatcher(
            config.workers.turn_workers, config.workers.queue_capacity
        )
        self._lifecycle_lock = threading.RLock()
        self._started = False
        self._closed = False

    def update_tools(self, specs: Sequence[ToolSpec[Any]]) -> None:
        """Publish a new catalog for future turns without mutating active snapshots."""
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
            self._catalog.replace_all(tuple(specs))

    def _validate_sync_extensions(self) -> None:
        targets: list[tuple[Any, str, str]] = [
            (self._prompt_builder, "build", "SystemPromptBuilder"),
            (self._toolset_resolver, "resolve", "ToolsetResolver"),
            (self._memory, "recall", "MemoryProvider"),
            (self._memory_updater, "update", "MemoryUpdater"),
            (self._guard, "check", "ToolGuard"),
            (self._review_scheduler, "schedule", "ReviewScheduler"),
            (self._provider_resolver, "resolve", "ProviderResolver"),
            (self._provider_resolver, "close", "ProviderResolver"),
            (self._lease_store, "acquire", "TurnLeaseStore"),
            (self._lease_store, "renew", "TurnLeaseStore"),
            (self._lease_store, "assert_current", "TurnLeaseStore"),
            (self._lease_store, "release", "TurnLeaseStore"),
        ]
        if self._approver is not None:
            targets.append((self._approver, "approve", "Approver"))
        for target, method, label in targets:
            callback = getattr(target, method, None)
            if callback is None or not callable(callback):
                raise TypeError(f"{label} must define {method}()")
            if _is_async_callable(callback):
                raise TypeError(f"{label}.{method} must be synchronous")

    def _validate_provider_routes(self) -> None:
        try:
            for route in self._config.routes:
                adapter = self._api_modes.get(route.api_mode)
                runtime = self._provider_resolver.resolve(route)
                if route.api_mode not in runtime.capabilities:
                    raise ValueError(
                        f"provider {route.provider!r} does not support "
                        f"{route.api_mode!r}"
                    )
                for target, method, label in (
                    (adapter, "encode", "ApiModeAdapter"),
                    (adapter, "decode", "ApiModeAdapter"),
                    (adapter, "normalize_error", "ApiModeAdapter"),
                    (runtime.transport, "send", "Transport"),
                    (runtime.transport, "close", "Transport"),
                ):
                    callback = getattr(target, method, None)
                    if callback is None or not callable(callback):
                        raise TypeError(f"{label} must define {method}()")
                    if _is_async_callable(callback):
                        raise TypeError(f"{label}.{method} must be synchronous")
        except BaseException:
            self._provider_resolver.close()
            raise

    def __enter__(self) -> "AgentCore":
        self.start_blocking()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close_blocking()

    async def __aenter__(self) -> "AgentCore":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    def start_blocking(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
            if self._started:
                return
            self._store.open()
            self._started = True

    async def start(self) -> None:
        future = submit_daemon_call(self.start_blocking, name="agentcore-start")
        await asyncio.shield(asyncio.wrap_future(future))

    def close_blocking(self) -> None:
        with log_context(self._config):
            self._close_blocking_bound()

    def _close_blocking_bound(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        logger.info('runtime.shutdown_started')
        self._observability.publish("runtime.shutdown_started", trace_id="runtime")
        timeout = self._config.timeouts.finalize_seconds
        drained = self._dispatcher.close(timeout=timeout)
        heartbeat_stopped = self._heartbeat.close(timeout)
        operations_stopped = self._operation_pool.close(
            cancel_pending=True, timeout=timeout
        )
        try:
            self._provider_resolver.close()
        except BaseException as exc:
            logger.error('runtime.provider_close_failed error_type=%s', type(exc).__name__)
            self._observability.publish(
                "runtime.provider_close_failed", trace_id="runtime",
                payload={"error_type": type(exc).__name__},
            )
        if self._started:
            self._store.close()
        if not (drained and heartbeat_stopped and operations_stopped):
            logger.warning('runtime.shutdown_incomplete')
            self._observability.publish(
                "runtime.shutdown_incomplete", trace_id="runtime"
            )
        else:
            logger.info('runtime.closed')
            self._observability.publish("runtime.closed", trace_id="runtime")
        warnings = self._observability.close(timeout)
        if warnings:
            logger.warning('runtime.observability_warning warning_count=%s', len(warnings))

    async def aclose(self) -> None:
        future = submit_daemon_call(self.close_blocking, name="agentcore-close")
        await asyncio.shield(asyncio.wrap_future(future))

    async def run_turn(
        self, user_input: UserInput | str, session_id: str | None = None, *,
        session_request: SessionBuildRequest | None = None,
    ) -> TurnResult:
        submission = self._submit_turn(
            user_input, session_id, session_request=session_request
        )
        try:
            return await asyncio.shield(asyncio.wrap_future(submission.future))
        except asyncio.CancelledError:
            submission.cancel()
            raise

    def run_turn_blocking(
        self, user_input: UserInput | str, session_id: str | None = None, *,
        session_request: SessionBuildRequest | None = None,
    ) -> TurnResult:
        submission = self._submit_turn(
            user_input, session_id, session_request=session_request
        )
        try:
            return submission.future.result()
        except KeyboardInterrupt:
            submission.cancel()
            raise

    def _submit_turn(
        self, user_input: UserInput | str, session_id: str | None, *,
        session_request: SessionBuildRequest | None,
    ):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        normalized = UserInput.normalize(user_input)
        effective_session_id = session_id or str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        try:
            return self._dispatcher.submit(
                effective_session_id,
                lambda cancel_event: self._run_submitted_turn(
                    normalized, effective_session_id, turn_id,
                    session_request, cancel_event,
                ),
            )
        except TurnQueueFullError:
            with log_context(self._config, effective_session_id):
                logger.warning('runtime.queue_saturated')
            self._observability.publish(
                "runtime.queue_saturated", trace_id="runtime",
                session_id=effective_session_id, turn_id=turn_id,
            )
            raise

    def reserve_recovery(self, session_id: str) -> TurnReservation[TurnResult]:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        return self._dispatcher.reserve(session_id)

    def submit_recovery(
        self, trigger: TurnTrigger,
        reservation: TurnReservation[TurnResult] | None = None,
    ):
        """Submit an internal recovery turn to the same per-session FIFO lane."""
        if trigger.kind != "internal_recovery":
            raise ValueError("submit_recovery requires an internal recovery trigger")
        session_id = trigger.session_id
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("AgentCore is closed")
        turn_id = str(uuid.uuid4())
        function = lambda cancel_event: self._run_submitted_turn(
            UserInput(text="Internal recovery"), session_id, turn_id,
            None, cancel_event, recovery=True,
        )
        if reservation is not None:
            if reservation.session_id != session_id:
                raise ValueError("recovery reservation belongs to another session")
            return reservation.commit(function)
        return self._dispatcher.submit(session_id, function)

    def _run_submitted_turn(
        self, user_input: UserInput, session_id: str, turn_id: str,
        session_request: SessionBuildRequest | None,
        cancel_event: threading.Event, recovery: bool = False,
    ) -> TurnResult:
        self.start_blocking()
        return ConversationLoop(
            config=self._config, user_input=user_input,
            session_id=session_id, turn_id=turn_id,
            owner_id=self._owner_id, session_request=session_request,
            cancel_event=cancel_event, store=self._store,
            session_contexts=self._session_contexts,
            lease_store=self._lease_store, heartbeat=self._heartbeat,
            provider_resolver=self._provider_resolver,
            api_modes=self._api_modes, route_states=self._route_states,
            guard=self._guard, approver=self._approver,
            memory_updater=self._memory_updater,
            review_scheduler=self._review_scheduler,
            observability=self._observability,
            call_operation=self.call_operation,
            recovery=recovery,
            delivery_destination=self._delivery_destination,
            max_recovery_attempts=self._max_recovery_attempts,
        ).run()

    def call_operation(
        self, function: Callable[[], T], *, timeout: float,
        cancel_event: threading.Event | None, deadline: float | None,
    ) -> T:
        context = copy_context()
        future = self._operation_pool.submit(lambda: context.run(function))
        started = time.monotonic()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                future.cancel()
                raise TurnCancelled("turn was cancelled")
            remaining = timeout - (time.monotonic() - started)
            if deadline is not None:
                budget_remaining = deadline - time.monotonic()
                if budget_remaining <= 0:
                    future.cancel()
                    raise RunBudgetExceeded("turn run budget was exhausted")
                remaining = min(remaining, budget_remaining)
            if remaining <= 0:
                future.cancel()
                raise OperationTimeoutError(f"operation exceeded {timeout:g}s")
            try:
                result = future.result(timeout=min(0.05, remaining))
            except FutureTimeoutError:
                continue
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if close is not None:
                    close()
                raise TypeError("synchronous extension returned an awaitable")
            return result


__all__ = [
    "AgentCore",
    "TurnQueueFullError",
    "WorkerQueueFullError",
]
