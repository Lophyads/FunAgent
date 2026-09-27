"""Scoped, bounded human interaction; replies never enter the turn queue."""

from __future__ import annotations

import inspect
import secrets
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tools.lifecycle import SessionResourceKey
from .interfaces import TurnCancelled
from .workers import DaemonWorkerPool, WorkerQueueFullError


class InteractionUnavailable(RuntimeError):
    pass


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    qid: str = Field(min_length=1)
    id: str | None = None
    question: str = Field(min_length=1, max_length=2000)
    choices: tuple[str, ...] = Field(default=(), max_length=4)
    multi_select: bool = False


@dataclass(frozen=True, slots=True)
class ClarifyPayload:
    questions: tuple[Question, ...]

    def __post_init__(self):
        questions = tuple(self.questions)
        if not 1 <= len(questions) <= 5 or len({q.qid for q in questions}) != len(questions):
            raise ValueError("clarification requires 1-5 questions with unique IDs")
        object.__setattr__(self, "questions", questions)


@dataclass(frozen=True, slots=True)
class ClarifyResponse:
    answers: Mapping[str, str | tuple[str, ...]]

    def __post_init__(self):
        object.__setattr__(self, "answers", immutable(self.answers))


@dataclass(frozen=True, slots=True)
class ApprovalPayload:
    tool_name: str
    scope: str
    arguments: Mapping[str, Any]
    choices: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "arguments", immutable(self.arguments))
        object.__setattr__(self, "choices", tuple(self.choices))


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["once", "session", "always", "deny"]


def immutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: immutable(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(immutable(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    session_id: str
    turn_id: str
    tool_call_id: str
    request_id: str
    kind: Literal["clarify", "approval"]
    payload: ClarifyPayload | ApprovalPayload
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        def plain(value):
            if isinstance(value, BaseModel):
                return value.model_dump(mode="json")
            if isinstance(value, Mapping):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(item) for item in value]
            return value

        if isinstance(self.payload, ClarifyPayload):
            payload = {"questions": plain(self.payload.questions)}
        else:
            payload = {
                "tool_name": self.payload.tool_name, "scope": self.payload.scope,
                "arguments": plain(self.payload.arguments), "choices": plain(self.payload.choices),
            }
        return {
            "session_id": self.session_id, "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id, "request_id": self.request_id,
            "kind": self.kind, "payload": payload,
            "created_at": self.created_at, "expires_at": self.expires_at,
        }


class InteractionPort(Protocol):
    """同步且快速返回的通知接口，不是 TCP 端口；present 只通知，答案由宿主另行 resolve。

    可以把请求交给 UI 事件队列或平台发送队列，但不能在通知 worker 中等待用户输入。
    """
    def present(self, request: InteractionRequest) -> None: ...


@dataclass(slots=True)
class _Pending:
    request: InteractionRequest
    key: SessionResourceKey
    owner_id: str
    port: InteractionPort
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    state: str = "pending"
    response: ClarifyResponse | ApprovalResponse | None = None


class InteractionBroker:
    def __init__(self, capacity: int = 128):
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self._capacity = capacity
        # 把宿主展示/转发与 Turn 的等待分开，并限制通知并发和队列，避免无限创建线程。
        self._notifications = DaemonWorkerPool(2, capacity, name="tool-notification")

    def pending(self, *, port: InteractionPort | None = None,
                owner_id: str | None = None) -> tuple[InteractionRequest, ...]:
        with self._lock:
            return tuple(
                p.request for p in self._pending.values()
                if p.state == "pending" and time.monotonic() < p.deadline
                and (port is None or p.port is port)
                and (owner_id is None or p.owner_id == owner_id)
            )

    def resolve(self, session_id: str, request_id: str, response: Any, *,
                port: InteractionPort) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if (pending is None or pending.port is not port or self._closed
                    or pending.request.session_id != session_id
                    or pending.state != "pending"):
                return False
            if time.monotonic() >= pending.deadline:
                self._finish(pending, "timed_out")
                return False
            try:
                answer = self._validate_response(pending.request, response)
            except (ValueError, TypeError, ValidationError):
                return False
            # 答案属于原请求，设置事件唤醒原 Turn；不能为人工答复再创建一个排队中的 Turn。
            pending.response = answer
            self._finish(pending, "resolved")
            return True

    @staticmethod
    def _validate_response(request: InteractionRequest, response: Any):
        payload = request.payload
        if isinstance(response, ClarifyResponse):
            response = {"answers": response.answers}
        if isinstance(payload, ApprovalPayload):
            answer = ApprovalResponse.model_validate(response)
            if answer.decision not in payload.choices:
                raise ValueError("approval choice is unavailable")
            return answer
        if not isinstance(response, Mapping) or set(response) != {"answers"}:
            raise ValueError("clarification requires answers")
        answers = response["answers"]
        if not isinstance(answers, Mapping) or set(answers) != {q.qid for q in payload.questions}:
            raise ValueError("answer IDs must match the questions")
        validated = {}
        for question in payload.questions:
            value = answers[question.qid]
            if question.multi_select:
                if not isinstance(value, (list, tuple)) or len(value) > 4:
                    raise ValueError("multi-select answer must be a bounded list")
                if any(not isinstance(v, str) or len(v) > 1000 for v in value):
                    raise ValueError("invalid multi-select answer")
                validated[question.qid] = tuple(value)
            else:
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError("answer must be bounded text")
                validated[question.qid] = value
        return ClarifyResponse(immutable(validated))

    @staticmethod
    def _finish(pending: _Pending, state: str) -> None:
        if pending.state == "pending":
            pending.state = state
            pending.event.set()

    def cancel(self, *, owner_id: str | None = None, turn_id: str | None = None,
               port: InteractionPort | None = None, key: SessionResourceKey | None = None):
        with self._lock:
            for p in self._pending.values():
                if (owner_id is None or p.owner_id == owner_id) and (
                    turn_id is None or p.request.turn_id == turn_id
                ) and (port is None or p.port is port) and (key is None or p.key == key):
                    self._finish(p, "cancelled")

    def request(self, kind, payload, *, key: SessionResourceKey, owner_id: str,
                turn_id: str, tool_call_id: str, port: InteractionPort | None,
                timeout: float, check_active: Callable[[], None]):
        if timeout <= 0:
            raise ValueError("interaction timeout must be positive")
        expected = {"clarify": ClarifyPayload, "approval": ApprovalPayload}.get(kind)
        if expected is None or not isinstance(payload, expected):
            raise TypeError("interaction kind and payload do not match")
        if port is None:
            raise InteractionUnavailable("no interaction capability for this turn")
        check_active()
        now = time.time()
        request = InteractionRequest(
            key.session_id, turn_id, tool_call_id, secrets.token_urlsafe(24),
            kind, payload, now, now + timeout,
        )
        pending = _Pending(request, key, owner_id, port, time.monotonic() + timeout)
        with self._lock:
            if self._closed or len(self._pending) >= self._capacity:
                raise InteractionUnavailable("interaction broker is unavailable")
            self._pending[request.request_id] = pending
        try:
            try:
                # 后台通知调用 port.present；其返回值不是用户答案，不结束这个 pending 请求。
                self._notifications.submit(lambda: self._present(pending))
            except (RuntimeError, WorkerQueueFullError):
                with self._lock:
                    self._finish(pending, "unavailable")
            while True:
                check_active()
                with self._lock:
                    if time.monotonic() >= pending.deadline:
                        self._finish(pending, "timed_out")
                    status = pending.state
                    if status == "resolved":
                        return pending.response
                    if status == "timed_out":
                        return None
                    if status == "cancelled":
                        raise TurnCancelled("interaction was cancelled")
                    if status == "unavailable":
                        raise InteractionUnavailable("notification endpoint is unavailable")
                # 等待发生在发起请求的执行线程，不占用通知 worker；定时醒来检查取消/lease。
                pending.event.wait(0.05)
        finally:
            with self._lock:
                self._pending.pop(request.request_id, None)

    def _present(self, pending: _Pending):
        with self._lock:
            if pending.state != "pending":
                return
        try:
            result = pending.port.present(pending.request)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if close is not None:
                    close()
                raise TypeError("InteractionPort.present must be synchronous")
        except Exception:
            with self._lock:
                self._finish(pending, "unavailable")

    def close(self, timeout: float = 0) -> bool:
        with self._lock:
            self._closed = True
            self.cancel()
        return self._notifications.close(cancel_pending=True, timeout=timeout)
