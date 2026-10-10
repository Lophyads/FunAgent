"""A single main worker; admission counts all accepted nonterminal tasks."""
from collections import deque
from dataclasses import replace
from threading import Condition, Thread
from uuid import uuid4
import time
import logging

from ..conf import ConfMgr
from ..contracts import (CancelResult, ControlError, DomainError, RecordScope,
                         RunContext, RunInput, RunResult)
from ..model import ModelRuntime, ModelStrategy, FallbackRule
from ..persistence import DbMgr
from ..session import SessionMgr
from ..interaction import InteractionMgr
from ..memory import MemoryMgr, MemoryPrepareRequest
from ..memory.retrieval import Evidence, SelectEvidence
from ..review import ReviewScheduler


class MainStrategy(ModelStrategy):
    @staticmethod
    def SelectModel():
        return ConfMgr.GetInstance().Serv().Options()["model_profile"]

    @staticmethod
    def MaxIterations():
        return ConfMgr.GetInstance().Serv().Options().get("max_iterations", 6)

    @staticmethod
    def ToolSet():
        return ConfMgr.GetInstance().Tool().Allowed("main")

    @staticmethod
    def FallbackPolicy():
        profile = ConfMgr.GetInstance().Model().Profile(MainStrategy.SelectModel())
        return FallbackRule(profile.fallback_profiles)


class TaskProcess:
    @staticmethod
    def Run(task_id, context):
        store = DbMgr.GetInstance()
        try:
            context.CheckCancelled()
            task = store.GetTask(task_id)
            session = SessionMgr.Load(task.request.session_id).session
            if session.prompt is None:
                prompt = ConfMgr.GetInstance().Serv().Options().get("system_prompt", "You are a helpful assistant.")
                SessionMgr.FreezePrompt(session.session_id, prompt)
            context.CheckCancelled()
            snapshot = store.BeginTurn(task_id)
            memory_conf = ConfMgr.GetInstance().Memory()
            policy = memory_conf.Policy("recall")
            access = {"workspace.read"}
            for name in policy.get("sources", ()):
                access.add(memory_conf.Sources()[name].get("read_access", f"memory.{name}.read"))
            context = replace(context, session_id=session.session_id, workspace=session.workspace,
                              access=frozenset(access), record_scope=RecordScope("main_history", task_id))
            memory = MemoryMgr.GetInstance()
            profile = memory.GetProfile(context)
            recalled = memory.Prepare(MemoryPrepareRequest(task.request.text), context)
            records, _ = SelectEvidence((*profile, *recalled.records), policy.get("max_records", 10), policy.get("max_chars", 16000))
            evidence = Evidence(records)
            # BeginTurn already contains this user; only this Run sees evidence.
            result = ModelRuntime.GetInstance().Run(RunInput(snapshot.messages, snapshot.session.prompt,
                                                          (evidence,) if evidence else ()), MainStrategy, context)
        except ControlError as error:
            result = RunResult("cancelled", reason=error.code)
        except DomainError as error:
            result = RunResult("failed", reason=error.code, error_code=error.code)
        try:
            committed = store.FinishTurn(task_id, result)
        except DomainError as error:
            actual = store.GetTask(task_id)
            if actual.status not in {"queued", "running"}:
                committed = actual
            else:
                # Preserve known effects when the final transaction fails.
                failure = replace(result, status="failed", final=None, reason=error.code, error_code=error.code)
                committed = store.FinishTurn(task_id, failure)
        policy = ConfMgr.GetInstance().Memory().Policy("review")
        if committed.status == "completed" and policy.get("enabled", False) and policy.get("sources"):
            try:
                ReviewScheduler.GetInstance().Schedule(committed.request.session_id, store.ReviewThrough(task_id))
            except DomainError as error:
                # The reply is already committed; background admission cannot
                # turn it into a failed task or rewrite its final assistant.
                logging.getLogger(__name__).warning("Review scheduling failed (%s)", error.code)
        return committed.result


class TaskMgr:
    _instance = None

    @classmethod
    def Initialize(cls, capacity):
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Tasks already initialized")
        if type(capacity) is not int or capacity <= 0:
            raise DomainError("INVALID_REQUEST", "Task capacity must be positive")
        DbMgr.GetInstance().RepairInterrupted()
        instance = cls()
        instance._capacity = capacity
        instance._condition = Condition()
        instance._queue = deque()
        instance._contexts = {}
        instance._running = None
        instance._accepting = True
        instance._worker = Thread(target=instance._work, name="funagent-main")
        instance._worker.start()
        cls._instance = instance

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            raise DomainError("NOT_READY", "Tasks not initialized")
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

    def _publish(self, task_id):
        self._queue.append(task_id)
        self._condition.notify_all()

    def Submit(self, request):
        with self._condition:
            if not self._accepting:
                raise DomainError("STOPPING", "Task admission stopped")
            if len(self._contexts) >= self._capacity:
                raise DomainError("CAPACITY", "Task capacity reached")
            task = DbMgr.GetInstance().RegisterTurn(request)
            try:
                self._contexts[task.task_id] = RunContext(run_id=uuid4().hex, task_id=task.task_id, session_id=request.session_id)
                self._publish(task.task_id)
            except Exception:
                if task.task_id in self._queue:
                    self._queue.remove(task.task_id)
                try:
                    DbMgr.GetInstance().FinishTurn(task.task_id, RunResult("failed", reason="SUBMIT_FAILED", error_code="SUBMIT_FAILED"))
                except Exception:
                    # Persistence unavailable: retain the slot and stop admission.
                    # The unpublished row is never run and startup will repair it.
                    self._accepting = False
                    self._condition.notify_all()
                else:
                    self._contexts.pop(task.task_id, None)
                raise DomainError("SUBMIT_FAILED", "Task could not be published") from None
            return task.task_id

    def Get(self, task_id):
        return DbMgr.GetInstance().GetTask(task_id)

    def Cancel(self, task_id):
        with self._condition:
            task = self.Get(task_id)
            context = self._contexts.get(task_id)
            if task.status not in {"queued", "running"} or context is None:
                return CancelResult(task_id, task.status, False)
            context.cancel.set()
            InteractionMgr.CancelTask(task_id)
            if self._running != task_id:
                task = DbMgr.GetInstance().FinishTurn(task_id, RunResult("cancelled", reason="CANCELLED"))
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
                timeout = ConfMgr.GetInstance().Serv().Options().get("task_timeout", 60)
                context = replace(context, deadline=time.monotonic() + timeout)
                self._running = task_id
            committed = False
            try:
                TaskProcess.Run(task_id, context)
                committed = True
            except Exception:
                try:
                    actual = self.Get(task_id)
                    if actual.status in {"queued", "running"}:
                        DbMgr.GetInstance().FinishTurn(task_id, RunResult("failed", reason="STORAGE", error_code="STORAGE"))
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
        self._worker.join()
        if self._contexts and error is None:
            error = DomainError("STORAGE", "Unresolved tasks require startup repair")
        if error is not None:
            raise error
