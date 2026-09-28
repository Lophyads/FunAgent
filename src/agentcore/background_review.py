"""Bounded, per-session background reviews over the existing turn dispatcher."""

from __future__ import annotations

import inspect
import logging
import threading
import uuid
from contextvars import copy_context
from dataclasses import dataclass
from typing import Callable

from .interfaces import TurnCancelled, TurnSnapshot
from .observability import ObservabilityHub
from .workers import TurnDispatcher, TurnSubmission

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewTask:
    turn_id: str
    snapshot: TurnSnapshot
    cancel_event: threading.Event

    @property
    def parent_trace_id(self) -> str:
        return self.snapshot.trace_id or self.snapshot.result.turn_id


class BackgroundReviewScheduler:
    """Enqueue without waiting for review; serialize reviews of the same chat.

    The creator owns this scheduler and must close it before closing clients
    used by its reviewer. close() cancels queued work and signals running work;
    False means a reviewer has not yet returned. This queue is process-local.
    """

    def __init__(
        self, reviewer: Callable[[ReviewTask], None], *,
        workers: int = 1, capacity: int = 32,
        observability: ObservabilityHub | None = None,
    ) -> None:
        if workers < 1 or capacity < 1:
            raise ValueError("review workers and capacity must be positive")
        if (not callable(reviewer) or inspect.iscoroutinefunction(reviewer)
                or inspect.iscoroutinefunction(getattr(reviewer, "__call__", None))):
            raise TypeError("reviewer must be a synchronous callable")
        self._reviewer = reviewer
        self._observability = observability
        self._dispatcher: TurnDispatcher[None] = TurnDispatcher(workers, capacity)

    def schedule(self, snapshot: TurnSnapshot) -> None:
        self.submit(snapshot)

    def submit(self, snapshot: TurnSnapshot) -> TurnSubmission[None]:
        if snapshot.context is None:
            raise ValueError("background review requires the original model context")
        snapshot = snapshot.copy()
        turn_id = str(uuid.uuid4())
        context = copy_context()
        reservation = self._dispatcher.reserve(snapshot.result.session_id)
        try:
            self._publish("review.queued", turn_id, snapshot, observe=False)
            submission = reservation.commit(lambda cancel: context.run(
                self._run, ReviewTask(turn_id, snapshot, cancel),
            ))
        except BaseException:
            reservation.cancel()
            raise
        # Jobs cancelled before dispatch still get a terminal trace event.
        submission.future.add_done_callback(
            lambda future: self._publish("review.cancelled", turn_id, snapshot, observe=False)
            if future.cancelled() else None
        )
        return submission

    def _run(self, task: ReviewTask) -> None:
        if task.cancel_event.is_set():
            self._publish("review.cancelled", task.turn_id, task.snapshot)
            return
        self._publish("review.started", task.turn_id, task.snapshot)
        try:
            if task.cancel_event.is_set():
                raise TurnCancelled("review was cancelled before execution")
            value = self._reviewer(task)
            if inspect.isawaitable(value):
                close = getattr(value, "close", None)
                if close is not None:
                    close()
                raise TypeError("reviewer returned an awaitable")
        except TurnCancelled:
            self._publish("review.cancelled", task.turn_id, task.snapshot)
            raise
        except BaseException as exc:
            self._publish("review.failed", task.turn_id, task.snapshot, error_type=type(exc).__name__)
            raise
        else:
            self._publish(
                "review.cancelled" if task.cancel_event.is_set() else "review.completed",
                task.turn_id, task.snapshot,
            )

    def _publish(
        self, name: str, turn_id: str, snapshot: TurnSnapshot, *,
        observe: bool = True, **payload,
    ) -> None:
        parent = snapshot.trace_id or snapshot.result.turn_id
        logger.info(
            "%s trace_id=%s parent_trace_id=%s session_id=%s error_type=%s",
            name, turn_id, parent, snapshot.result.session_id, payload.get("error_type"),
        )
        if observe and self._observability is not None:
            try:
                self._observability.publish(
                    name, trace_id=turn_id, session_id=snapshot.result.session_id,
                    turn_id=turn_id, payload={"parent_trace_id": parent, **payload},
                )
            except Exception:
                logger.exception("review observation failed")

    def close(self, *, timeout: float = 5) -> bool:
        return self._dispatcher.close(timeout=timeout)


__all__ = ["BackgroundReviewScheduler", "ReviewTask"]
