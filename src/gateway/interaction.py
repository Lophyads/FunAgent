"""Gateway-local notification routes; request state belongs to the shared Broker."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Literal

from agentcore.interfaces import HostCapabilities, TurnHostContext
from agentcore.interaction import (
    InteractionBroker, InteractionPort, InteractionRequest, InteractionUnavailable,
)
from tools.lifecycle import remaining


InteractionMode = Literal["none", "poll", "push"]


class _TurnPort:
    """A revocable notification address with no Agent or Gateway reference."""

    __slots__ = ("_notify", "_cancel_event")

    def __init__(self, notify: Callable[[_TurnPort, InteractionRequest], None]):
        self._notify = notify
        self._cancel_event: threading.Event | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def present(self, request: InteractionRequest) -> None:
        return self._notify(self, request)


class _InteractionRoutes:
    """Own only routing and the bounded poll queue, never interaction outcomes."""

    def __init__(
        self, broker: InteractionBroker, *, mode: InteractionMode,
        endpoint: InteractionPort | None, capacity: int,
    ):
        self._broker = broker
        self._mode = mode
        self._endpoint = endpoint
        self._capacity = capacity
        self._condition = threading.Condition(threading.RLock())
        self._ports: dict[_TurnPort, str] = {}
        self._notifications: OrderedDict[str, InteractionRequest] = OrderedDict()
        self._closed = False

    def bind(self, session_id: str) -> tuple[TurnHostContext, _TurnPort | None]:
        with self._condition:
            if self._closed:
                raise InteractionUnavailable("gateway interaction routes are closed")
            if self._mode == "none":
                return TurnHostContext(), None
            port = _TurnPort(self._present)
            self._ports[port] = session_id
            return TurnHostContext(HostCapabilities(clarify=True, approval=True), port), port

    def watch_cancellation(self, port: _TurnPort | None, event: threading.Event) -> None:
        if port is not None:
            with self._condition:
                port._cancel_event = event

    def release(self, port: _TurnPort | None) -> None:
        if port is None:
            return
        with self._condition:
            self._ports.pop(port, None)
            self._broker.cancel(port=port)
            self._prune()
            self._condition.notify_all()

    def detach(self, session_id: str) -> None:
        with self._condition:
            for port, sid in tuple(self._ports.items()):
                if sid == session_id:
                    self.release(port)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for port in tuple(self._ports):
                self.release(port)
            self._notifications.clear()
            self._condition.notify_all()

    def _present(self, port: _TurnPort, request: InteractionRequest) -> None:
        with self._condition:
            if self._closed or port.cancelled or self._ports.get(port) != request.session_id:
                raise InteractionUnavailable("turn notification port is closed")
            if request not in self._broker.pending(port=port):
                return
            if self._mode == "poll":
                self._prune()
                if len(self._notifications) >= self._capacity:
                    raise InteractionUnavailable("gateway notification queue is full")
                self._notifications[request.request_id] = request
                self._condition.notify_all()
                return
        # Broker's bounded notification workers call user code outside routing locks.
        assert self._endpoint is not None
        return self._endpoint.present(request)

    def pending(self, session_id: str | None = None) -> tuple[InteractionRequest, ...]:
        with self._condition:
            return tuple(
                request for port, sid in self._ports.items()
                if not port.cancelled and (session_id is None or sid == session_id)
                for request in self._broker.pending(port=port)
            )

    def _prune(self) -> None:
        active = {request.request_id for request in self.pending()}
        for request_id in tuple(self._notifications):
            if request_id not in active:
                self._notifications.pop(request_id, None)

    def next(
        self, *, session_id: str | None = None, timeout: float | None = None,
    ) -> InteractionRequest | None:
        if self._mode != "poll":
            raise RuntimeError("next_interaction requires poll mode")
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        with self._condition:
            while not self._closed:
                self._prune()
                for request_id, request in self._notifications.items():
                    if session_id is None or request.session_id == session_id:
                        self._notifications.pop(request_id)
                        return request
                if deadline is not None and remaining(deadline) == 0:
                    return None
                self._condition.wait(None if deadline is None else remaining(deadline))
        return None

    def resolve(self, session_id: str, request_id: str, response: Any) -> bool:
        with self._condition:
            for port, sid in self._ports.items():
                if sid == session_id and not port.cancelled and self._broker.resolve(
                    session_id, request_id, response, port=port,
                ):
                    self._notifications.pop(request_id, None)
                    self._condition.notify_all()
                    return True
        return False
