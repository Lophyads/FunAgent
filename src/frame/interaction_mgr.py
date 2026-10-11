"""Task-bound human requests; answers bypass the occupied main queue."""
from dataclasses import dataclass, field
from math import isfinite
from threading import Event, Lock
from uuid import uuid4
import time

from .contracts import ApprovalBinding, ControlError, DomainError, Interaction, RunContext
from .protocols import InteractionAccess
from .clarification import ValidateQuestions, ValidateAnswers
from .event_mgr import Emit


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
        with self._lock:
            waiter = self._pending.get(interaction_id)
            if waiter is not None:
                self._expire(interaction_id, waiter)
            if waiter is None or waiter.error is not None or (waiter.request.task_id, waiter.request.run_id) != (task_id, run_id):
                raise DomainError("INTERACTION_STALE", "Human request is no longer pending for this run")
            if waiter.request.kind == "approval":
                valid = type(answer) is bool
            elif waiter.request.kind == "clarify":
                answer = ValidateAnswers(waiter.request.questions, answer)
                valid = True
            if not valid:
                raise DomainError("INVALID_REQUEST", "Invalid human answer")
            waiter.answer = answer
            self._pending.pop(interaction_id)
            waiter.event.set()
            Emit(waiter.context, 'interaction_resolved', {'interaction_id': interaction_id})
