"""A bounded, independent queue; only pending jobs may coalesce."""
from collections import OrderedDict
from threading import Condition, Thread
from uuid import uuid4
import time

from .conf import ConfMgr
from .contracts import DomainError, ReviewJob, RunContext, RunResult, freeze
from .protocols import ReviewExecutor, ReviewQueue
from ..persistence import DbMgr
from ..persistence.protocols import ReviewStore


class ReviewScheduler:
    _instance = None

    @classmethod
    def Initialize(cls, capacity=16, *, executor: ReviewExecutor, start_worker=True):
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Review already initialized")
        if type(capacity) is not int or capacity <= 0:
            raise DomainError("INVALID_REQUEST", "Review capacity must be positive")
        instance = cls()
        instance._executor = executor
        instance._store: ReviewStore = DbMgr.GetInstance()
        instance._timeout = ConfMgr.GetInstance().Memory().Policy("review").get("timeout", 30)
        instance._condition = Condition()
        instance._capacity, instance._pending = capacity, OrderedDict()
        instance._status, instance._running = {}, None
        instance._accepting = True
        instance._worker = Thread(target=instance._work, name="funagent-review")
        cls._instance = instance
        if start_worker:
            try:
                instance.Start()
            except Exception:
                cls.Close()
                raise

    def Start(self):
        self._worker.start()

    @classmethod
    def GetInstance(cls) -> ReviewQueue:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Review not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        if cls._instance is not None:
            instance = cls._instance
            try:
                instance.Stop()
            finally:
                if not instance._worker.is_alive():
                    cls._instance = None

    def Schedule(self, session_id, through_message_id):
        if type(through_message_id) is not int or through_message_id <= 0:
            raise DomainError("INVALID_REQUEST", "Review requires a complete Turn")
        store = self._store
        # Validate before publication, never changing an executing job.
        store.ReviewRange(session_id, 0, through_message_id)
        with self._condition:
            if not self._accepting:
                raise DomainError("STOPPING", "Review admission stopped")
            if store.ReviewWatermark(session_id) >= through_message_id:
                return
            running = self._running[0] if self._running else None
            pending = self._pending.get(session_id)
            if ((running and running.session_id == session_id and running.through_message_id >= through_message_id) or
                    (pending and pending.through_message_id >= through_message_id)):
                return
            if pending is None and len(self._pending) >= self._capacity:
                if not running or running.session_id != session_id:
                    self._status[session_id] = {"state": "failed", "through_message_id": through_message_id, "error_code": "CAPACITY"}
                raise DomainError("CAPACITY", "Review queue capacity reached")
            self._pending[session_id] = ReviewJob(session_id, through_message_id)
            self._condition.notify_all()

    def GetStatus(self, session_id):
        trigger = self._store.ReviewTrigger(session_id)
        with self._condition:
            status = dict(self._status.get(session_id, {"state": "idle", "through_message_id": 0, "error_code": None, "cancelled_through": None}))
            status.update(trigger)
            running = self._running[0] if self._running else None
            pending = self._pending.get(session_id)
            status["running_through"] = running.through_message_id if running and running.session_id == session_id else None
            status["pending_through"] = pending.through_message_id if pending else None
            if status["running_through"] is not None:
                status["state"] = "running"
            elif pending is not None:
                status["state"] = "queued"
            return freeze(status)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or not self._accepting)
                if not self._pending:
                    return
                session_id, job = self._pending.popitem(last=False)
                context = RunContext(run_id=uuid4().hex, purpose="review", session_id=session_id,
                                     deadline=time.monotonic() + self._timeout)
                self._running = job, context
            try:
                result = self._executor.Run(job, context)
            except Exception:
                result = RunResult("failed", reason="REVIEW_FAILED", error_code="REVIEW_FAILED")
            with self._condition:
                self._status[session_id] = {"state": result.status, "through_message_id": job.through_message_id,
                                            "error_code": None if result.status == "completed" else result.error_code or result.reason,
                                            "cancelled_through": self._status.get(session_id, {}).get("cancelled_through")}
                self._running = None
                self._condition.notify_all()

    def RequestStop(self):
        with self._condition:
            self._accepting = False
            for job in self._pending.values():
                self._status[job.session_id] = {"state": "cancelled", "through_message_id": job.through_message_id, "error_code": "CANCELLED",
                                                 "cancelled_through": job.through_message_id}
            self._pending.clear()
            if self._running:
                self._running[1].cancel.set()
            self._condition.notify_all()

    def Stop(self):
        self.RequestStop()
        if self._worker.ident is not None:
            self._worker.join()

    def IsStopped(self):
        return not self._worker.is_alive()
