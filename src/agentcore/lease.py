from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .models import LeaseFence, LeaseHandle, LeasePolicy
from .interfaces import TurnCancelled
from .storage import LeaseLostError, SQLiteStore


class LeaseAcquireTimeout(TimeoutError):
    pass


class TurnLeaseStore(Protocol):
    def acquire(
        self, session_id: str, owner_id: str, turn_id: str,
        ttl_seconds: float, wait_timeout: float,
    ) -> LeaseHandle: ...

    def renew(self, lease: LeaseHandle, ttl_seconds: float) -> LeaseHandle: ...

    def assert_current(self, lease: LeaseHandle) -> None: ...

    def release(self, lease: LeaseHandle) -> None: ...


class SQLiteTurnLeaseStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def acquire(
        self, session_id: str, owner_id: str, turn_id: str,
        ttl_seconds: float, wait_timeout: float,
    ) -> LeaseHandle:
        deadline = time.monotonic() + wait_timeout
        while True:
            lease = self._store.acquire_lease(
                session_id=session_id, owner_id=owner_id,
                turn_id=turn_id, ttl_seconds=ttl_seconds,
            )
            if lease is not None:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeaseAcquireTimeout(
                    f"timed out waiting for session lease: {session_id}"
                )
            time.sleep(min(0.05, remaining))

    def renew(self, lease: LeaseHandle, ttl_seconds: float) -> LeaseHandle:
        return self._store.renew_lease(lease, ttl_seconds=ttl_seconds)

    def assert_current(self, lease: LeaseHandle) -> None:
        self._store.assert_lease(lease)

    def release(self, lease: LeaseHandle) -> None:
        self._store.release_lease(lease)


@dataclass(slots=True)
class _HeartbeatEntry:
    lease: LeaseHandle
    lost_event: threading.Event
    next_renewal: float


class LeaseHeartbeatService:
    """One daemon thread renews all active leases independently of tool workers."""

    def __init__(
        self, store: TurnLeaseStore, *, ttl_seconds: float,
        interval_seconds: float,
        on_renewed: Callable[[LeaseHandle], None] | None = None,
        on_lost: Callable[[LeaseHandle, BaseException], None] | None = None,
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._interval = interval_seconds
        self._on_renewed = on_renewed
        self._on_lost = on_lost
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._entries: dict[tuple[str, str, int], _HeartbeatEntry] = {}
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="agentcore-lease-heartbeat", daemon=True
        )
        self._thread.start()

    @staticmethod
    def _key(lease: LeaseHandle) -> tuple[str, str, int]:
        return lease.session_id, lease.owner_id, lease.fencing_token

    def register(self, lease: LeaseHandle, lost_event: threading.Event) -> None:
        if self._closed.is_set():
            raise RuntimeError("lease heartbeat service is closed")
        self._commands.put(("register", (lease, lost_event)))
        self._wake.set()

    def unregister(self, lease: LeaseHandle) -> None:
        self._commands.put(("unregister", self._key(lease)))
        self._wake.set()

    def close(self, timeout: float) -> bool:
        if not self._closed.is_set():
            self._closed.set()
            self._commands.put(("stop", None))
            self._wake.set()
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._closed.is_set():
            self._wake.clear()
            self._drain_commands()
            now = time.monotonic()
            for key, entry in tuple(self._entries.items()):
                if entry.next_renewal > now:
                    continue
                try:
                    renewed = self._store.renew(entry.lease, self._ttl)
                except BaseException as exc:
                    entry.lost_event.set()
                    self._entries.pop(key, None)
                    if self._on_lost is not None:
                        try:
                            self._on_lost(entry.lease, exc)
                        except BaseException:
                            pass
                else:
                    entry.lease = renewed
                    entry.next_renewal = time.monotonic() + self._interval
                    if self._on_renewed is not None:
                        try:
                            self._on_renewed(renewed)
                        except BaseException:
                            pass
            next_renewal = min(
                (entry.next_renewal for entry in self._entries.values()),
                default=time.monotonic() + 0.25,
            )
            self._wake.wait(max(0, next_renewal - time.monotonic()))
        self._drain_commands()
        self._entries.clear()

    def _drain_commands(self) -> None:
        while True:
            try:
                command, value = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "register":
                lease, lost_event = value
                self._entries[self._key(lease)] = _HeartbeatEntry(
                    lease=lease, lost_event=lost_event,
                    next_renewal=time.monotonic() + self._interval,
                )
            elif command == "unregister":
                self._entries.pop(value, None)


class LeaseScope:
    """One turn's lease lifecycle, separate from history and persistence."""

    def __init__(
        self, *, store: TurnLeaseStore, heartbeat: LeaseHeartbeatService,
        session_id: str, owner_id: str, turn_id: str, policy: LeasePolicy,
    ) -> None:
        self._store, self._heartbeat, self._policy = store, heartbeat, policy
        self.session_id, self.owner_id, self.turn_id = session_id, owner_id, turn_id
        self._lease: LeaseHandle | None = None
        self._lost = threading.Event()
        self._closed = False
        self._started = False

    @property
    def fence(self) -> LeaseFence:
        if self._closed or self._lease is None:
            raise RuntimeError("lease scope has no write fence")
        return self._lease.fence

    def acquire(self, check_active: Callable[[], None]) -> None:
        if self._started or self._closed:
            raise RuntimeError("lease scope can only be acquired once")
        self._started = True
        deadline = time.monotonic() + self._policy.wait_timeout_seconds
        first_attempt = True
        while True:
            check_active()
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not first_attempt:
                raise LeaseAcquireTimeout(f"timed out waiting for session lease: {self.session_id}")
            try:
                self._lease = self._store.acquire(
                    self.session_id, self.owner_id, self.turn_id,
                    self._policy.ttl_seconds, max(0.0, min(0.1, remaining)),
                )
                break
            except LeaseAcquireTimeout:
                first_attempt = False
        self._heartbeat.register(self._lease, self._lost)

    def assert_current(self) -> None:
        if self._closed:
            raise TurnCancelled("lease scope is closed")
        if self._lost.is_set():
            raise LeaseLostError("session lease heartbeat was lost")
        if self._lease is not None:
            # Renewal changes the stored expiry; the original handle is only
            # an identity/fence, never authoritative evidence of validity.
            self._store.assert_current(self._lease)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._lease is not None:
            try:
                self._heartbeat.unregister(self._lease)
            finally:
                self._store.release(self._lease)


__all__ = [
    "LeaseAcquireTimeout", "LeaseScope",
    "LeaseHeartbeatService",
    "LeaseLostError",
    "SQLiteTurnLeaseStore",
    "TurnLeaseStore",
]
