from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .models import LeaseHandle
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

    def unregister(self, lease: LeaseHandle) -> None:
        self._commands.put(("unregister", self._key(lease)))

    def close(self, timeout: float) -> bool:
        if self._closed.is_set():
            return not self._thread.is_alive()
        self._closed.set()
        self._commands.put(("stop", None))
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._closed.is_set():
            self._drain_commands()
            now = time.monotonic()
            next_wait = 0.25
            for key, entry in tuple(self._entries.items()):
                if entry.next_renewal > now:
                    next_wait = min(next_wait, entry.next_renewal - now)
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
                    entry.next_renewal = now + self._interval
                    if self._on_renewed is not None:
                        try:
                            self._on_renewed(renewed)
                        except BaseException:
                            pass
            self._closed.wait(max(0.01, next_wait))
        self._drain_commands()

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


__all__ = [
    "LeaseAcquireTimeout",
    "LeaseHeartbeatService",
    "LeaseLostError",
    "SQLiteTurnLeaseStore",
    "TurnLeaseStore",
]
