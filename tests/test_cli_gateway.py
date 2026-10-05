"""Offline acceptance tests for CLI -> Gateway -> Agent ownership and concurrency."""

from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agentcore import (
    AgentCore, GatewayRuntime, LeasePolicy, Message, NormalizedModelResponse,
    SQLiteStore, ToolCall, ToolEntry, WorkerPolicy,
)
from agentcore import cli
from agentcore.interaction import InteractionBroker, InteractionUnavailable
from agentcore.workers import TurnDispatcher, WorkerPoolClosedError, submit_daemon_call
from gateway.interaction import _InteractionRoutes
from tools.lifecycle import CleanupReport, SessionResourcesBusyError
from tools.runtime import get_tool_registry, get_tool_services
from conftest import ScriptedTransport, scripted_provider_stack
from test_interactive_tools import ask, clarify_response


def make_gateway(config, *responses, adapter=None, **options):
    transport = ScriptedTransport(*responses)
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport})
    host = GatewayRuntime(
        config, adapter, provider_resolver=resolver, api_modes=modes, **options,
    )
    return host, transport


def seed_crashed_sessions(config, *session_ids):
    store = SQLiteStore(config.database_path)
    store.open()
    try:
        for sid in session_ids:
            lease = store.acquire_lease(
                session_id=sid, owner_id="dead", turn_id=sid, ttl_seconds=30,
            )
            store.create_session(
                sid, system_prompt_bytes=b"system",
                user_message=Message.user("unfinished"), fence=lease.fence,
            )
    finally:
        store.close()
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("UPDATE session_leases SET expires_at=?", (time.time() - 1,))


class Adapter:
    destination = "test:cli-gateway"

    def __init__(self):
        self.closed = 0
        self.sent = []

    def send(self, *, obligation_id, destination, text):
        self.sent.append((obligation_id, destination, text))
        return True

    def close(self):
        self.closed += 1


def test_real_cli_clarification_and_denied_approval_stay_in_one_turn(
    config_factory, monkeypatch, capsys,
):
    config = config_factory()
    final = NormalizedModelResponse(content="CLI final", finish_reason="stop")
    approval = NormalizedModelResponse(tool_calls=[
        ToolCall(id="approval", name="test_action", arguments="{}"),
    ], finish_reason="tool_calls")
    host, transport = make_gateway(
        config, clarify_response(), approval, final, interaction_mode="poll",
    )
    calls = []

    class Args(BaseModel):
        pass

    registry = get_tool_registry()
    registry.replace_all((*registry.entries(), ToolEntry(
        tool_id="test.action", implementation_version="1", name="test_action",
        description="Test approval", args_model=Args, requires_approval=True,
        handler=lambda args, context: calls.append("executed"),
    )))
    inputs = iter(["choose and act", "2", "deny", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "GatewayRuntime", lambda *args, **kwargs: host)
    from memory.feature import MemoryConfig, SqliteConfig
    monkeypatch.setattr(cli, "load_memory_config", lambda path: MemoryConfig(
        review_every_turns=0, sqlite=SqliteConfig(config.database_path.parent / "memory.db"),
    ))
    monkeypatch.setattr(cli, "configure_logging", lambda config: SimpleNamespace(
        close=lambda **kwargs: CleanupReport(),
    ))
    assert cli.run_cli(["chat", "--session", "cli-e2e"]) == 0
    assert capsys.readouterr().out.count("assistant> CLI final") == 1
    assert calls == [] and host.delivery is None and transport.closed
    assert len(transport.requests) == 3
    store = SQLiteStore(config.database_path)
    store.open()
    try:
        messages = store.load_messages("cli-e2e")
        assert [m.content for m in messages if m.role == "user"] == ["choose and act"]
        clarification, denied = [m for m in messages if m.role == "tool"]
        assert json.loads(clarification.content)["result"]["responses"][0]["user_response"] == "B"
        assert denied.tool_outcome_state == "denied"
        assert store.list_pending_deliveries() == []
    finally:
        store.close()


def test_poll_scope_does_not_consume_another_sessions_request(config_factory, final_response):
    host, _ = make_gateway(config_factory(), clarify_response(), final_response, interaction_mode="poll")
    with host:
        submission = host.submit_turn("ask", "other")
        assert host.next_interaction(session_id="current", timeout=0.1) is None
        request = host.next_interaction(session_id="other", timeout=2)
        assert request is not None
        assert host.resolve_interaction("other", request.request_id, {"answers": {"q0": "A"}})
        assert submission.future.result(2).status == "completed"


def test_poll_queue_overflow_wakes_waiter_and_revoked_port_cannot_notify():
    broker = InteractionBroker(capacity=4)
    routes = _InteractionRoutes(broker, mode="poll", endpoint=None, capacity=1)
    _, first = routes.bind("s1")
    _, second = routes.bind("s1")
    presented = threading.Event()
    original = first._notify

    def notify(port, request):
        original(port, request)
        presented.set()

    first._notify = notify
    try:
        with ThreadPoolExecutor(2) as pool:
            waiting = pool.submit(ask, broker, first)
            assert presented.wait(2)
            overflow = pool.submit(ask, broker, second)
            with pytest.raises(InteractionUnavailable):
                overflow.result(1)
            request = routes.next(timeout=1)
            routes.release(first)
            with pytest.raises(InteractionUnavailable):
                first.present(request)
            assert not routes.resolve("s1", request.request_id, {"answers": {"q0": "late"}})
            from agentcore.interfaces import TurnCancelled
            with pytest.raises(TurnCancelled):
                waiting.result(1)
    finally:
        routes.close()
        assert broker.close(2)


@pytest.mark.parametrize("returns_awaitable", [False, True])
def test_bad_push_notification_finishes_without_human_timeout(
    config_factory, final_response, returns_awaitable,
):
    class Port:
        def present(self, request):
            if returns_awaitable:
                async def invalid():
                    pass
                return invalid()
            raise RuntimeError("endpoint disconnected")

    host, _ = make_gateway(
        config_factory(), clarify_response(), final_response,
        interaction_mode="push", interaction_port=Port(),
    )
    with host:
        result = host.submit_turn("ask", "push").future.result(2)
        assert result.tool_outcomes[0].state == "failed"
        assert host.pending_interactions() == ()


def test_cli_only_pumps_current_session_and_reports_late_answer(monkeypatch, capsys):
    from agentcore.interaction import ClarifyPayload, InteractionRequest, Question

    request = InteractionRequest(
        session_id="mine", turn_id="turn", tool_call_id="call",
        request_id="request", kind="clarify",
        payload=ClarifyPayload((
            Question(qid="a", question="First?"),
            Question(qid="b", question="Second?"),
        )), created_at=time.time(), expires_at=time.time() + 60,
    )
    future = Future()

    class Host:
        def next_interaction(self, *, session_id, timeout):
            assert session_id == "mine"
            return request
        def pending_interactions(self, session_id):
            assert session_id == "mine"
            return () if future.done() else (request,)
        def resolve_interaction(self, *args):
            return False

    def answer(prompt):
        future.set_result(SimpleNamespace(
            warnings=[], final_text="done", status="completed", error=None,
        ))
        return "late"

    monkeypatch.setattr("builtins.input", answer)
    cli._pump(Host(), SimpleNamespace(future=future), "mine")
    output = capsys.readouterr()
    assert "First?" in output.out and "Second?" not in output.out
    assert "expired or cancelled" in output.err

def test_detach_cancels_owned_work_and_explicit_attach_allows_new_turn(
    config_factory, final_response,
):
    host, _ = make_gateway(config_factory(), clarify_response(), final_response, interaction_mode="poll")
    with host:
        active = host.submit_turn("ask", "sid")
        request = host.next_interaction(timeout=2)
        queued = host.submit_turn("queued", "sid")
        host.detach_session("sid")
        assert active.future.result(2).status == "interrupted"
        assert queued.future.result(2).status == "interrupted"
        assert not host.resolve_interaction("sid", request.request_id, {"answers": {"q0": "late"}})
        with pytest.raises(RuntimeError, match="attach"):
            host.submit_turn("rejected", "sid")
        host.attach_session("sid")
        assert host.run_turn_blocking("new", "sid").status == "completed"


def test_close_racing_with_admission_is_bounded_and_leaves_no_orphan(
    config_factory, final_response, monkeypatch,
):
    host, _ = make_gateway(config_factory(), final_response, interaction_mode="poll")
    entered, release = threading.Event(), threading.Event()
    original = host.agent.submit_turn
    def paused_submit(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(host.agent, "submit_turn", paused_submit)
    host.start_blocking()
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(host.submit_turn, "hello", "race")
            assert entered.wait(2)
            started = time.monotonic()
            report = host.close_blocking(timeout=0.03)
            assert time.monotonic() - started < 0.4
            assert any(issue.resource == "admission" for issue in report.issues)
            release.set()
            submission = pending.result(2)
            assert host.close_blocking(timeout=2).completed
            assert submission.future.done()
            assert host.pending_interactions() == ()
            assert host._submissions == {}
    finally:
        release.set()
        host.close_blocking(timeout=2)


def test_reset_excludes_new_turns_and_close_keeps_its_store(config_factory, monkeypatch):
    host, _ = make_gateway(config_factory())
    entered, release = threading.Event(), threading.Event()
    def reset(key):
        entered.set()
        assert release.wait(3)
        return CleanupReport()
    monkeypatch.setattr(get_tool_services(), "reset_session_resources", reset)
    host.attach_session("sid")
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(host.reset_session_resources, "sid")
            assert entered.wait(2)
            with pytest.raises(SessionResourcesBusyError):
                host.submit_turn("racing", "sid")
            report = host.close_blocking(timeout=0.02)
            assert any(issue.resource == "session_reset" for issue in report.issues)
            assert not host.agent._closed
            release.set()
            assert pending.result(2).completed
            assert host.close_blocking(timeout=2).completed
    finally:
        release.set()
        host.close_blocking(timeout=2)


def test_failed_adapter_start_cleans_owned_resources_only(config_factory, final_response):
    class FailingAdapter(Adapter):
        def start(self):
            raise ValueError("startup failed")
    adapter = FailingAdapter()
    config = config_factory()
    host, transport = make_gateway(config, adapter=adapter)
    services = get_tool_services()
    with pytest.raises(ValueError, match="startup failed"):
        host.start_blocking()
    assert host.agent._closed and transport.closed and adapter.closed == 1
    assert get_tool_services() is services
    other, _ = make_gateway(config, final_response)
    with other:
        assert other.run_turn_blocking("still available").status == "completed"


def test_delivery_construction_failure_closes_created_agent(config_factory, monkeypatch):
    import gateway.gateway as gateway_module
    created = []
    def agent(*args, **kwargs):
        value = AgentCore(*args, **kwargs)
        created.append(value)
        return value
    def fail(*args, **kwargs):
        raise ValueError("delivery initialization failed")
    monkeypatch.setattr(gateway_module, "AgentCore", agent)
    monkeypatch.setattr(gateway_module, "DeliveryRuntime", fail)
    adapter = Adapter()
    with pytest.raises(ValueError, match="delivery initialization"):
        make_gateway(config_factory(), adapter=adapter)
    assert created[0]._closed and adapter.closed == 1


def test_blocked_start_can_close_with_a_shared_deadline(config_factory):
    entered, release = threading.Event(), threading.Event()
    class BlockingAdapter(Adapter):
        def start(self):
            entered.set()
            assert release.wait(3)
    adapter = BlockingAdapter()
    host, _ = make_gateway(config_factory(), adapter=adapter)
    try:
        with ThreadPoolExecutor(1) as pool:
            startup = pool.submit(host.start_blocking)
            assert entered.wait(2)
            started = time.monotonic()
            report = host.close_blocking(timeout=0.03)
            assert time.monotonic() - started < 0.4
            assert any(issue.resource == "startup" for issue in report.issues)
            release.set()
            with pytest.raises(RuntimeError, match="closing"):
                startup.result(2)
            assert host.close_blocking(timeout=2).completed
            assert adapter.closed == 1
    finally:
        release.set()
        host.close_blocking(timeout=2)


def test_delivery_timeout_retains_store_until_send_and_adapter_close_finish(
    config_factory, final_response,
):
    entered, release = threading.Event(), threading.Event()
    class BlockingAdapter(Adapter):
        def send(self, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().send(**kwargs)
        def close(self):
            assert host.agent._store.load_session("delivery") is not None
            super().close()
    adapter = BlockingAdapter()
    host, _ = make_gateway(config_factory(), final_response, adapter=adapter)
    try:
        result = host.submit_turn("hello", "delivery").future.result(2)
        assert result.status == "completed" and entered.wait(2)
        report = host.close_blocking(timeout=0.03)
        assert any(issue.resource == "delivery" for issue in report.issues)
        assert host.agent._store.load_session("delivery") is not None
        assert adapter.closed == 0
        release.set()
        assert host.close_blocking(timeout=2).completed
        assert host.agent._closed and adapter.closed == 1 and len(adapter.sent) == 1
    finally:
        release.set()
        host.close_blocking(timeout=2)


def test_adapter_close_failure_allows_safe_cleanup_and_retry(config_factory):
    class FlakyAdapter(Adapter):
        def close(self):
            super().close()
            if self.closed == 1:
                raise ValueError("close failed")
    adapter = FlakyAdapter()
    host, transport = make_gateway(config_factory(), adapter=adapter)
    host.start_blocking()
    report = host.close_blocking(timeout=2)
    assert any(issue.resource == "adapter" for issue in report.issues)
    assert host.agent._closed and transport.closed
    assert host.close_blocking(timeout=2).completed and adapter.closed == 2


def test_recovery_backpressure_does_not_block_startup(config_factory, final_response):
    config = config_factory(workers=WorkerPolicy(queue_capacity=1, turn_workers=1)).model_copy(update={
        "lease": LeasePolicy(ttl_seconds=0.3, renew_interval_seconds=0.05, wait_timeout_seconds=1),
    })
    seed_crashed_sessions(config, "one", "two")
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    finish_second = threading.Event()
    def first_response(request):
        entered.set()
        assert release.wait(3)
        return final_response
    def second_response(request):
        second.set()
        assert finish_second.wait(3)
        return final_response
    host, _ = make_gateway(
        config, first_response, second_response, recovery_policy="owned", owns_database=True,
    )
    try:
        startup = submit_daemon_call(host.start_blocking, name="test-start")
        startup.result(1)
        assert entered.wait(1) and not second.is_set()
        release.set()
        assert second.wait(2)
        recovery = host.attach_session("two")
        finish_second.set()
        assert recovery.future.result(timeout=2).status == "completed"
        for sid in ("one", "two"):
            assert not host.agent._store.get_session_recovery(sid)["resume_pending"]
    finally:
        release.set()
        finish_second.set()
        host.close_blocking(timeout=2)


def test_failed_recovery_binding_releases_fifo_reservation(
    config_factory, final_response, monkeypatch,
):
    config = config_factory(workers=WorkerPolicy(queue_capacity=1, turn_workers=1))
    seed_crashed_sessions(config, "sid")
    host, _ = make_gateway(config, final_response, interaction_mode="poll")
    def fail(sid):
        raise InteractionUnavailable("closed route")
    host.start_blocking()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(host._interactions, "bind", fail)
            with pytest.raises(InteractionUnavailable):
                host.attach_session("sid")
        submission = host.attach_session("sid")
        assert submission.future.result(2).status == "completed"
    finally:
        host.close_blocking(timeout=2)


def test_dispatcher_cancellation_callbacks_can_reenter_without_lock_deadlock():
    dispatcher = TurnDispatcher(max_workers=1, capacity=2)
    reservation = dispatcher.reserve("sid")
    waiting = dispatcher.submit("sid", lambda cancel: None)
    callback_ran = threading.Event()
    def cancelled(future):
        with pytest.raises(WorkerPoolClosedError):
            dispatcher.reserve("other")
        callback_ran.set()
    waiting.future.add_done_callback(cancelled)
    shutdown = submit_daemon_call(lambda: dispatcher.close(timeout=1), name="test-close")
    assert shutdown.result(2) and callback_ran.is_set()
    reservation.cancel()


def test_cli_cleanup_preserves_business_error_and_attempts_all_steps(monkeypatch, capsys):
    calls = []
    budgets = []
    config = SimpleNamespace(timeouts=SimpleNamespace(finalize_seconds=5))

    class Host:
        def start_blocking(self):
            calls.append("start")
        def close_blocking(self, *, timeout):
            calls.append("gateway")
            budgets.append(timeout)
            raise OSError("gateway cleanup failed")

    def close_tools(*, timeout):
        calls.append("tools")
        budgets.append(timeout)
        raise RuntimeError("tools cleanup failed")

    def close_logging(*, timeout):
        calls.append("logging")
        budgets.append(timeout)
        return CleanupReport()

    def run(*args):
        raise ValueError("original business failure")

    class Feature:
        settings = SimpleNamespace(enabled=True)
        prompt_builder = object()
        review_scheduler = object()
        def __init__(self, *args):
            pass
        def install_tool(self, registry):
            calls.append("install_memory")
        def close(self, *, timeout):
            calls.append("memory")
            budgets.append(timeout)
            return True

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.ToolSystemConfig, "from_agent_config", lambda config: object())
    monkeypatch.setattr(cli, "initialize_tool_system", lambda config: calls.append("initialize"))
    monkeypatch.setattr(cli, "load_memory_config", lambda path: object())
    monkeypatch.setattr(cli, "MemoryFeature", Feature)
    monkeypatch.setattr(cli, "get_tool_registry", lambda: object())
    monkeypatch.setattr(cli, "configure_logging", lambda config: SimpleNamespace(close=close_logging))
    monkeypatch.setattr(cli, "GatewayRuntime", lambda *args, **kwargs: Host())
    monkeypatch.setattr(cli, "shutdown_tool_system", close_tools)
    monkeypatch.setattr(cli, "_chat_loop", run)
    with pytest.raises(ValueError, match="original business failure"):
        cli._chat(SimpleNamespace(config="unused.toml", session="sid"))
    assert calls == ["initialize", "install_memory", "start", "gateway", "memory", "tools", "logging"]
    assert 5 >= budgets[0] >= budgets[1] >= budgets[2] >= budgets[3] >= 0
    output = capsys.readouterr()
    assert "gateway cleanup failed" in output.err and "tools cleanup failed" in output.err

def test_agent_initialization_failure_unwinds_workers_and_adapter(config_factory, monkeypatch):
    import agentcore.core as core_module
    config = config_factory()
    transport = ScriptedTransport()
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport})
    before = set(threading.enumerate())
    def fail(*args, **kwargs):
        raise ValueError("dispatcher failed")
    monkeypatch.setattr(core_module, "TurnDispatcher", fail)
    adapter = Adapter()
    with pytest.raises(ValueError, match="dispatcher failed"):
        GatewayRuntime(config, adapter, provider_resolver=resolver, api_modes=modes)
    assert transport.closed and adapter.closed == 1
    assert not [thread for thread in threading.enumerate() if thread not in before]


def test_gateway_rejects_async_callable_endpoint_before_allocating(config_factory, monkeypatch):
    class Callback:
        async def __call__(self, request):
            pass
    port = SimpleNamespace(present=Callback())
    monkeypatch.setattr("gateway.gateway.AgentCore", lambda *a, **kw: pytest.fail("allocated Agent"))
    with pytest.raises(TypeError, match="synchronous"):
        GatewayRuntime(config_factory(), interaction_mode="push", interaction_port=port)


def test_cancellation_immediately_rejects_reply_before_turn_finalization(config_factory):
    host, _ = make_gateway(config_factory(), clarify_response(), interaction_mode="poll")
    with host:
        submission = host.submit_turn("ask", "sid")
        request = host.next_interaction(timeout=2)
        submission.cancel()
        assert host.pending_interactions() == ()
        assert not host.resolve_interaction("sid", request.request_id, {"answers": {"q0": "late"}})
        assert submission.future.result(2).status == "interrupted"


async def test_async_gateway_cancellation_preserves_background_finalization(config_factory):
    host, _ = make_gateway(config_factory(), clarify_response(), interaction_mode="poll")
    await host.start()
    try:
        task = asyncio.create_task(host.run_turn("ask", "async"))
        request = await asyncio.to_thread(host.next_interaction, session_id="async", timeout=2)
        assert request is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not host.resolve_interaction("async", request.request_id, {"answers": {"q0": "late"}})
        assert (await host.aclose(timeout=2)).completed
    finally:
        await host.aclose(timeout=2)


def test_cli_pumps_attached_recovery_before_reading_new_input(
    config_factory, final_response, monkeypatch, capsys,
):
    config = config_factory()
    seed_crashed_sessions(config, "resume", "other")
    host, transport = make_gateway(config, clarify_response(), final_response, interaction_mode="poll")
    prompts = []
    inputs = iter(["1", "/exit"])
    def input_line(prompt):
        prompts.append(prompt)
        return next(inputs)
    monkeypatch.setattr("builtins.input", input_line)
    with host:
        assert cli._chat_loop(host, "resume") == 0
        assert prompts == ["answer> ", "you> "]
        assert len(transport.requests) == 2
        assert capsys.readouterr().out.count("assistant> done") == 1
        assert [m.content for m in host.agent._store.load_messages("resume") if m.role == "user"] == ["unfinished"]
        assert not host.agent._store.get_session_recovery("other")["resume_pending"]


def test_cli_ctrl_c_during_interaction_cancels_turn_and_returns_130(config_factory, monkeypatch):
    host, _ = make_gateway(config_factory(), clarify_response(), interaction_mode="poll")
    def input_line(prompt):
        if prompt == "you> ":
            return "ask"
        raise KeyboardInterrupt()
    monkeypatch.setattr("builtins.input", input_line)
    with host:
        assert cli._chat_loop(host, "interrupt") == 130
        assert host.pending_interactions() == ()
        assert host.agent.stop_turns(2).completed

def test_completed_recovery_is_not_retained_by_a_running_gateway(config_factory, final_response):
    config = config_factory()
    seed_crashed_sessions(config, "sid")
    host, _ = make_gateway(config, final_response, final_response)
    with host:
        recovery = host.attach_session("sid")
        future = weakref.ref(recovery.future)
        assert recovery.future.result(timeout=2).status == "completed"
        del recovery
        # A later turn in the same FIFO lane proves recovery callbacks have exited.
        assert host.run_turn_blocking("next", "sid").status == "completed"
        gc.collect()
        assert future() is None
