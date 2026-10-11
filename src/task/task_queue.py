"""A single main worker; admission counts all accepted nonterminal tasks."""
from collections import deque
from dataclasses import replace
from threading import Condition, Thread
from uuid import uuid4
import time
from ..frame.contracts import CancelResult, DomainError, RunContext, RunResult, PageQuery
from ..persistence.db_mgr import DbMgr
from ..persistence.protocols import TaskStore
from ..frame.interaction_mgr import InteractionMgr
from .protocols import TaskExecutor
from ..frame.event_mgr import Emit


class TaskQueue:
    def __init__(self, capacity, *, executor: TaskExecutor, timeout=60, start_worker=True):
        if type(capacity) is not int or capacity <= 0:
            raise DomainError("INVALID_REQUEST", "Task capacity must be positive")
        self._store: TaskStore = DbMgr.GetInstance()
        self._store.RepairInterrupted()
        self._executor, self._timeout = executor, timeout
        self._capacity = capacity
        self._condition = Condition()
        self._queue = deque()
        self._contexts = {}
        self._running = None
        self._accepting = True
        self._worker = Thread(target=self._work, name="funagent-main")
        if start_worker:
            try:
                self.Start()
            except Exception:
                self.RequestStop()
                if self._worker.ident is not None:
                    self._worker.join()
                raise

    def Start(self):
        self._worker.start()

    def _publish(self, task_id):
        self._queue.append(task_id)
        self._condition.notify_all()

    def Submit(self, request):
        with self._condition:
            if request.request_key is not None:
                existing = self._store.FindTurn(request)
                if existing is not None:
                    return existing.task_id
            if not self._accepting:
                raise DomainError("STOPPING", "Task admission stopped")
            if len(self._contexts) >= self._capacity:
                raise DomainError("CAPACITY", "Task capacity reached")
            task = self._store.RegisterTurn(request)
            try:
                self._contexts[task.task_id] = RunContext(run_id=uuid4().hex, task_id=task.task_id, session_id=request.session_id)
                Emit(self._contexts[task.task_id], 'task_queued', {'text': request.text, 'request_key': request.request_key})
                self._publish(task.task_id)
            except Exception:
                if task.task_id in self._queue:
                    self._queue.remove(task.task_id)
                try:
                    self._store.FinishTurn(task.task_id, RunResult("failed", reason="SUBMIT_FAILED", error_code="SUBMIT_FAILED"))
                except Exception:
                    # Persistence unavailable: retain the slot and stop admission.
                    # The unpublished row is never run and startup will repair it.
                    self._accepting = False
                    self._condition.notify_all()
                else:
                    self._contexts.pop(task.task_id, None)
                raise DomainError("SUBMIT_FAILED", "Task could not be published") from None
            return task.task_id

    def Status(self):
        with self._condition:
            return {"accepting": self._accepting, "capacity": self._capacity,
                    "accepted": len(self._contexts), "queued": len(self._queue),
                    "running": self._running is not None}

    def Get(self, task_id):
        return self._store.GetTask(task_id)

    def List(self, session_id, query=PageQuery()):
        return self._store.ListTasks(session_id, query)

    def Cancel(self, task_id):
        with self._condition:
            task = self.Get(task_id)
            context = self._contexts.get(task_id)
            if task.status not in {"queued", "running"} or context is None:
                return CancelResult(task_id, task.status, False)
            context.cancel.set()
            Emit(context, 'cancel_requested')
            InteractionMgr.CancelTask(task_id)
            if self._running != task_id:
                task = self._store.FinishTurn(task_id, RunResult("cancelled", reason="CANCELLED"))
                Emit(context, 'task_committed', {'status': task.status})
                if task_id in self._queue:
                    self._queue.remove(task_id)
                del self._contexts[task_id]
            return CancelResult(task_id, task.status, True)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or not self._accepting)
                if not self._queue:
                    return
                task_id = self._queue.popleft()
                context = self._contexts[task_id]
                timeout = self._timeout
                context = replace(context, deadline=time.monotonic() + timeout)
                self._running = task_id
            committed = False
            try:
                self._executor.Run(task_id, context)
                committed = True
            except Exception:
                try:
                    actual = self.Get(task_id)
                    if actual.status in {"queued", "running"}:
                        self._store.FinishTurn(task_id, RunResult("failed", reason="STORAGE", error_code="STORAGE"))
                    committed = True
                except Exception:
                    # Do not invent a terminal state or free unresolved capacity.
                    with self._condition:
                        self._accepting = False
                        for pending in self._contexts.values():
                            pending.cancel.set()
            finally:
                InteractionMgr.CancelTask(task_id)
                with self._condition:
                    if committed:
                        self._contexts.pop(task_id, None)
                    self._running = None
                    self._condition.notify_all()

    def RequestStop(self):
        """Stop admission and wake cancellation without waiting on borrowed IO."""
        with self._condition:
            self._accepting = False
            for task_id, context in self._contexts.items():
                context.cancel.set()
                InteractionMgr.CancelTask(task_id)
            self._condition.notify_all()

    def Stop(self):
        self.RequestStop()
        error = None
        with self._condition:
            for task_id in tuple(self._contexts):
                try:
                    self.Cancel(task_id)
                except Exception as failure:
                    error = failure if isinstance(failure, DomainError) else DomainError("STORAGE", "Task shutdown persistence failed")
        if self._worker.ident is not None:
            self._worker.join()
        if self._contexts and error is None:
            error = DomainError("STORAGE", "Unresolved tasks require startup repair")
        if error is not None:
            raise error

    def IsStopped(self) -> bool:
        return not self._worker.is_alive()
