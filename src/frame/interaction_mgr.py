"""Task-bound human requests; answers bypass the occupied main queue."""
from dataclasses import dataclass, field
from contextlib import ExitStack
from math import isfinite
from threading import Event, Lock
from uuid import uuid4
import time

from .contracts import ApprovalBinding, ControlError, DomainError, Interaction, RunContext
from .protocols import InteractionAccess
from .clarification import ValidateQuestions, ValidateAnswers
from .event_mgr import Emit
from ..observability.observation_mgr import ObservationMgr
from .clarification import QuestionData


@dataclass
class _Waiter:
    request: Interaction
    context: RunContext
    event: Event = field(default_factory=Event)
    answer: object = None
    error: DomainError | None = None


class InteractionMgr:
    _instance = None

    @classmethod
    def Initialize(cls, timeout=120):
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Interactions already initialized")
        if type(timeout) not in {int, float} or not isfinite(timeout) or timeout <= 0:
            raise DomainError("INVALID_REQUEST", "Interaction timeout must be positive")
        instance = cls()
        instance._timeout = timeout
        instance._lock = Lock()
        instance._pending = {}
        instance._closed = False
        cls._instance = instance

    @classmethod
    def GetInstance(cls) -> InteractionAccess:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Interactions not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        instance = cls._instance
        if instance is not None:
            with instance._lock:
                instance._closed = True
                for identifier, waiter in tuple(instance._pending.items()):
                    instance._end(identifier, waiter, ControlError("CANCELLED"))
            cls._instance = None

    @classmethod
    def CancelTask(cls, task_id):
        instance = cls._instance
        if instance is not None:
            with instance._lock:
                for identifier, waiter in tuple(instance._pending.items()):
                    if waiter.request.task_id == task_id:
                        instance._end(identifier, waiter, ControlError("CANCELLED"))

    def _end(self, identifier, waiter, error):
        mgr = ObservationMgr.GetInstance()
        with mgr.UseBinding(('interaction', identifier)):
            mgr.Event('interaction.ended', interaction_id=identifier, error_code=error.code, occurred_at=time.time())
        self._pending.pop(identifier, None)
        waiter.error = error
        waiter.event.set()
        Emit(waiter.context, 'interaction_cleared', {'interaction_id': identifier})

    def _expire(self, identifier, waiter):
        if self._pending.get(identifier) is not waiter:
            return
        try:
            waiter.context.CheckCancelled()
        except ControlError as error:
            self._end(identifier, waiter, error)
        else:
            if time.monotonic() >= waiter.request.expires_at:
                self._end(identifier, waiter, DomainError("INTERACTION_EXPIRED", "Human request expired"))

    def Wait(self, kind, prompt, context, *, binding=None, questions=()):
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('interaction.wait', input=lambda: dict(kind=kind, prompt=prompt,
                       questions=[QuestionData(q) for q in questions],
                       approval=dict(call_id=binding.call_id, tool_name=binding.tool_name,
                                     arguments_digest=binding.arguments_digest) if binding else None)):
            try:
                answer = self._wait(kind, prompt, context, binding=binding, questions=questions)
            except DomainError as error:
                status = ('expired' if error.code == 'INTERACTION_EXPIRED' else 'cancelled' if error.code == 'CANCELLED'
                          else 'timeout' if error.code == 'DEADLINE' else 'failed')
                mgr.Complete(status=status, error_code=error.code)
                raise
            mgr.Complete(status='declined' if kind == 'approval' and answer is False else 'answered',
                         output=lambda: {'answer': answer})
            return answer

    def _wait(self, kind, prompt, context, *, binding=None, questions=()):
        mgr = ObservationMgr.GetInstance()
        if context.purpose != "main" or not context.task_id or not context.run_id:
            raise DomainError("INTERACTION_DENIED", "This run cannot request human input")
        if kind not in {"clarify", "approval"} or not isinstance(prompt, str) or not prompt.strip():
            raise DomainError("INVALID_REQUEST", "Invalid human request")
        if kind == "clarify":
            questions = ValidateQuestions(questions)
        elif questions:
            raise DomainError("INVALID_REQUEST", "Questions require a clarification request")
        if binding is not None and (kind != "approval" or not isinstance(binding, ApprovalBinding)
                or (binding.task_id, binding.run_id) != (context.task_id, context.run_id)
                or not binding.call_id or not binding.tool_name or not binding.arguments_digest):
            raise DomainError("INVALID_REQUEST", "Invalid approval binding")
        context.CheckCancelled()
        deadline = time.monotonic() + self._timeout
        if context.deadline is not None:
            deadline = min(deadline, context.deadline)
        request = Interaction(uuid4().hex, context.task_id, context.run_id, kind, prompt, deadline, binding, questions)
        waiter = _Waiter(request, context)
        with self._lock:
            if self._closed:
                raise DomainError("NOT_READY", "Interactions closed")
            context.CheckCancelled()
            if any(w.request.task_id == context.task_id for w in self._pending.values()):
                raise DomainError("INTERACTION_PENDING", "Task already has a human request")
            mgr.Update(interaction_id=request.interaction_id, expires_at=deadline,
                       **({'call_id': binding.call_id} if binding else {}))
            mgr.Bind(('interaction', request.interaction_id))
            self._pending[request.interaction_id] = waiter
        Emit(context, 'interaction_pending', {'interaction_id': request.interaction_id, 'kind': kind})
        try:
            while not waiter.event.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                with self._lock:
                    self._expire(request.interaction_id, waiter)
            if waiter.error is not None:
                raise waiter.error
            context.CheckCancelled()
            return waiter.answer
        finally:
            with self._lock:
                self._pending.pop(request.interaction_id, None)

    def Pending(self, task_id):
        with self._lock:
            for identifier, waiter in tuple(self._pending.items()):
                self._expire(identifier, waiter)
            return tuple(w.request for w in self._pending.values() if w.request.task_id == task_id)

    def Resolve(self, interaction_id, answer, *, task_id, run_id):
        mgr = ObservationMgr.GetInstance()
        # Acquire the server-owned target while it is still pending, and keep
        # the lease through publication even if Wait has already returned.
        with ExitStack() as leases:
            with self._lock:
                waiter = self._pending.get(interaction_id)
                if waiter is not None:
                    self._expire(interaction_id, waiter)
                if waiter is None or waiter.error is not None or (waiter.request.task_id, waiter.request.run_id) != (task_id, run_id):
                    raise DomainError("INTERACTION_STALE", "Human request is no longer pending for this run")
                leases.enter_context(mgr.UseBinding(('interaction', interaction_id)))
                occurred_at = time.time()
                try:
                    if waiter.request.kind == 'approval':
                        if type(answer) is not bool:
                            raise DomainError('INVALID_REQUEST', 'Invalid human answer')
                    else:
                        answer = ValidateAnswers(waiter.request.questions, answer)
                except DomainError as error:
                    mgr.Event('interaction.answer_invalid', interaction_id=interaction_id,
                              error_code=error.code, occurred_at=occurred_at)
                    raise
                waiter.answer = answer
                self._pending.pop(interaction_id)
                waiter.event.set()
            mgr.Event('interaction.resolved', interaction_id=interaction_id, answer=lambda: answer,
                      occurred_at=occurred_at)
            Emit(waiter.context, 'interaction_resolved', {'interaction_id': interaction_id})
