from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from typing import Any

from agentcore.core import AgentCore
from .delivery import DeliveryAdapter, DeliveryRuntime
from log.logging_system import LoggingHandle, configure_logging, log_context
from agentcore.models import AgentConfig, TurnResult, TurnTrigger, UserInput
from agentcore.workers import TurnQueueFullError, TurnSubmission, submit_daemon_call


logger = logging.getLogger(__name__)


class GatewayRuntime:
    """Owns startup recovery, inbound admission, and reply delivery."""

    def __init__(
        self, config: AgentConfig, adapter: DeliveryAdapter, *,
        max_recovery_attempts: int = 3, **agent_options: Any,
    ) -> None:
        if not adapter.destination.strip():
            raise ValueError("adapter destination must not be blank")
        for method in ("send", "start", "close"):
            callback = getattr(adapter, method, None)
            if callback is not None and inspect.iscoroutinefunction(callback):
                raise TypeError(f"delivery adapter {method} must be synchronous")
        if "delivery_destination" in agent_options:
            raise TypeError("delivery_destination is owned by GatewayRuntime")
        self.adapter = adapter
        self.agent = AgentCore(
            config, delivery_destination=adapter.destination,
            max_recovery_attempts=max_recovery_attempts, **agent_options,
        )
        self.delivery = DeliveryRuntime(
            self.agent._store, adapter,
            lease_ttl_seconds=config.lease.ttl_seconds,
            observability=self.agent._observability,
            queue_capacity=config.workers.queue_capacity,
            log_config=config,
        )
        self._max_recovery_attempts = max_recovery_attempts
        self._runtime_id = str(uuid.uuid4())
        self.last_shutdown_clean: bool | None = None
        self._lock = threading.RLock()
        self._started = False
        self._closed = False
        self._log_handle: LoggingHandle | None = None
        self._inbound_open = False
        self._recovery_submissions: list[TurnSubmission[TurnResult]] = []
        self._scheduled_sessions: set[str] = set()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._monitor_interval = min(
            1.0, max(0.05, config.lease.ttl_seconds / 3)
        )

    def __enter__(self) -> "GatewayRuntime":
        self.start_blocking()
        return self

    def __exit__(self, *args: object) -> None:
        self.close_blocking()

    async def __aenter__(self) -> "GatewayRuntime":
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def start_blocking(self) -> None:
        with log_context(self.agent._config):
            self._start_blocking_bound()

    def _start_blocking_bound(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("GatewayRuntime is closed")
            if self._started:
                return
            if self._log_handle is None:
                self._log_handle = configure_logging(
                    self.agent._config, mode="gateway"
                )
        try:
            self._start_blocking_impl()
        except BaseException as exc:
            logger.error('runtime.startup_failed error_type=%s', type(exc).__name__)
            self.agent._observability.publish(
                "runtime.startup_failed", trace_id="runtime",
                payload={"error_type": type(exc).__name__},
            )
            if self._log_handle is not None:
                self._log_handle.close(
                    timeout=self.agent._config.timeouts.finalize_seconds
                )
                self._log_handle = None
            raise

    def _start_blocking_impl(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("GatewayRuntime is closed")
            if self._started:
                return
            self.agent.start_blocking()
            self.last_shutdown_clean = (
                self.agent._store.record_runtime_start(self._runtime_id)
            )
            logger.info('runtime.startup_started')
            self.agent._observability.publish(
                "runtime.startup_started", trace_id="runtime"
            )
            adapter_start = getattr(self.adapter, "start", None)
            if adapter_start is not None:
                readiness = adapter_start()
                if inspect.isawaitable(readiness):
                    close = getattr(readiness, "close", None)
                    if close is not None:
                        close()
                    raise TypeError("delivery adapter start must be synchronous")
                if readiness is False:
                    raise RuntimeError("delivery adapter is not ready")
            converted = self.agent._store.convert_stale_active_turns()
            if converted:
                logger.warning('recovery.interrupted_turns_converted session_count=%s', len(converted))
            self.agent._observability.publish(
                "recovery.interrupted_turns_converted", trace_id="runtime",
                payload={"session_count": len(converted)},
            )
            self.delivery.drain()
            pending = self.agent._store.list_pending_recoveries(
                self._max_recovery_attempts
            )
            for session_id in pending:
                while not self._schedule_recovery(session_id):
                    oldest = next(
                        (item for item in self._recovery_submissions
                         if not item.future.done()),
                        None,
                    )
                    if oldest is None:
                        raise TurnQueueFullError(
                            "startup recovery could not reserve a session slot"
                        )
                    try:
                        oldest.future.result()
                    except BaseException:
                        pass
            self._inbound_open = True
            self._started = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_recovery,
                name="gateway-recovery-monitor", daemon=True,
            )
            self._monitor_thread.start()
            logger.info('runtime.ready recovery_count=%s', len(pending))
            self.agent._observability.publish(
                "runtime.ready", trace_id="runtime",
                payload={"recovery_count": len(pending)},
            )

    async def start(self) -> None:
        future = submit_daemon_call(
            self.start_blocking, name="gateway-start"
        )
        await asyncio.shield(asyncio.wrap_future(future))

    def _schedule_recovery(self, session_id: str) -> bool:
        with log_context(self.agent._config, session_id):
            return self._schedule_recovery_bound(session_id)

    def _schedule_recovery_bound(self, session_id: str) -> bool:
        with self._lock:
            if self._closed or session_id in self._scheduled_sessions:
                return True
            try:
                reservation = self.agent.reserve_recovery(session_id)
            except TurnQueueFullError:
                return False
            committed = False
            try:
                lifecycle = self.agent._store.get_session_lifecycle(session_id)
                trigger = TurnTrigger(
                    kind="internal_recovery", session_id=session_id,
                    recovery_reason=(
                        lifecycle["resume_reason"] if lifecycle else None
                    ),
                    source_turn_id=(
                        lifecycle["resume_source_turn_id"] if lifecycle else None
                    ),
                )
                submission = self.agent.submit_recovery(
                    trigger, reservation=reservation
                )
                committed = True
                submission.future.add_done_callback(
                    lambda _future, sid=session_id: self._queue_delivery(sid)
                )
                self._recovery_submissions.append(submission)
                self._scheduled_sessions.add(session_id)
                logger.info('recovery.scheduled')
                self.agent._observability.publish(
                    "recovery.scheduled", trace_id="runtime",
                    session_id=session_id,
                )
                return True
            except BaseException:
                if not committed:
                    reservation.cancel()
                raise

    def _monitor_recovery(self) -> None:
        with log_context(self.agent._config):
            self._monitor_recovery_bound()

    def _monitor_recovery_bound(self) -> None:
        while not self._monitor_stop.wait(self._monitor_interval):
            try:
                converted = self.agent._store.convert_stale_active_turns()
                if converted:
                    logger.warning('recovery.interrupted_turns_converted session_count=%s', len(converted))
                    self.agent._observability.publish(
                        "recovery.interrupted_turns_converted",
                        trace_id="runtime",
                        payload={"session_count": len(converted)},
                    )
                pending = self.agent._store.list_pending_recoveries(
                    self._max_recovery_attempts
                )
                for session_id in pending:
                    self._schedule_recovery(session_id)
            except Exception as exc:
                logger.error('recovery.monitor_failed error_type=%s', type(exc).__name__)
                self.agent._observability.publish(
                    "recovery.monitor_failed", trace_id="runtime",
                    payload={"error_type": type(exc).__name__},
                )

    def _queue_delivery(self, session_id: str) -> None:
        try:
            self.delivery.submit_drain(session_id)
        except RuntimeError:
            # The obligation stays in SQLite for the next startup.
            pass

    def _check_inbound(self) -> None:
        with self._lock:
            if self._closed or not self._inbound_open:
                raise RuntimeError("GatewayRuntime is not accepting turns")

    def run_turn_blocking(
        self, user_input: UserInput | str, session_id: str | None = None
    ) -> TurnResult:
        self.start_blocking()
        self._check_inbound()
        result = self.agent.run_turn_blocking(user_input, session_id)
        try:
            self.delivery.submit_drain(result.session_id).result()
        except Exception:
            # Persisted obligation remains available for replay.
            pass
        return result

    async def run_turn(
        self, user_input: UserInput | str, session_id: str | None = None
    ) -> TurnResult:
        await self.start()
        self._check_inbound()
        result = await self.agent.run_turn(user_input, session_id)
        try:
            future = self.delivery.submit_drain(result.session_id)
            await asyncio.shield(asyncio.wrap_future(future))
        except Exception:
            # Delivery failure never changes the completed model turn.
            pass
        return result

    def wait_for_recovery(self) -> list[TurnResult]:
        """Wait for recovery turns already scheduled at the time of this call."""
        with self._lock:
            submissions = tuple(self._recovery_submissions)
        results = []
        for submission in submissions:
            results.append(submission.future.result())
        return results

    def close_blocking(self) -> None:
        with log_context(self.agent._config):
            self._close_blocking_bound()

    def _close_blocking_bound(self) -> None:
        try:
            self._close_blocking_impl()
        finally:
            if self._log_handle is not None:
                self._log_handle.close(
                    timeout=self.agent._config.timeouts.finalize_seconds
                )
                self._log_handle = None

    def _close_blocking_impl(self) -> None:
        self._monitor_stop.set()
        with self._lock:
            if self._closed:
                return
            self._inbound_open = False
            self._closed = True
        timeout = self.agent._config.timeouts.finalize_seconds
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout)
        turns_drained = self.agent._dispatcher.close(timeout=timeout)
        delivery_drained = self.delivery.close(timeout)
        try:
            if self._started and turns_drained and delivery_drained:
                self.agent._store.mark_runtime_clean(self._runtime_id)
        except Exception as exc:
            logger.warning('runtime.shutdown_incomplete error_type=%s', type(exc).__name__)
            self.agent._observability.publish(
                "runtime.shutdown_incomplete", trace_id="runtime",
                payload={"error_type": type(exc).__name__},
            )
        finally:
            self.agent.close_blocking()
        adapter_close = getattr(self.adapter, "close", None)
        if adapter_close is not None:
            try:
                adapter_close()
            except Exception:
                pass

    async def aclose(self) -> None:
        future = submit_daemon_call(
            self.close_blocking, name="gateway-close"
        )
        await asyncio.shield(asyncio.wrap_future(future))


__all__ = ["GatewayRuntime"]

