from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from agentcore import (
    AgentCore, GatewayRuntime, HostCapabilities, HumanApprovalRequired,
    NormalizedModelResponse, SessionBuildRequest, SQLiteStore,
    ToolCall, ToolEntry, ToolRegistry, TurnHostContext, WorkerPolicy,
)
from agentcore.approval import ApprovalGrantStore
from agentcore.interaction import ApprovalPayload, InteractionBroker, InteractionUnavailable
from tools.config import ToolSystemConfig, WorkspaceConfig
from tools.lifecycle import ResourceUnavailableError, SessionResourceKey
from tools.runtime import (
    ToolSystemConfigConflict, ToolSystemNotInitialized, get_tool_registry,
    get_tool_services, initialize_tool_system, shutdown_tool_system,
)
from tools import runtime
from conftest import ScriptedTransport, configure_tools, scripted_provider_stack
from test_interactive_tools import QueuePort, ask, clarify_response


def gateway(config, *responses, **options):
    resolver, modes, _ = scripted_provider_stack(config, {"primary": ScriptedTransport(*responses)})
    return GatewayRuntime(config, provider_resolver=resolver, api_modes=modes, **options)


def test_initialization_is_explicit_idempotent_and_process_owned(config_factory, final_response):
    config = config_factory()
    with pytest.raises(ToolSystemNotInitialized):
        AgentCore(config)
    system = ToolSystemConfig.from_agent_config(config)
    initialize_tool_system(system)
    registry, services = get_tool_registry(), get_tool_services()
    initialize_tool_system(system)
    assert get_tool_registry() is registry and get_tool_services() is services
    with pytest.raises(ToolSystemConfigConflict):
        initialize_tool_system(replace(system, tools=system.tools.model_copy(update={"interaction_capacity": 2})))
    with gateway(config, final_response) as one, gateway(config, final_response) as two:
        assert one.agent._tool_registry is two.agent._tool_registry is registry
        assert one.close_blocking().completed
        assert get_tool_services() is services
        assert two.run_turn_blocking("still open", "s2").status == "completed"
    assert shutdown_tool_system(timeout=2).completed
    with pytest.raises(ToolSystemNotInitialized):
        initialize_tool_system(system)


def test_discovery_failure_leaves_no_partial_runtime(monkeypatch):
    original = runtime.discover_builtin_tools
    def fail(registry):
        raise ValueError("broken test module")
    monkeypatch.setattr(runtime, "discover_builtin_tools", fail)
    with pytest.raises(ValueError, match="broken test module"):
        initialize_tool_system(ToolSystemConfig())
    assert runtime._state is runtime.ToolSystemState.UNINITIALIZED
    with pytest.raises(ToolSystemNotInitialized):
        get_tool_registry()
    monkeypatch.setattr(runtime, "discover_builtin_tools", original)
    initialize_tool_system(ToolSystemConfig())
    assert get_tool_registry().generation == 1


def test_registry_replacement_validates_entire_batch(config_factory):
    configure_tools(config_factory())
    registry = get_tool_registry()
    generation, entries = registry.generation, registry.entries()
    with pytest.raises(ValueError, match="duplicate"):
        registry.replace_all((*entries, entries[0]))
    assert registry.generation == generation
    assert registry.entries() == entries
    with pytest.raises(TypeError, match="callable"):
        registry.replace_all((replace(entries[0], handler=None),))
    assert registry.generation == generation
    assert not hasattr(registry, "services") and not hasattr(registry, "workspace_root")


def test_session_keys_isolate_session_grants():
    grants = ApprovalGrantStore()
    key = SessionResourceKey("same-id")
    grants.grant(key, "write", "session")
    assert grants.allowed(SessionResourceKey("same-id"), "write")
    assert not grants.allowed(SessionResourceKey("other-id"), "write")


def test_turn_workspace_isolation_without_chdir(config_factory, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "note.txt").write_text("one", encoding="utf-8")
    (two / "note.txt").write_text("two", encoding="utf-8")
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path),
    })
    read = NormalizedModelResponse(tool_calls=[ToolCall(
        id="read", name="read_file", arguments='{"path":"note.txt"}',
    )], finish_reason="tool_calls")
    final = NormalizedModelResponse(content="done", finish_reason="stop")
    before = Path.cwd()
    with gateway(config, read, final) as first, gateway(config, read, final) as second:
        request = SessionBuildRequest.from_defaults(config.session_defaults)
        s1 = first.submit_turn("read", "s1", session_request=replace(request, workspace=one))
        s2 = second.submit_turn("read", "s2", session_request=replace(request, workspace=two))
        assert s1.future.result(3).status == "completed"
        assert s2.future.result(3).status == "completed"
        for agent, sid, expected in ((first.agent, "s1", "one"), (second.agent, "s2", "two")):
            tool = next(m for m in agent._store.load_messages(sid) if m.role == "tool")
            assert json.loads(tool.content)["result"]["lines"][0]["text"] == expected
    assert Path.cwd() == before


def test_workspace_change_and_reset_preserve_history(config_factory, tmp_path, final_response):
    from test_tool_execution_resources import StatefulBrowser
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=one, allowed_root=tmp_path),
    })
    with gateway(config, final_response, final_response) as host:
        host.run_turn_blocking("hello", "s1")
        services = get_tool_services()
        key = host.agent.session_resource_key("s1")
        services.grants.grant(key, "write", "session")
        browser = StatefulBrowser()
        services._providers["browser"] = browser
        browser._session(key)
        request = replace(SessionBuildRequest.from_defaults(config.session_defaults), workspace=two)
        assert host.run_turn_blocking("again", "s1", session_request=request).status == "completed"
        assert key in browser._sessions and not browser.closed
        assert Path(host.agent._store.load_session("s1").workspace) == two
        assert host.reset_session_resources("s1").completed
        assert browser.closed == [key] and not services.grants.allowed(key, "write")
        assert host.agent._store.load_messages("s1")
        assert Path(host.agent._store.load_session("s1").workspace) == two


def test_replacement_agent_keeps_session_grants(config_factory, final_response):
    config = config_factory()
    with gateway(config, final_response) as first:
        first.run_turn_blocking("one", "stable")
        key = first.agent.session_resource_key("stable")
        services = get_tool_services()
        services.grants.grant(key, "write", "session")
    with gateway(config, final_response) as replacement:
        replacement.run_turn_blocking("two", "stable")
        assert replacement.agent.session_resource_key("stable") == key
        assert services.grants.allowed(key, "write")


def test_two_gateways_do_not_consume_or_resolve_each_others_requests(config_factory, final_response):
    config = config_factory()
    with gateway(config, clarify_response(), final_response, interaction_mode="poll") as first, \
         gateway(config, clarify_response(), final_response, interaction_mode="poll") as second:
        a, b = first.submit_turn("a", "s1"), second.submit_turn("b", "s2")
        r1, r2 = first.next_interaction(timeout=2), second.next_interaction(timeout=2)
        assert r1.session_id == "s1" and r2.session_id == "s2"
        assert not first.resolve_interaction("s2", r2.request_id, {"answers": {"q0": "stolen"}})
        first.detach_session("s1")
        assert a.future.result(3).status == "interrupted"
        assert second.resolve_interaction("s2", r2.request_id, {"answers": {"q0": "ok"}})
        assert b.future.result(3).status == "completed"


def test_queued_turn_keeps_submission_host_context(config_factory, agent_factory, final_response):
    config = config_factory(workers=WorkerPolicy(turn_workers=1, operation_workers=2))
    entered, release = threading.Event(), threading.Event()
    def wait_model(request):
        entered.set()
        assert release.wait(3)
        return final_response
    transport = ScriptedTransport(wait_model, clarify_response(), final_response, clarify_response(), final_response)
    with agent_factory(config, transports={"primary": transport}) as agent:
        blocking = agent.submit_turn("block", "first")
        assert entered.wait(2)
        first_port, second_port = QueuePort(), QueuePort()
        one = agent.submit_turn("one", "second", host_context=TurnHostContext(HostCapabilities(clarify=True), first_port))
        two = agent.submit_turn("two", "third", host_context=TurnHostContext(HostCapabilities(clarify=True), second_port))
        release.set()
        assert blocking.future.result(3).status == "completed"
        for submission, port, sid in ((one, first_port, "second"), (two, second_port, "third")):
            request = port.next()
            assert request.session_id == sid
            assert get_tool_services().interactions.resolve(sid, request.request_id, {"answers": {"q0": "ok"}}, port=port)
            assert submission.future.result(3).status == "completed"


def test_headless_agent_filters_clarify(config_factory, agent_factory, final_response):
    transport = ScriptedTransport(final_response)
    with agent_factory(config_factory(), transports={"primary": transport}) as agent:
        agent.run_turn_blocking("hello")
    assert "clarify" not in {item["function"]["name"] for item in transport.requests[0].body["tools"]}


def test_broker_capacity_and_shutdown_are_bounded():
    broker, port = InteractionBroker(capacity=1), QueuePort()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(ask, broker, port)
            port.next()
            with pytest.raises(InteractionUnavailable):
                ask(broker, QueuePort())
            broker.cancel(owner_id="owner")
            from agentcore import TurnCancelled
            with pytest.raises(TurnCancelled):
                pending.result(2)
    finally:
        assert broker.close(1)


def test_payload_is_defensively_copied_and_serializable():
    arguments = {"items": [{"name": "original"}]}
    payload = ApprovalPayload("write", "file", arguments, ("once", "deny"))
    arguments["items"][0]["name"] = "modified"
    assert payload.arguments["items"][0]["name"] == "original"
    with pytest.raises(TypeError):
        payload.arguments["items"][0]["name"] = "edited"
    from agentcore.interaction import InteractionRequest
    request = InteractionRequest("s", "t", "c", "id", "approval", payload, time.time(), time.time() + 10)
    assert json.loads(json.dumps(request.to_dict()))["payload"]["arguments"] == {"items": [{"name": "original"}]}


def test_timed_out_stateless_handler_is_revoked_and_agent_waits_for_exit(config_factory, agent_factory, final_response):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    stale_checks = []
    borrowed = []

    class Args(BaseModel):
        pass

    def handler(args, context):
        borrowed.append(context)
        entered.set()
        try:
            assert release.wait(3)
            with pytest.raises(ResourceUnavailableError):
                context.check_active()
            stale_checks.append(True)
            return {}
        finally:
            exited.set()

    registry = ToolRegistry()
    registry.register(ToolEntry(
        tool_id="test.slow", implementation_version="1", name="slow", description="slow",
        args_model=Args, handler=handler, side_effects="none", timeout_seconds=0.05,
    ))
    response = NormalizedModelResponse(tool_calls=[ToolCall(id="slow", name="slow", arguments="{}")], finish_reason="tool_calls")
    config = config_factory()
    def next_response(request, context):
        # This turn is still running: expiry must be per call, not just Turn cancellation.
        with pytest.raises(ResourceUnavailableError):
            borrowed[0].check_active()
        return final_response

    agent = agent_factory(config, tool_registry=registry, transports={"primary": ScriptedTransport(response, next_response)})
    try:
        submission = agent.submit_turn("go", "busy")
        assert entered.wait(2)
        result = submission.future.result(3)
        assert result.tool_outcomes[0].state == "unknown"
        key, services = agent.session_resource_key("busy"), get_tool_services()
        services.grants.grant(key, "write", "session")
        # No backend operation is running, so there is no resource occupancy to track.
        assert services.reset_session_resources(key).completed
        assert not services.grants.allowed(key, "write")
        report = agent.close_blocking(timeout=0.02)
        assert not report.completed and agent._store._opened
        release.set()
        assert exited.wait(2)
        assert agent.close_blocking(timeout=2).completed
        assert stale_checks
        assert services.reset_session_resources(key).completed
    finally:
        release.set()
        agent.close_blocking(timeout=2)


def test_cleanup_failure_reports_other_resources_and_retries(config_factory):
    configure_tools(config_factory())
    services = get_tool_services()
    key = SessionResourceKey("s")
    calls = []

    class Resource:
        def __init__(self, name):
            self.name, self.fail = name, name == "first"
        def close_session(self, key, *, deadline):
            calls.append(self.name)
            if self.fail:
                self.fail = False
                raise OSError("private detail")
        def close(self, *, deadline):
            pass

    services._providers.update(first=Resource("first"), second=Resource("second"))
    report = services.reset_session_resources(key)
    assert not report.completed and set(calls) == {"first", "second"}
    assert report.issues[0].error_code == "OSError" and "private" not in repr(report)
    assert services.reset_session_resources(key).completed
    assert calls.count("first") == 2 and calls.count("second") == 2


def test_cleanup_timeout_joins_original_call(config_factory):
    configure_tools(config_factory())
    services = get_tool_services()
    release, entered = threading.Event(), threading.Event()
    calls = []
    class Resource:
        def close(self, *, deadline):
            calls.append(1)
            entered.set()
            release.wait(3)
    services._providers["slow"] = Resource()
    try:
        started = time.monotonic()
        report = shutdown_tool_system(timeout=0.05)
        assert entered.is_set()
        assert not report.completed and any(i.timed_out for i in report.issues)
        assert time.monotonic() - started < 0.5
        release.set()
        assert shutdown_tool_system(timeout=2).completed
        assert calls == [1]
    finally:
        release.set()


@pytest.mark.parametrize("options", [
    {"interaction_mode": "push"},
    {"interaction_mode": "poll", "interaction_port": QueuePort()},
    {"interaction_mode": "invalid"},
    {"recovery_policy": "owned"},
])
def test_gateway_rejects_invalid_configuration_before_creating_agent(config_factory, options):
    with pytest.raises((ValueError, TypeError)):
        GatewayRuntime(config_factory(), **options)


def test_custom_approver_can_request_human_without_blocking_operation_pool(config_factory, final_response):
    class Approver:
        def approve(self, invocation):
            return HumanApprovalRequired()
    config = config_factory()
    configure_tools(config, approver=Approver())
    class Args(BaseModel):
        pass
    entry = ToolEntry(
        tool_id="test.action", implementation_version="1", name="action", description="action",
        args_model=Args, handler=lambda args, context: {"ok": True}, requires_approval=True,
    )
    get_tool_registry().replace_all((entry,))
    response = NormalizedModelResponse(tool_calls=[ToolCall(id="a", name="action", arguments="{}")], finish_reason="tool_calls")
    with gateway(config, response, final_response, interaction_mode="poll") as host:
        submission = host.submit_turn("act", "approval")
        request = host.next_interaction(timeout=2)
        assert request.kind == "approval"
        assert host.agent._operation_pool.submit(lambda: "free").result(1) == "free"
        assert not host.resolve_interaction("approval", request.request_id, {"decision": "always"})
        assert not submission.future.done()
        assert host.resolve_interaction("approval", request.request_id, {"decision": "session"})
        assert submission.future.result(3).status == "completed"
        assert get_tool_services().grants.allowed(host.agent.session_resource_key("approval"), "action")


def test_idle_reaping_preserves_session_grants(config_factory, context_factory):
    from test_tool_execution_resources import StatefulBrowser, visit
    configure_tools(config_factory())
    services = get_tool_services()
    browser = StatefulBrowser()
    services._provider_factories["browser"] = lambda: browser
    with context_factory() as context:
        key = context.resource_key
        assert visit(context) == 1
        services.grants.grant(key, "write", "session")
        assert services.reap_idle(now=time.monotonic() + 2000).completed
        assert browser.closed == [key]
        # Activity means backend operations, not the lifetime of an entire turn.
        assert visit(context) == 1
        assert services.grants.allowed(key, "write")


def test_invalid_workspace_does_not_create_session(config_factory, tmp_path):
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path),
    })
    with gateway(config) as host:
        request = replace(SessionBuildRequest.from_defaults(config.session_defaults), workspace=tmp_path.parent)
        result = host.run_turn_blocking("go", "invalid", session_request=request)
        assert result.error.code == "invalid_workspace"
        assert host.agent._store.load_session("invalid") is None


def test_attached_recovery_does_not_touch_other_session_and_can_ask(config_factory, final_response):
    import sqlite3
    from agentcore import Message
    config = config_factory()
    store = SQLiteStore(config.database_path)
    store.open()
    for sid in ("mine", "other"):
        lease = store.acquire_lease(session_id=sid, owner_id="dead", turn_id=sid, ttl_seconds=60)
        store.create_session(
            sid,
            system_prompt_bytes=b"system", user_message=Message.user("unfinished"), fence=lease.fence,
        )
    store.close()
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("UPDATE session_leases SET expires_at=?", (time.time() - 1,))
    with gateway(config, clarify_response(), final_response, interaction_mode="poll") as host:
        assert not host.agent._store.get_session_recovery("mine")["resume_pending"]
        submission = host.attach_session("mine")
        assert submission is not None
        assert host.attach_session("mine") is submission
        request = host.next_interaction(timeout=2)
        assert request.session_id == "mine"
        assert not host.agent._store.get_session_recovery("other")["resume_pending"]
        with sqlite3.connect(config.database_path) as connection:
            owner = connection.execute("SELECT owner_id FROM session_leases WHERE session_id='other'").fetchone()[0]
        assert owner == "dead"
        assert host.resolve_interaction("mine", request.request_id, {"answers": {"q0": "yes"}})
        assert submission.future.result(3).status == "completed"
        messages = host.agent._store.load_messages("mine")
        assert len([m for m in messages if m.role == "user" and m.kind == "normal"]) == 1
        assert host.agent._store.list_pending_deliveries() == []


def test_human_wait_pauses_run_budget_but_keeps_lease_alive(config_factory, final_response):
    from agentcore import LeasePolicy, TimeoutPolicy
    config = config_factory(timeouts=TimeoutPolicy(run_budget_seconds=0.5)).model_copy(update={
        "lease": LeasePolicy(ttl_seconds=0.3, renew_interval_seconds=0.05, wait_timeout_seconds=1),
    })
    with gateway(config, clarify_response(), final_response, interaction_mode="poll") as host:
        submission = host.submit_turn("ask", "wait")
        request = host.next_interaction(timeout=2)
        assert request is not None
        # Deliberately exceed both the run budget and lease TTL during a human wait.
        threading.Event().wait(0.7)
        assert host.resolve_interaction("wait", request.request_id, {"answers": {"q0": "ok"}})
        assert submission.future.result(3).status == "completed"


def test_provider_timeout_error_during_cleanup_can_retry(config_factory):
    configure_tools(config_factory())
    calls = []
    class Resource:
        def close(self, *, deadline):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("remote close timeout")
    get_tool_services()._providers["fake"] = Resource()
    assert not shutdown_tool_system(timeout=1).completed
    assert shutdown_tool_system(timeout=1).completed
    assert len(calls) == 2


def test_approval_requires_all_scopes_before_handler(config_factory, final_response):
    from tools.registry import ToolExecutionPolicy
    config = config_factory()
    configure_tools(config)
    calls = []
    class Args(BaseModel):
        pass
    get_tool_registry().replace_all((ToolEntry(
        tool_id="test.multi", implementation_version="1", name="multi", description="multi",
        args_model=Args, handler=lambda args, context: calls.append(1),
        policy_resolver=lambda args: ToolExecutionPolicy(approval_scopes=("first", "second")),
    ),))
    response = NormalizedModelResponse(tool_calls=[ToolCall(id="m", name="multi", arguments="{}")], finish_reason="tool_calls")
    with gateway(config, response, final_response, interaction_mode="poll") as host:
        submission = host.submit_turn("go", "multi")
        first = host.next_interaction(timeout=2)
        assert first.payload.scope == "first" and calls == []
        host.resolve_interaction("multi", first.request_id, {"decision": "session"})
        second = host.next_interaction(timeout=2)
        assert second.payload.scope == "second" and calls == []
        host.resolve_interaction("multi", second.request_id, {"decision": "deny"})
        assert submission.future.result(3).tool_outcomes[0].state == "denied"
        assert calls == []
        assert not get_tool_services().grants.allowed(host.agent.session_resource_key("multi"), "first")


def test_cli_eof_cancels_pending_turn_and_new_waits_for_reset(config_factory, monkeypatch):
    from agentcore.cli import _chat_loop
    from tools.lifecycle import CleanupIssue, CleanupReport

    config = config_factory()
    host = gateway(config, clarify_response(), interaction_mode="poll")
    with host:
        responses = iter(["ask"])
        def input_until_eof(prompt):
            try:
                return next(responses)
            except StopIteration:
                raise EOFError()
        monkeypatch.setattr("builtins.input", input_until_eof)
        assert _chat_loop(host, "cli-eof") == 0
        assert host.pending_interactions("cli-eof") == ()
        assert not host._closed
        assert host.agent.stop_turns(2).completed

    class Host:
        def __init__(self):
            self.attached, self.detached = [], []
        def attach_session(self, sid):
            self.attached.append(sid)
            return None
        def reset_session_resources(self, sid):
            return CleanupReport((CleanupIssue("session", sid, "close", "busy"),))
        def detach_session(self, sid):
            self.detached.append(sid)

    borrowed = Host()
    responses = iter(["/new", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    assert _chat_loop(borrowed, "old") == 0
    assert borrowed.attached == ["old"] and borrowed.detached == []

def test_tools_doctor_and_logs_do_not_start_agent(tmp_path, monkeypatch):
    from agentcore.cli import run_cli
    from test_config_and_cli import _write_config
    config = tmp_path / "agentcore.toml"
    _write_config(config)
    monkeypatch.setattr("gateway.gateway.GatewayRuntime", lambda *a, **kw: pytest.fail("doctor constructed Gateway"))
    assert run_cli(["tools", "doctor", "--config", str(config)]) == 0
    assert run_cli(["logs", "list", "--config", str(config)]) == 0
    assert runtime._state is runtime.ToolSystemState.UNINITIALIZED


def test_relative_workspace_config_uses_toml_directory(tmp_path):
    from agentcore import load_config
    from test_config_and_cli import _write_config
    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    (tmp_path / "work").mkdir()
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write('\n[workspace]\ndefault_cwd = "work"\nallowed_root = "."\n')
    config = load_config(config_path)
    assert config.workspace.default_cwd == tmp_path / "work"
    assert config.workspace.allowed_root == tmp_path


def test_computer_probe_cache_has_ttl_refresh_and_environment_identity(monkeypatch):
    from tools.computer_backend import CuaCliBackend
    probes = []
    def probe(cls):
        probes.append(1)
        return True
    monkeypatch.setattr(CuaCliBackend, "_probe_available", classmethod(probe))
    monkeypatch.setattr("tools.computer_backend.shutil.which", lambda name: "fake-driver")
    assert CuaCliBackend.available() and CuaCliBackend.available()
    assert len(probes) == 1
    assert CuaCliBackend.available(refresh=True)
    assert len(probes) == 2
    monkeypatch.setenv("XDG_RUNTIME_DIR", "new-probe-environment")
    assert CuaCliBackend.available()
    assert len(probes) == 3
    key, _, value = CuaCliBackend._availability_cache
    CuaCliBackend._availability_cache = (key, time.monotonic() - 60, value)
    assert CuaCliBackend.available(ttl=1)
    assert len(probes) == 4
