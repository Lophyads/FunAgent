"""进程内应用协调层：管理接入、交互路由、恢复调度和可选投递。

CLI/平台负责展示与用户身份认证；AgentCore 负责模型循环和工具执行。
Gateway 拥有自建的 Agent/投递组件，只借用进程共享 ToolServices，不负责全局关闭。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Callable, Literal

from agentcore.core import AgentCore
from agentcore.interaction import InteractionPort, InteractionRequest
from agentcore.models import AgentConfig, TurnResult, TurnTrigger, UserInput
from agentcore.session_context import SessionBuildRequest
from agentcore.workers import TurnQueueFullError, TurnSubmission, submit_daemon_call
from tools.lifecycle import CleanupIssue, CleanupReport, SessionResourcesBusyError, remaining
from tools.runtime import get_tool_services
from log.logging_system import log_context
from .delivery import DeliveryAdapter, DeliveryRuntime
from .interaction import InteractionMode, _InteractionRoutes, _TurnPort

logger = logging.getLogger(__name__)


def _require_sync(callback: Any, label: str) -> None:
    if not callable(callback) or inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    ):
        raise TypeError(f"{label} must be synchronous and callable")


def _call_sync(callback):
    result = callback()
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if close is not None:
            close()
        raise TypeError("adapter lifecycle methods must be synchronous")
    return result


class GatewayRuntime:
    """组织 Agent 的应用生命周期，而不是网络服务器或另一套模型循环。"""

    def __init__(
        self, config: AgentConfig, adapter: DeliveryAdapter | None = None, *,
        interaction_mode: InteractionMode = "none",
        interaction_port: InteractionPort | None = None,
        recovery_policy: Literal["attached", "owned"] = "attached",
        owns_database: bool = False,
        owned_sessions: frozenset[str] | None = None,
        max_recovery_attempts: int = 3, **agent_options: Any,
    ):
        """配置当前接入端的交互方式和主动恢复范围。

        interaction_mode: none 不提供人工交互；poll 由宿主读取内部通知队列；
            push 调用宿主的 interaction_port.present(request)。
        interaction_port: 仅 push 模式传入的同步、快速返回的通知对象，不是 TCP 端口；
            它只展示/转交通知，答案稍后通过 resolve_interaction 提交。
        recovery_policy: attached 只扫描已接入 Session；owned 扫描显式声明的管理范围。
            恢复会提交内部 Turn，可能调用模型，不只是读取历史。
        owns_database: 允许 owned 模式扫描全库；不代表文件所有权或跨进程排他锁。
        owned_sessions: 替代全库声明的 Session 集合，同时限制 attach/submit 的接入范围；
            与 owns_database=True 互斥。未指定集合时，attached 仍只扫描已接入会话。
        max_recovery_attempts: 内部恢复尝试上限，不是 Provider HTTP 重试次数。
        adapter: 可选的最终回复投递适配器，与人工交互通知出口的职责不同。
        """
        if interaction_mode not in {"none", "poll", "push"}:
            raise ValueError("invalid interaction mode")
        if (interaction_mode == "push") != (interaction_port is not None):
            raise ValueError("only push mode requires an interaction port")
        if interaction_port is not None:
            _require_sync(getattr(interaction_port, "present", None), "InteractionPort.present")
        if recovery_policy not in {"attached", "owned"}:
            raise ValueError("invalid recovery policy")
        if recovery_policy == "owned" and not (owns_database or owned_sessions is not None):
            raise ValueError("owned recovery requires explicit database or session ownership")
        if owns_database and owned_sessions is not None:
            raise ValueError("choose database ownership or an explicit session scope")
        if owned_sessions is not None:
            owned_sessions = frozenset(owned_sessions)
            if any(not isinstance(sid, str) or not sid.strip() for sid in owned_sessions):
                raise ValueError("owned session IDs must not be blank")
        if adapter is not None:
            destination = getattr(adapter, "destination", None)
            if not isinstance(destination, str) or not destination.strip():
                raise ValueError("invalid delivery destination")
            _require_sync(getattr(adapter, "send", None), "delivery adapter send")
            for method in ("start", "close"):
                callback = getattr(adapter, method, None)
                if callback is not None:
                    _require_sync(callback, f"delivery adapter {method}")
        if "delivery_destination" in agent_options:
            raise TypeError("delivery_destination is owned by GatewayRuntime")
        self._config = config
        self.adapter = adapter
        # 借用已初始化的进程服务；关闭本 Gateway 不能使其他使用者的资源失效。
        self._services = get_tool_services()
        self._interactions = _InteractionRoutes(
            self._services.interactions, mode=interaction_mode,
            endpoint=interaction_port, capacity=config.tools.interaction_capacity,
        )
        self._recovery_policy = recovery_policy
        # None 与空集合不同：owned 模式下 None 表示已声明全库，空集合表示不接管任何会话。
        self._owned_sessions = owned_sessions
        self._max_recovery_attempts = max_recovery_attempts
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._started = False
        self._closed = False
        # attached 是当前接入范围；detached 阻止隐式重新提交；resetting 阻止清理期间的新任务。
        self._attached: set[str] = set()
        self._detached: set[str] = set()
        self._resetting: set[str] = set()
        # 只跟踪尚未收尾的提交，完成回调会移除；恢复任务也复用同一份 submission。
        self._submissions: dict[Future[TurnResult], tuple[str, TurnSubmission[TurnResult]]] = {}
        self._recoveries: dict[str, TurnSubmission[TurnResult]] = {}
        self._delivery_waiters: dict[Future[TurnResult], Future[None]] = {}
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._adapter_close: Future | None = None
        self.delivery: DeliveryRuntime | None = None
        try:
            self.agent = AgentCore(
                config, delivery_destination=adapter.destination if adapter else None,
                max_recovery_attempts=max_recovery_attempts, **agent_options,
            )
        except BaseException:
            self._interactions.close()
            try:
                self._close_adapter(time.monotonic() + config.timeouts.finalize_seconds)
            except Exception:
                logger.exception("gateway.adapter_cleanup_failed")
            raise
        try:
            if adapter is not None:
                # Agent owns these dependencies; Delivery borrows them until it drains.
                self.delivery = DeliveryRuntime(
                    self.agent._store, adapter, lease_ttl_seconds=config.lease.ttl_seconds,
                    observability=self.agent._observability,
                    queue_capacity=config.workers.queue_capacity, log_config=config,
                )
        except BaseException:
            self._cleanup_failed_start()
            raise

    def __enter__(self):
        self.start_blocking()
        return self

    def __exit__(self, *args):
        self.close_blocking()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    def start_blocking(self) -> None:
        initializing = False
        try:
            with self._start_lock:
                with self._condition:
                    if self._monitor_stop.is_set():
                        raise RuntimeError("GatewayRuntime is closing")
                    if self._started:
                        return
                initializing = True
                self.agent.start_blocking()
                start = getattr(self.adapter, "start", None)
                if start is not None and _call_sync(start) is False:
                    raise RuntimeError("delivery adapter is not ready")
                with self._condition:
                    if self._monitor_stop.is_set():
                        raise RuntimeError("GatewayRuntime is closing")
                    self._started = True
                    # 监控线程只发现并提交恢复任务；模型循环仍由 AgentCore 的 worker 执行。
                    self._monitor = threading.Thread(
                        target=self._monitor_recovery, name="gateway-recovery", daemon=True,
                    )
                    if self.delivery is not None:
                        self.delivery.submit_drain()
                    self._monitor.start()
                    self._scan_recovery()
            with log_context(self._config):
                logger.info("runtime.ready")
        except BaseException:
            if initializing:
                self._cleanup_failed_start()
            raise

    def _cleanup_failed_start(self) -> None:
        # __enter__ failures do not invoke __exit__; preserve the startup exception.
        try:
            report = self.close_blocking()
            if not report.completed:
                logger.warning("gateway.start_cleanup_incomplete issues=%s", report.issues)
        except Exception:
            logger.exception("gateway.start_cleanup_failed")

    async def start(self) -> None:
        await asyncio.shield(asyncio.wrap_future(
            submit_daemon_call(self.start_blocking, name="gateway-start"),
        ))

    def _check_open(self) -> None:
        if self._monitor_stop.is_set() or not self._started:
            raise RuntimeError("GatewayRuntime is not accepting turns")

    def attach_session(self, session_id: str) -> TurnSubmission[TurnResult] | None:
        """登记接入并检查恢复；返回恢复提交或 None，poll 宿主也要为恢复任务处理交互。"""
        if not session_id.strip():
            raise ValueError("session_id must not be blank")
        self.start_blocking()
        with self._condition:
            return self._attach_session(session_id)

    def _attach_session(self, session_id: str) -> TurnSubmission[TurnResult] | None:
        """在接入锁内先预留恢复任务的位置，保证后续用户输入不会越过同 Session 的恢复。"""
        self._check_open()
        if session_id in self._resetting:
            raise SessionResourcesBusyError("session resources are being reset")
        if self._owned_sessions is not None and session_id not in self._owned_sessions:
            raise PermissionError("session is outside the configured ownership scope")
        self._attached.add(session_id)
        self._detached.discard(session_id)
        scope = frozenset({session_id})
        self.agent._store.mark_stale_leases(scope)
        pending = self.agent._store.list_pending_recoveries(self._max_recovery_attempts, scope)
        if pending:
            return self._schedule_recovery(session_id)
        return self._recoveries.get(session_id)

    def detach_session(self, session_id: str) -> None:
        """取消本接入范围内该 Session 的任务和交互，不删除历史，也不重置共享后端。"""
        with self._condition:
            self._attached.discard(session_id)
            self._detached.add(session_id)
            for sid, submission in tuple(self._submissions.values()):
                if sid == session_id:
                    submission.cancel()
            self._interactions.detach(session_id)

    def pending_interactions(self, session_id: str | None = None) -> tuple[InteractionRequest, ...]:
        return self._interactions.pending(session_id)

    def next_interaction(
        self, *, session_id: str | None = None, timeout: float | None = None,
    ) -> InteractionRequest | None:
        return self._interactions.next(session_id=session_id, timeout=timeout)

    def resolve_interaction(self, session_id: str, request_id: str, response: Any) -> bool:
        # 答复直接完成原请求；不能排进同 Session FIFO，否则会排在正等待它的 Turn 后面。
        return self._interactions.resolve(session_id, request_id, response)

    def _track(
        self, session_id: str, submission: TurnSubmission[TurnResult], port: _TurnPort | None,
    ) -> TurnSubmission[TurnResult]:
        self._interactions.watch_cancellation(port, submission.cancel_event)
        self._submissions[submission.future] = (session_id, submission)
        if self.delivery is not None:
            self._delivery_waiters[submission.future] = Future()
        submission.future.add_done_callback(
            lambda future: self._complete_submission(session_id, future, port),
        )
        return submission

    def _complete_submission(
        self, session_id: str, future: Future[TurnResult], port: _TurnPort | None,
    ) -> None:
        with self._condition:
            self._submissions.pop(future, None)
            recovery = self._recoveries.get(session_id)
            if recovery is not None and recovery.future is future:
                self._recoveries.pop(session_id)
            self._interactions.release(port)
            if self.delivery is not None and not self._monitor_stop.is_set() and not future.cancelled():
                try:
                    drain = self.delivery.submit_drain(session_id)
                except RuntimeError:
                    # The durable obligation remains for a later drain.
                    pass
                else:
                    drain.add_done_callback(lambda _: self._finish_delivery(future))
                    return
            self._finish_delivery(future)

    def _finish_delivery(self, turn_future: Future[TurnResult]) -> None:
        with self._condition:
            waiter = self._delivery_waiters.pop(turn_future, None)
        if waiter is not None:
            waiter.set_result(None)

    def submit_turn(self, user_input: UserInput | str, session_id: str | None = None, *,
                    session_request: SessionBuildRequest | None = None) -> TurnSubmission[TurnResult]:
        """准备接入和交互上下文后提交；实际排队、执行与取消令牌由 AgentCore 管理。"""
        self.start_blocking()
        sid = str(uuid.uuid4()) if session_id is None else session_id
        with self._condition:
            self._check_open()
            if sid in self._detached:
                raise RuntimeError("attach the detached session before submitting")
            if not sid.strip():
                raise ValueError("session_id must not be blank")
            self._attach_session(sid)
            # 同一宿主的每个 Turn 都绑定独立端口，取消一轮不会关闭其他轮的通知出口。
            host, port = self._interactions.bind(sid)
            try:
                submission = self.agent.submit_turn(
                    user_input, sid, session_request=session_request, host_context=host,
                )
            except BaseException:
                self._interactions.release(port)
                raise
            return self._track(sid, submission, port)

    def run_turn_blocking(self, user_input, session_id=None, **kwargs) -> TurnResult:
        """阻塞等结果；poll CLI 应用 submit_turn + 消息泵，避免无人读取待答问题。"""
        submission = self.submit_turn(user_input, session_id, **kwargs)
        try:
            result = submission.future.result()
            with self._condition:
                delivery = self._delivery_waiters.get(submission.future)
            if delivery is not None:
                delivery.result()
            return result
        except KeyboardInterrupt:
            submission.cancel()
            raise

    async def run_turn(self, user_input, session_id=None, **kwargs) -> TurnResult:
        submission = self.submit_turn(user_input, session_id, **kwargs)
        try:
            result = await asyncio.shield(asyncio.wrap_future(submission.future))
            with self._condition:
                delivery = self._delivery_waiters.get(submission.future)
            if delivery is not None:
                await asyncio.shield(asyncio.wrap_future(delivery))
            return result
        except asyncio.CancelledError:
            submission.cancel()
            raise

    def _schedule_recovery(self, session_id: str) -> TurnSubmission[TurnResult]:
        with self._condition:
            if session_id in self._recoveries:
                return self._recoveries[session_id]
            self._check_open()
            # 预留 FIFO 位置不占用执行 worker；后续装配失败必须释放预留和交互端口。
            reservation = self.agent.reserve_recovery(session_id)
            port = None
            try:
                host, port = self._interactions.bind(session_id)
                lifecycle = self.agent._store.get_session_recovery(session_id)
                submission = self.agent.submit_recovery(
                    TurnTrigger(kind="internal_recovery", session_id=session_id,
                                recovery_reason=lifecycle["resume_reason"] if lifecycle else None),
                    reservation, host_context=host,
                )
            except BaseException:
                reservation.cancel()
                self._interactions.release(port)
                raise
            self._recoveries[session_id] = submission
            return self._track(session_id, submission, port)

    def _scan_recovery(self):
        with self._condition:
            if self._monitor_stop.is_set():
                return
            # Store 把 None 解释为全库。只有 owned + 显式所有权声明才能走该范围；
            # 默认 attached 防止打开一个 CLI 就自动继续数据库里其他宿主的未完成任务。
            scope = self._owned_sessions if self._recovery_policy == "owned" else frozenset(self._attached)
            self.agent._store.mark_stale_leases(scope)
            pending = self.agent._store.list_pending_recoveries(self._max_recovery_attempts, scope)
            for sid in pending:
                if self._monitor_stop.is_set():
                    break
                if sid in self._detached or sid in self._resetting:
                    continue
                try:
                    self._schedule_recovery(sid)
                except TurnQueueFullError:
                    break

    def _monitor_recovery(self):
        interval = min(1.0, max(0.05, self._config.lease.ttl_seconds / 3))
        while not self._monitor_stop.wait(interval):
            try:
                self._scan_recovery()
            except Exception as exc:
                logger.error("recovery.monitor_failed error_type=%s", type(exc).__name__)

    def reset_session_resources(self, session_id: str) -> CleanupReport:
        """重置已接入且无在途提交的会话资源；不删除消息或永久授权。"""
        with self._condition:
            self._check_open()
            if session_id not in self._attached:
                raise PermissionError("session is not attached")
            if session_id in self._resetting or any(
                sid == session_id for sid, _ in self._submissions.values()
            ):
                raise SessionResourcesBusyError("session still has queued or running turns")
            self._resetting.add(session_id)
        # 后端关闭可能阻塞：释放接入锁再清理，resetting 标记负责拒绝同 Session 新提交。
        try:
            return self._services.reset_session_resources(self.agent.session_resource_key(session_id))
        finally:
            with self._condition:
                self._resetting.discard(session_id)
                self._condition.notify_all()

    def close_blocking(self, *, timeout: float | None = None) -> CleanupReport:
        """有界关闭自有组件；各步骤共享总 deadline，超时通过报告保留未完成状态。"""
        deadline = time.monotonic() + (
            self._config.timeouts.finalize_seconds if timeout is None else max(0, timeout)
        )
        if not self._close_lock.acquire(timeout=remaining(deadline)):
            return self._busy_report("gateway", "close_busy")
        try:
            if self._closed:
                return CleanupReport()
            report = self._stop_admission(deadline)
            if not report.completed:
                return report
            report = self._close_components(deadline)
            if report.completed:
                with self._condition:
                    self._attached.clear()
                    self._detached.clear()
                    self._closed = True
            return report
        finally:
            self._close_lock.release()

    def _stop_admission(self, deadline: float) -> CleanupReport:
        """Revoke input first, then wait for admitted setup/reset operations."""
        self._monitor_stop.set()
        self._interactions.close()
        if not self._condition.acquire(timeout=remaining(deadline)):
            return self._busy_report("admission")
        try:
            while self._resetting:
                if remaining(deadline) == 0:
                    return self._busy_report("session_reset")
                self._condition.wait(remaining(deadline))
        finally:
            self._condition.release()
        if not self._start_lock.acquire(timeout=remaining(deadline)):
            return self._busy_report("startup")
        self._start_lock.release()
        return CleanupReport()

    def _close_components(self, deadline: float) -> CleanupReport:
        issues: list[CleanupIssue] = []

        def clean(resource: str, action: Callable[[], CleanupReport | bool | None]) -> bool:
            try:
                result = action()
                if isinstance(result, CleanupReport):
                    issues.extend(result.issues)
                    return result.completed
                if result is False:
                    issues.extend(self._busy_report(resource).issues)
                    return False
                return True
            except Exception as exc:
                issues.append(CleanupIssue(
                    resource, "host", "close", type(exc).__name__, isinstance(exc, TimeoutError),
                ))
                return False

        monitor_stopped = clean("recovery", lambda: self._stop_monitor(remaining(deadline)))
        turns_stopped = clean("turns", lambda: self.agent.stop_turns(remaining(deadline)))
        delivery_stopped = self.delivery is None or clean(
            "delivery", lambda: self.delivery.close(remaining(deadline)),
        )
        if delivery_stopped:
            clean("adapter", lambda: self._close_adapter(deadline))
        # Delivery 和恢复线程借用 Agent 的 Store/观测组件，先让使用者退出再释放依赖。
        # 此处不调用 shutdown_tool_system：共享工具服务由最外层进程入口统一关闭。
        adapter_running = self._adapter_close is not None and not self._adapter_close.done()
        if monitor_stopped and turns_stopped and delivery_stopped and not adapter_running:
            clean("agent", lambda: self.agent.close_blocking(remaining(deadline)))
        return CleanupReport(tuple(issues))

    def _stop_monitor(self, timeout: float) -> bool:
        if self._monitor is None or self._monitor.ident is None:
            return True
        self._monitor.join(timeout)
        return not self._monitor.is_alive()

    @staticmethod
    def _busy_report(resource: str, code: str = "worker_busy") -> CleanupReport:
        return CleanupReport((CleanupIssue(resource, "host", "close", code, True),))

    def _close_adapter(self, deadline: float) -> None:
        close = getattr(self.adapter, "close", None)
        if close is None:
            return
        if self._adapter_close is None:
            if remaining(deadline) == 0:
                raise TimeoutError("adapter cleanup budget exhausted")
            self._adapter_close = submit_daemon_call(
                lambda: _call_sync(close), name="gateway-adapter-close",
            )
        try:
            self._adapter_close.result(timeout=remaining(deadline))
        except Exception:
            if self._adapter_close.done():
                self._adapter_close = None
            raise

    async def aclose(self, *, timeout: float | None = None) -> CleanupReport:
        return await asyncio.shield(asyncio.wrap_future(submit_daemon_call(
            lambda: self.close_blocking(timeout=timeout), name="gateway-close",
        )))


__all__ = ["GatewayRuntime"]
