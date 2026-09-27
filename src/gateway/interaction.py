"""当前 Gateway 的通知路由，不重复管理请求的等待、答案或超时。

通知链路：Broker -> 本轮 _TurnPort -> _InteractionRoutes -> poll 队列或 push 宿主。
答复链路：宿主 -> Gateway.resolve_interaction -> Broker 校验并唤醒原等待者。
"""

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
    """每个 Turn 独立的通知句柄；通过回调路由，不是网络端口，也不拥有宿主资源。"""

    __slots__ = ("_notify", "_cancel_event")

    def __init__(self, notify: Callable[[_TurnPort, InteractionRequest], None]):
        # bind() 传入当前路由器的 _present；发送时携带自身身份，供路由器检查是否已撤销。
        self._notify = notify
        self._cancel_event: threading.Event | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def present(self, request: InteractionRequest) -> None:
        return self._notify(self, request)


class _InteractionRoutes:
    """只管理通知去向和有界队列；请求是否有效、是否已回答，始终由共享 Broker 裁决。"""

    def __init__(
        self, broker: InteractionBroker, *, mode: InteractionMode,
        endpoint: InteractionPort | None, capacity: int,
    ):
        self._broker = broker
        self._mode = mode
        # push 的宿主总出口，只负责展示/转交通知；poll 使用本对象的队列，不需要 endpoint。
        self._endpoint = endpoint
        self._capacity = capacity
        self._condition = threading.Condition(threading.RLock())
        # 端口对象身份 -> Session：同 Session 的多轮可以有不同端口，不能共用一个可变出口。
        self._ports: dict[_TurnPort, str] = {}
        # 这里只存待展示通知；被取走不代表已回答，Broker 仍保留 pending 请求等待答复。
        self._notifications: OrderedDict[str, InteractionRequest] = OrderedDict()
        self._closed = False

    def bind(self, session_id: str) -> tuple[TurnHostContext, _TurnPort | None]:
        """每次提交绑定一个可撤销端口，经 TurnHostContext 交给 Agent，不暴露整个 Gateway。"""
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
            # 先撤销路由，再取消原请求；迟到通知和答复不能重新激活已经结束的 Turn。
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
        # Broker 的通知 worker 调用这里，不是 CLI 主线程；不能在这里直接阻塞读取终端。
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
                # poll 也会主动唤醒收信者，只是把展示和 input() 留给 CLI 主线程。
                self._condition.notify_all()
                return
        # push 回调在锁外执行且应尽快返回；答案必须稍后经 resolve 提交，不能从 present 返回。
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
                # 无通知时休眠，新通知/撤销/关闭会唤醒；超时让 CLI 能再次检查 Turn 是否完成。
                self._condition.wait(None if deadline is None else remaining(deadline))
        return None

    def resolve(self, session_id: str, request_id: str, response: Any) -> bool:
        # request_id 不是权限凭据：答复还必须匹配本 Gateway 的有效端口与 Session。
        # 平台用户身份认证属于宿主职责，不在这个进程内路由组件中实现。
        with self._condition:
            for port, sid in self._ports.items():
                if sid == session_id and not port.cancelled and self._broker.resolve(
                    session_id, request_id, response, port=port,
                ):
                    self._notifications.pop(request_id, None)
                    self._condition.notify_all()
                    return True
        return False
