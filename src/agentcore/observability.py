from __future__ import annotations

import hashlib
import inspect
import secrets
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, TypeAlias

from .workers import DaemonWorkerPool, WorkerQueueFullError, submit_daemon_call


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    name: str
    occurred_at: float
    trace_id: str
    session_id: str | None
    turn_id: str | None
    phase: str | None
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    name: str
    occurred_at: float
    trace_id: str
    session_id: str | None
    turn_id: str | None
    phase: str | None
    payload: Mapping[str, JsonValue]


class LifecycleObserver(Protocol):
    def observe(self, event: AgentEvent) -> None: ...


class EventSink(Protocol):
    def emit(self, event: EventEnvelope) -> None: ...

    def close(self) -> None: ...


class NullEventSink:
    def emit(self, event: EventEnvelope) -> None:
        return None

    def close(self) -> None:
        return None


def _is_async(callback: Any) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    )


class ObservabilityHub:
    """Failure-isolated, centrally redacted observer and event-sink fanout."""

    _SENSITIVE_KEYS = {
        "api_key", "authorization", "credential", "credential_ref",
        "prompt", "system_prompt", "user_input", "message", "arguments",
        "result", "content", "tool_result", "input", "output",
        "text", "body", "headers", "user_message",
    }

    def __init__(
        self, *, observers: tuple[LifecycleObserver, ...] = (),
        sinks: tuple[EventSink, ...] = (), capacity: int = 256,
        observer_timeout: float = 2.0,
    ) -> None:
        for index, observer in enumerate(observers):
            callback = getattr(observer, "observe", None)
            if callback is None or _is_async(callback):
                raise TypeError(f"observer {index}.observe must be synchronous")
        for index, sink in enumerate(sinks):
            emit = getattr(sink, "emit", None)
            close = getattr(sink, "close", None)
            if emit is None or close is None or _is_async(emit) or _is_async(close):
                raise TypeError(f"event sink {index} must be synchronous")
        self._observers = observers
        self._sinks = sinks
        self._timeout = observer_timeout
        self._salt = secrets.token_bytes(32)
        self._observer_pool = DaemonWorkerPool(
            max(1, len(observers)), capacity, name="agentcore-observer"
        )
        self._sink_pool = DaemonWorkerPool(1, capacity, name="agentcore-event-sink")
        self._closed = threading.Event()
        self._dropped = 0
        self._drop_lock = threading.Lock()

    @property
    def dropped_events(self) -> int:
        with self._drop_lock:
            return self._dropped

    def publish(
        self, name: str, *, trace_id: str,
        session_id: str | None = None, turn_id: str | None = None,
        phase: str | None = None, payload: Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        if self._closed.is_set():
            return ("observability hub is closed",)
        occurred_at = time.time()
        safe_payload = self._freeze(self._sanitize(payload or {}))
        safe_session = self._digest(session_id) if session_id else None
        safe_turn = self._digest(turn_id) if turn_id else None
        event = AgentEvent(
            name=name, occurred_at=occurred_at, trace_id=trace_id,
            session_id=safe_session, turn_id=safe_turn, phase=phase,
            payload=safe_payload,
        )
        warnings: list[str] = []
        for index, observer in enumerate(self._observers):
            try:
                future = self._observer_pool.submit(
                    lambda observer=observer: observer.observe(event)
                )
                future.result(timeout=self._timeout)
            except FutureTimeoutError:
                warnings.append(f"observer {index} timed out")
            except BaseException as exc:
                warnings.append(
                    f"observer {index} failed: {type(exc).__name__}: {exc}"
                )
        envelope = EventEnvelope(
            event_id=str(uuid.uuid4()), name=name, occurred_at=occurred_at,
            trace_id=trace_id, session_id=safe_session, turn_id=safe_turn,
            phase=phase, payload=safe_payload,
        )
        try:
            self._sink_pool.submit(lambda: self._publish_sinks(envelope))
        except WorkerQueueFullError:
            with self._drop_lock:
                self._dropped += 1
            warnings.append("event sink queue saturated; event dropped")
        return tuple(warnings)

    def close(self, timeout: float) -> tuple[str, ...]:
        if self._closed.is_set():
            return ()
        self._closed.set()
        warnings: list[str] = []
        if not self._observer_pool.close(cancel_pending=True, timeout=timeout):
            warnings.append("observer pool did not stop before timeout")
        if not self._sink_pool.close(cancel_pending=False, timeout=timeout):
            warnings.append("event sink queue did not drain before timeout")
        for index, sink in enumerate(self._sinks):
            try:
                future = submit_daemon_call(
                    sink.close, name=f"agentcore-sink-close-{index}"
                )
                future.result(timeout=max(0.0, timeout))
            except FutureTimeoutError:
                warnings.append(f"event sink {index}.close timed out")
            except BaseException as exc:
                warnings.append(
                    f"event sink {index}.close failed: {type(exc).__name__}: {exc}"
                )
        return tuple(warnings)

    def _publish_sinks(self, event: EventEnvelope) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except BaseException:
                pass

    @staticmethod
    def _freeze(value: Any) -> Any:
        if isinstance(value, dict):
            return MappingProxyType({
                key: ObservabilityHub._freeze(item)
                for key, item in value.items()
            })
        if isinstance(value, list):
            return tuple(ObservabilityHub._freeze(item) for item in value)
        return value

    def _digest(self, value: str) -> str:
        return hashlib.sha256(self._salt + value.encode("utf-8")).hexdigest()[:24]

    def _sanitize(self, value: Mapping[str, Any]) -> dict[str, JsonValue]:
        safe: dict[str, JsonValue] = {}
        for key, item in list(value.items())[:64]:
            normalized = key.lower()
            if normalized in self._SENSITIVE_KEYS or any(
                marker in normalized
                for marker in ("secret", "token", "password", "api_key")
            ):
                safe[key] = "[redacted]"
            else:
                safe[key] = self._json_value(item)
        return safe

    def _json_value(self, value: Any) -> JsonValue:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:512]
        if isinstance(value, Mapping):
            return self._sanitize({
                str(key): item for key, item in list(value.items())[:64]
            })
        if isinstance(value, (list, tuple)):
            return [self._json_value(item) for item in value[:64]]
        return type(value).__name__


__all__ = [
    "AgentEvent",
    "EventEnvelope",
    "EventSink",
    "JsonValue",
    "LifecycleObserver",
    "NullEventSink",
    "ObservabilityHub",
]
