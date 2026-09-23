from __future__ import annotations

import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar


T = TypeVar("T")
_STOP = object()


class WorkerPoolClosedError(RuntimeError):
    pass


class WorkerQueueFullError(RuntimeError):
    pass


class TurnQueueFullError(RuntimeError):
    pass


@dataclass(slots=True)
class _WorkItem(Generic[T]):
    future: Future[T]
    function: Callable[[], T]


class DaemonWorkerPool:
    """A bounded pool whose daemon threads cannot hold interpreter exit hostage."""

    def __init__(self, max_workers: int, capacity: int, *, name: str) -> None:
        self._capacity = capacity
        self._name = name
        self._queue: queue.Queue[_WorkItem[Any] | object] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._outstanding = 0
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{name}-{index + 1}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, function: Callable[[], T]) -> Future[T]:
        with self._lock:
            if self._closed:
                raise WorkerPoolClosedError(f"worker pool {self._name} is closed")
            if self._outstanding >= self._capacity:
                raise WorkerQueueFullError(f"worker pool {self._name} is saturated")
            self._outstanding += 1
        future: Future[T] = Future()
        self._queue.put(_WorkItem(future=future, function=function))
        return future

    def close(self, *, cancel_pending: bool, timeout: float) -> bool:
        with self._lock:
            if self._closed:
                return not any(thread.is_alive() for thread in self._threads)
            self._closed = True

        if cancel_pending:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, _WorkItem):
                    item.future.cancel()
                    with self._lock:
                        self._outstanding -= 1
                self._queue.task_done()

        for _ in self._threads:
            self._queue.put(_STOP)
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                assert isinstance(item, _WorkItem)
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    item.future.set_result(item.function())
                except BaseException as exc:
                    item.future.set_exception(exc)
            finally:
                if isinstance(item, _WorkItem):
                    with self._lock:
                        self._outstanding -= 1
                self._queue.task_done()


@dataclass(frozen=True, slots=True)
class TurnSubmission(Generic[T]):
    future: Future[T]
    cancel_event: threading.Event

    def cancel(self) -> None:
        self.cancel_event.set()


@dataclass(slots=True)
class _TurnJob(Generic[T]):
    session_id: str
    future: Future[T]
    cancel_event: threading.Event
    function: Callable[[threading.Event], T] | None


@dataclass(frozen=True, slots=True)
class TurnReservation(Generic[T]):
    """A queued session position that does not occupy a worker."""

    session_id: str
    _dispatcher: "TurnDispatcher"
    _job: _TurnJob[T]

    def commit(
        self, function: Callable[[threading.Event], T]
    ) -> TurnSubmission[T]:
        return self._dispatcher._commit_reserved(self._job, function)

    def cancel(self) -> None:
        self._dispatcher._cancel_reserved(self._job)


class TurnDispatcher:
    """FIFO lanes per session over a bounded daemon worker pool."""

    def __init__(self, max_workers: int, capacity: int) -> None:
        self._capacity = capacity
        self._pool = DaemonWorkerPool(max_workers, capacity, name="agentcore-turn")
        self._lock = threading.Lock()
        self._closed = False
        self._total = 0
        self._active_sessions: set[str] = set()
        self._blocked_sessions: set[str] = set()
        self._waiting: dict[str, deque[_TurnJob[Any]]] = {}
        self._jobs: dict[int, _TurnJob[Any]] = {}

    def submit(
        self,
        session_id: str,
        function: Callable[[threading.Event], T],
    ) -> TurnSubmission[T]:
        future: Future[T] = Future()
        job = _TurnJob(session_id, future, threading.Event(), function)
        dispatch = False
        with self._lock:
            if self._closed:
                raise WorkerPoolClosedError("turn dispatcher is closed")
            if self._total >= self._capacity:
                raise TurnQueueFullError("turn queue is full")
            self._total += 1
            self._jobs[id(job)] = job
            if session_id in self._active_sessions:
                self._waiting.setdefault(session_id, deque()).append(job)
            else:
                self._active_sessions.add(session_id)
                dispatch = True
        if dispatch:
            self._dispatch(job)
        return TurnSubmission(future=future, cancel_event=job.cancel_event)

    def reserve(self, session_id: str) -> TurnReservation[T]:
        """Synchronously hold FIFO position before building a recovery job."""
        job: _TurnJob[T] = _TurnJob(
            session_id, Future(), threading.Event(), None
        )
        with self._lock:
            if self._closed:
                raise WorkerPoolClosedError("turn dispatcher is closed")
            if self._total >= self._capacity:
                raise TurnQueueFullError("turn queue is full")
            self._total += 1
            self._jobs[id(job)] = job
            self._waiting.setdefault(session_id, deque()).append(job)
            if session_id not in self._active_sessions:
                self._active_sessions.add(session_id)
                self._blocked_sessions.add(session_id)
        return TurnReservation(session_id, self, job)

    def _commit_reserved(
        self, job: _TurnJob[T],
        function: Callable[[threading.Event], T],
    ) -> TurnSubmission[T]:
        dispatch = False
        with self._lock:
            if self._closed or id(job) not in self._jobs:
                raise WorkerPoolClosedError("turn reservation is no longer active")
            if job.function is not None:
                raise RuntimeError("turn reservation was already committed")
            job.function = function
            lane = self._waiting.get(job.session_id)
            if (
                job.session_id in self._blocked_sessions
                and lane and lane[0] is job
            ):
                lane.popleft()
                if not lane:
                    self._waiting.pop(job.session_id, None)
                self._blocked_sessions.discard(job.session_id)
                dispatch = True
        if dispatch:
            self._dispatch(job)
        return TurnSubmission(job.future, job.cancel_event)

    def _cancel_reserved(self, job: _TurnJob[Any]) -> None:
        next_job: _TurnJob[Any] | None = None
        with self._lock:
            if job.function is not None:
                raise RuntimeError("committed reservation cannot be cancelled")
            if self._jobs.pop(id(job), None) is None:
                return
            self._total -= 1
            job.future.cancel()
            lane = self._waiting.get(job.session_id)
            if lane:
                lane.remove(job)
                if not lane:
                    self._waiting.pop(job.session_id, None)
            if job.session_id in self._blocked_sessions:
                lane = self._waiting.get(job.session_id)
                if not lane:
                    self._blocked_sessions.discard(job.session_id)
                    self._active_sessions.discard(job.session_id)
                elif lane[0].function is not None:
                    next_job = lane.popleft()
                    if not lane:
                        self._waiting.pop(job.session_id, None)
                    self._blocked_sessions.discard(job.session_id)
        if next_job is not None:
            self._dispatch(next_job)

    def close(self, *, timeout: float) -> bool:
        with self._lock:
            if self._closed:
                return self._pool.close(cancel_pending=False, timeout=timeout)
            self._closed = True
            jobs = list(self._jobs.values())
            waiting = [job for lane in self._waiting.values() for job in lane]
            self._waiting.clear()
            for job in waiting:
                if job.future.cancel():
                    self._jobs.pop(id(job), None)
                    self._total -= 1
            for job in jobs:
                job.cancel_event.set()
        return self._pool.close(cancel_pending=False, timeout=timeout)

    def _dispatch(self, job: _TurnJob[Any]) -> None:
        try:
            self._pool.submit(lambda: self._run_job(job))
        except BaseException as exc:
            if not job.future.done():
                job.future.set_exception(exc)
            self._complete(job)

    def _run_job(self, job: _TurnJob[Any]) -> None:
        if not job.future.set_running_or_notify_cancel():
            self._complete(job)
            return
        try:
            assert job.function is not None
            job.future.set_result(job.function(job.cancel_event))
        except BaseException as exc:
            job.future.set_exception(exc)
        finally:
            self._complete(job)

    def _complete(self, job: _TurnJob[Any]) -> None:
        next_job: _TurnJob[Any] | None = None
        with self._lock:
            if self._jobs.pop(id(job), None) is not None:
                self._total -= 1
            lane = self._waiting.get(job.session_id)
            if lane and not self._closed:
                if lane[0].function is None:
                    self._blocked_sessions.add(job.session_id)
                else:
                    next_job = lane.popleft()
                    if not lane:
                        self._waiting.pop(job.session_id, None)
            else:
                self._active_sessions.discard(job.session_id)
                self._blocked_sessions.discard(job.session_id)
        if next_job is not None:
            self._dispatch(next_job)


def submit_daemon_call(function: Callable[[], T], *, name: str) -> Future[T]:
    future: Future[T] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future
