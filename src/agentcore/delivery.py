from __future__ import annotations

import uuid
import threading
import time
import inspect
import logging
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Protocol

from .lease import LeaseAcquireTimeout, LeaseHeartbeatService, SQLiteTurnLeaseStore
from .logging_system import log_context
from .models import AgentConfig
from .observability import ObservabilityHub
from .storage import LeaseLostError, SQLiteStore
from .workers import DaemonWorkerPool


logger = logging.getLogger(__name__)


class DeliveryAdapter(Protocol):
    """Adapters must use obligation_id as an idempotency key."""

    destination: str

    def send(
        self, *, obligation_id: str, destination: str, text: str
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class DeliveryBatch:
    delivered: int = 0
    failed: int = 0


class DeliveryRuntime:
    """Replays stored replies without making another model request."""

    def __init__(
        self, store: SQLiteStore, adapter: DeliveryAdapter, *,
        lease_ttl_seconds: float = 30.0,
        observability: ObservabilityHub | None = None,
        queue_capacity: int = 128,
        log_config: AgentConfig | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self._leases = SQLiteTurnLeaseStore(store)
        self._ttl = lease_ttl_seconds
        self._owner_id = str(uuid.uuid4())
        self._heartbeat = LeaseHeartbeatService(
            self._leases, ttl_seconds=lease_ttl_seconds,
            interval_seconds=lease_ttl_seconds / 3,
        )
        self._observability = observability
        self._log_config = log_config
        self._pool = DaemonWorkerPool(
            1, queue_capacity, name="gateway-delivery"
        )

    def submit_drain(
        self, session_id: str | None = None
    ) -> Future[DeliveryBatch]:
        return self._pool.submit(lambda: self.drain(session_id))

    def close(self, timeout: float) -> bool:
        drained = self._pool.close(cancel_pending=False, timeout=timeout)
        heartbeat_stopped = self._heartbeat.close(timeout)
        return drained and heartbeat_stopped

    def drain(self, session_id: str | None = None) -> DeliveryBatch:
        delivered = failed = 0
        for obligation in self.store.list_pending_deliveries():
            if session_id is not None and obligation["session_id"] != session_id:
                continue
            if obligation["destination"] != self.adapter.destination:
                continue
            if self._deliver_one(obligation):
                delivered += 1
            else:
                failed += 1
        return DeliveryBatch(delivered, failed)

    def _deliver_one(self, obligation: dict[str, object]) -> bool:
        with log_context(self._log_config, str(obligation['session_id'])):
            return self._deliver_one_bound(obligation)

    def _deliver_one_bound(self, obligation: dict[str, object]) -> bool:
        session_id = str(obligation["session_id"])
        turn_id = str(obligation["turn_id"])
        obligation_id = str(obligation["id"])
        try:
            lease = self._leases.acquire(
                session_id, self._owner_id, f"delivery:{obligation_id}",
                self._ttl, 0.0,
            )
        except LeaseAcquireTimeout:
            return False
        lost_event = threading.Event()
        self._heartbeat.register(lease, lost_event)
        try:
            claimed = self.store.claim_delivery(
                obligation_id, fence=lease.fence
            )
            if claimed is None:
                return False
            if self._log_config is not None:
                logger.debug('delivery.attempted attempt=%s', int(claimed['attempts']) + 1)
            self._emit(
                "delivery.attempted", session_id, turn_id,
                {"attempt": int(claimed["attempts"]) + 1},
            )
            send_started = time.monotonic()
            try:
                if lost_event.is_set():
                    raise LeaseLostError("delivery lease was lost")
                self._leases.assert_current(lease)
                acknowledged = self.adapter.send(
                    obligation_id=obligation_id,
                    destination=str(claimed["destination"]),
                    text=str(claimed["content"]),
                )
                if inspect.isawaitable(acknowledged):
                    close = getattr(acknowledged, "close", None)
                    if close is not None:
                        close()
                    raise TypeError("delivery adapter send must be synchronous")
                if not isinstance(acknowledged, bool):
                    raise TypeError("delivery adapter must return a bool acknowledgement")
                error_type = None if acknowledged else "not_acknowledged"
            except BaseException as exc:
                acknowledged = False
                error_type = type(exc).__name__
            try:
                committed = self.store.finish_delivery(
                    obligation_id, delivered=acknowledged,
                    error_type=error_type, fence=lease.fence,
                )
            except LeaseLostError:
                return False
            if not committed:
                return False
            event = "delivery.delivered" if acknowledged else "delivery.failed"
            payload = {
                "duration_ms": int((time.monotonic() - send_started) * 1000),
                **({"error_type": error_type} if error_type else {}),
            }
            if self._log_config is not None:
                log_method = logger.info if acknowledged else logger.warning
                log_method('%s payload=%s', event, payload)
            self._emit(event, session_id, turn_id, payload)
            return acknowledged
        finally:
            self._heartbeat.unregister(lease)
            try:
                self._leases.release(lease)
            except LeaseLostError:
                pass

    def _emit(
        self, name: str, session_id: str, turn_id: str,
        payload: dict[str, object],
    ) -> None:
        if self._observability is not None:
            self._observability.publish(
                name, trace_id=turn_id, session_id=session_id,
                turn_id=turn_id, payload=payload,
            )


__all__ = ["DeliveryAdapter", "DeliveryBatch", "DeliveryRuntime"]

