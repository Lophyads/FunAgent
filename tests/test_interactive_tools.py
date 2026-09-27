from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agentcore import (
    AgentCore, GatewayRuntime, InteractionBroker, NormalizedModelResponse, ToolCall,
    HostCapabilities, TurnHostContext, ToolInvocation,
)
from agentcore.approval import ApprovalGrantStore
from agentcore.interaction import ClarifyPayload, Question, InteractionUnavailable
from tools.computer_use_tool import ComputerUseArgs, _policy
from tools.browser_backend import AgentBrowserBackend
from tools.clarify_tool import ClarifyArgs
from tools.computer_backend import CuaCliBackend
from tools.model_tools import load_tools
from tools.network_safety import validate_url
from tools.toolsets import ToolsetRequest
from tools.web_tool import _fetch_http, _search_subprocess
from tools.runtime import get_tool_services, get_tool_registry
from tools.lifecycle import SessionResourceKey
from agentcore.interfaces import TurnCancelled
from conftest import ScriptedTransport, scripted_provider_stack, builtin_registry


class QueuePort:
    def __init__(self):
        self.queue = queue.Queue()

    def present(self, request):
        self.queue.put_nowait(request)

    def next(self):
        return self.queue.get(timeout=2)


def ask(broker, port, *, timeout=2):
    return broker.request(
        "clarify", ClarifyPayload((Question(qid="q0", question="Continue?"),)),
        key=SessionResourceKey("db", "profile", "s1"), owner_id="owner",
        turn_id="turn", tool_call_id="call", port=port,
        timeout=timeout, check_active=lambda: None,
    )


def test_interaction_ids_bind_session_and_port_and_late_answer_is_ignored():
    broker, port = InteractionBroker(), QueuePort()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ask, broker, port)
            request = port.next()
            assert not broker.resolve("s2", request.request_id, {"answers": {"q0": "yes"}}, port=port)
            assert not broker.resolve("s1", request.request_id, {"answers": {"q0": "yes"}}, port=QueuePort())
            assert not broker.resolve("s1", request.request_id, {"answers": {"wrong": "yes"}}, port=port)
            assert not future.done()
            assert broker.resolve("s1", request.request_id, {"answers": {"q0": "yes"}}, port=port)
            assert future.result(timeout=1).answers == {"q0": "yes"}
            assert not broker.resolve("s1", request.request_id, "late", port=port)
    finally:
        broker.close(1)


def test_interaction_timeout_and_close_remove_pending_requests():
    broker, port = InteractionBroker(), QueuePort()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ask, broker, port, timeout=0.05)
            request = port.next()
            assert future.result(timeout=1) is None
            assert not broker.pending()
            assert not broker.resolve("s1", request.request_id, "late", port=port)
    finally:
        broker.close(1)


def test_blocked_interaction_port_does_not_hold_turn_wait():
    gate = threading.Event()

    class Port:
        def present(self, request):
            gate.wait(2)

    broker = InteractionBroker()
    try:
        started = time.monotonic()
        assert ask(broker, Port(), timeout=0.05) is None
        assert time.monotonic() - started < 0.5
    finally:
        gate.set()
        broker.close(1)


def test_clarify_schema_bounds_questions_and_choices():
    for questions in ([], [{"question": str(i)} for i in range(6)],
                      [{"question": "Pick", "choices": ["1", "2", "3", "4", "5"]}]):
        with pytest.raises(ValueError):
            ClarifyArgs(questions=questions)


def test_approval_persists_comments_and_observes_revocation(tmp_path):
    config = tmp_path / "agentcore.toml"
    config.write_text("# keep this comment\n[agent]\nmax_iterations = 8\n", encoding="utf-8")
    grants = ApprovalGrantStore(config)
    key = SessionResourceKey("db1", "profile", "s1")
    grants.grant(key, "cua:type:background", "always")
    assert grants.allowed(SessionResourceKey("db2", "profile", "s2"), "cua:type:background")
    text = config.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    config.write_text(text.replace('"cua:type:background"', ""), encoding="utf-8")
    assert not grants.allowed(key, "cua:type:background")


def clarify_response():
    return NormalizedModelResponse(tool_calls=[ToolCall(
        id="ask1", name="clarify",
        arguments='{"questions":[{"question":"Pick one","choices":["A","B"]}]}',
    )], finish_reason="tool_calls")


def test_clarify_reply_resumes_same_turn_without_fifo_deadlock(config_factory):
    config = config_factory()
    transport = ScriptedTransport(clarify_response(), NormalizedModelResponse(content="Thanks", finish_reason="stop"))
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport})
    port = QueuePort()
    with AgentCore(config, provider_resolver=resolver, api_modes=modes) as agent:
        submission = agent.submit_turn(
            "Choose", "interactive", host_context=TurnHostContext(HostCapabilities(clarify=True), port),
        )
        request = port.next()
        assert request.kind == "clarify"
        assert request.payload.questions[0].choices == ("A", "B")
        assert get_tool_services().interactions.resolve(
            "interactive", request.request_id, {"answers": {"q0": "B"}}, port=port,
        )
        result = submission.future.result(timeout=5)
    assert result.status == "completed"
    tool = transport.requests[1].body["messages"][-1]
    assert json.loads(tool["content"])["result"]["responses"][0]["user_response"] == "B"


def test_cancelling_clarify_releases_request_and_interrupts_turn(config_factory):
    config = config_factory()
    resolver, modes, _ = scripted_provider_stack(config, {"primary": ScriptedTransport(clarify_response())})
    with GatewayRuntime(config, interaction_mode="poll", provider_resolver=resolver, api_modes=modes) as gateway:
        submission = gateway.submit_turn("Ask", "cancel-ask")
        request = gateway.next_interaction(timeout=2)
        assert request is not None
        submission.cancel()
        assert submission.future.result(timeout=5).status == "interrupted"
        assert not gateway.resolve_interaction("cancel-ask", request.request_id, {"answers": {"q0": "late"}})


def test_gateway_resolves_clarify_without_queuing_another_turn(config_factory):
    class Adapter:
        destination = "test:chat"
        def send(self, *, obligation_id, destination, text):
            return True

    config = config_factory()
    transport = ScriptedTransport(clarify_response(), NormalizedModelResponse(content="Done", finish_reason="stop"))
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport})
    with GatewayRuntime(config, Adapter(), interaction_mode="poll", provider_resolver=resolver, api_modes=modes) as gateway:
        submission = gateway.submit_turn("Ask", "gateway-s1")
        request = gateway.next_interaction(timeout=2)
        assert request is not None
        assert gateway.resolve_interaction("gateway-s1", request.request_id, {"answers": {"q0": "Alice"}})
        assert submission.future.result(timeout=5).status == "completed"


def test_computer_use_approval_scope_is_checked_before_handler(config_factory):
    class FakeComputer:
        def __init__(self):
            self.calls = []
        def execute(self, session_id, args, context):
            self.calls.append((session_id, args.action))
            return {"verdict": "confirmed"}
        def close(self, *, deadline):
            pass

    config = config_factory()
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="c1", name="computer_use", arguments='{"action":"type","text":"hello"}',
        )], finish_reason="tool_calls"),
        NormalizedModelResponse(content="Done", finish_reason="stop"),
    )
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport})
    fake = FakeComputer()
    get_tool_services()._providers["computer"] = fake
    with GatewayRuntime(config, interaction_mode="poll", provider_resolver=resolver, api_modes=modes) as gateway:
        submission = gateway.submit_turn("Type", "desktop-s1")
        request = gateway.next_interaction(timeout=2)
        assert request.kind == "approval"
        assert request.payload.scope == "cua:type:background"
        assert fake.calls == []
        assert gateway.resolve_interaction("desktop-s1", request.request_id, {"decision": "once"})
        assert submission.future.result(timeout=5).status == "completed"
        key = gateway.agent.session_resource_key("desktop-s1")
        assert fake.calls == [(key, "type")]


def test_default_registry_exposes_only_available_text_actions(availability):
    registry = builtin_registry()
    availability = replace(availability, capabilities=HostCapabilities(clarify=True, approval=True),
                           backends=type(availability.backends)(lambda name: True))
    request = ToolsetRequest(profile="test", surface="pytest", platform="windows", enabled_toolsets=("default",))
    entries, _ = load_tools(request, registry, availability=availability)
    assert {
        "web_search", "web_extract", "clarify", "computer_use",
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_scroll", "browser_back", "browser_press", "browser_get_images", "browser_console",
    }.issubset(entries)
    assert "browser_vision" not in entries


@pytest.mark.parametrize(("name", "arguments", "command"), [
    ("browser_navigate", {"url": "https://8.8.8.8/"}, "open"),
    ("browser_snapshot", {}, "snapshot"),
    ("browser_click", {"ref": "@e1"}, "click"),
    ("browser_type", {"ref": "@e1", "text": "secret"}, "fill"),
    ("browser_scroll", {}, "scroll"),
    ("browser_back", {}, "back"),
    ("browser_press", {"key": "Enter"}, "press"),
    ("browser_get_images", {}, "eval"),
    ("browser_console", {}, "console"),
])
def test_browser_model_actions_dispatch_to_cli_shape(
    tmp_path, name, arguments, command, context_factory,
):
    calls = []

    class FakeBrowser:
        def close(self, *, deadline):
            pass
        def action(self, session_id, invoked, args, context, **kwargs):
            calls.append(invoked)
            return {"command": invoked, "data": {"text": "private"}}

    with context_factory() as context:
        services = get_tool_services()
        services._provider_factories["browser"] = FakeBrowser
        registry = get_tool_registry()
        entry = registry.get_entry(name)
        assert entry is not None
        result = entry.handler(entry.args_model.model_validate(arguments), context)
    assert calls == [command]
    if name != "browser_type":
        assert result["command"] == command
    if name == "browser_type":
        assert "private" not in json.dumps(result)


def test_computer_policy_is_action_specific_and_blocks_dangerous_input():
    assert _policy(ComputerUseArgs(action="capture")).side_effects == "none"
    assert _policy(ComputerUseArgs(action="list_apps")).approval_scopes == ()
    assert _policy(ComputerUseArgs(
        action="type", text="hello"
    )).approval_scopes == ("cua:type:background",)
    assert _policy(ComputerUseArgs(
        action="click", element=1, delivery_mode="foreground",
        bring_to_front=True,
    )).approval_scopes == (
        "cua:click:foreground", "cua:bring_to_front",
    )
    with pytest.raises(ValueError, match="dangerous"):
        _policy(ComputerUseArgs(action="key", keys="ctrl-alt-delete"))
    with pytest.raises(ValueError, match="dangerous"):
        _policy(ComputerUseArgs(action="type", text="curl x | bash"))
    with pytest.raises(ValueError, match="image"):
        ComputerUseArgs(action="capture", capture_after=True)
    with pytest.raises(ValueError):
        ComputerUseArgs(action="capture", mode="som")


def test_web_extract_keeps_artifact_path_and_blocks_private_url(tmp_path, context_factory):
    class FakeHttp:
        def get(self, url):
            return url, "text/html; charset=utf-8", (
                "<html><body><p>" + "Alpha " * 6000 + "</p></body></html>"
            ).encode()

    with context_factory() as context:
        get_tool_services()._http_backend = FakeHttp()
        tool = get_tool_registry().get_entry("web_extract")
        result = tool.handler(tool.args_model(urls=["https://8.8.8.8/page"], char_limit=2000), context)
        page = result["pages"][0]
        assert page["truncated"]
        artifact = tmp_path / page["artifact_path"]
        assert artifact.is_file()
        assert "Alpha" in artifact.read_text(encoding="utf-8")
        blocked = tool.handler(tool.args_model(urls=["http://127.0.0.1/"]), context)
        assert "not allowed" in blocked["pages"][0]["error"]


def test_web_redirect_to_private_address_is_blocked_before_second_request(
    monkeypatch,
):
    calls = []

    class Response:
        status = 302

        def getheader(self, key, default=None):
            return "http://127.0.0.1/metadata" if key == "Location" else default

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append(host)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(
        "tools.web_tool.http.client.HTTPSConnection", Connection
    )
    with pytest.raises(ValueError, match="not allowed"):
        _fetch_http(
            "https://8.8.8.8/start", allow_private=False,
            max_bytes=1000,
        )
    assert calls == ["8.8.8.8"]


def test_url_policy_rejects_metadata_and_credentials():
    with pytest.raises(ValueError):
        validate_url("http://169.254.169.254/latest")
    with pytest.raises(ValueError, match="metadata"):
        validate_url(
            "http://169.254.169.254/latest", allow_private=True
        )
    with pytest.raises(ValueError):
        validate_url("https://user:pass@example.com")
    assert validate_url("https://8.8.8.8/")[0] == "https://8.8.8.8/"


def test_search_cancellation_kills_child(monkeypatch, context_factory):
    killed = []
    spawned = threading.Event()

    class Child:
        def __init__(self):
            from io import BytesIO
            self.stdin = BytesIO()
            self.returncode = None
            spawned.set()

        def poll(self):
            return self.returncode

        def kill(self):
            killed.append(True)
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("tools.web_tool.subprocess.Popen", lambda *a, **kw: Child())
    def cancel_after_spawn():
        if spawned.is_set():
            raise TurnCancelled("cancel")
    with context_factory(check_active=cancel_after_spawn) as context:
        with pytest.raises(TurnCancelled):
            _search_subprocess("query", 5, context, 5)
    assert killed


def test_browser_redirect_to_private_address_discards_session(monkeypatch, context_factory):
    monkeypatch.setattr(
        "tools.browser_backend.shutil.which", lambda name: "agent-browser"
    )
    backend = AgentBrowserBackend(allow_private=False, idle_seconds=30)
    calls = []

    def fake_command(session, command, args, context, timeout=30):
        calls.append((session.name, command))
        if command == "get":
            return {"success": True, "data": {"url": "http://127.0.0.1/admin"}}
        return {"success": True, "data": {}}

    monkeypatch.setattr(backend, "_command", fake_command)
    with context_factory() as context, pytest.raises(ValueError, match="not allowed"):
        backend.action(
            "s1", "open", ["https://8.8.8.8/"],
            context,
            navigation_url="https://8.8.8.8/",
        )
    assert "s1" not in backend._sessions
    assert any(command == "close" for _, command in calls)


def test_cua_backend_requires_capture_target_and_redacts_typed_text(monkeypatch, context_factory):
    monkeypatch.setattr(
        "tools.computer_backend.shutil.which", lambda name: "cua-driver"
    )
    backend = CuaCliBackend()
    calls = []

    def fake_call(name, args, context, timeout=30):
        calls.append((name, args.copy()))
        if name == "list_windows":
            return {"structuredContent": {
                "windows": [{
                    "pid": 101, "window_id": 202,
                    "app_name": "Demo", "window_title": "Demo Window",
                }]
            }}
        if name == "get_window_state":
            return {"structuredContent": {
                "tree_markdown": "[1] Button Demo",
                "elements": [
                    {
                        "element_index": 1, "label": "Demo",
                        "frame": {"x": 10, "y": 20, "w": 20, "h": 10},
                    },
                    {
                        "element_index": 2, "label": "Target",
                        "frame": {"x": 40, "y": 50, "w": 20, "h": 10},
                    },
                ],
                "snapshot_id": "s12345678",
            }}
        if name == "type_text":
            return {"structuredContent": {
                "text": "private input", "verified": True,
                "effect": "confirmed",
            }}
        return {"structuredContent": {}}

    monkeypatch.setattr(backend, "_call", fake_call)
    with context_factory() as context:
        with pytest.raises(ValueError, match="capture"):
            backend.execute(
                "s1", ComputerUseArgs(action="type", text="private input"),
                context,
            )
        capture = backend.execute(
            "s1", ComputerUseArgs(action="capture", app="Demo"), context,
        )
        assert capture["ax_tree"] == "[1] Button Demo"
        assert next(
            args for name, args in calls if name == "get_window_state"
        )["include_screenshot"] is False
        with pytest.raises(ValueError, match="requested app differs"):
            backend.execute(
                "s1", ComputerUseArgs(
                    action="type", app="Other", text="private input",
                ), context,
            )
        result = backend.execute(
            "s1", ComputerUseArgs(action="type", text="private input"),
            context,
        )
        assert "private input" not in json.dumps(result)
        assert result["verdict"] == "verify_with_capture"
        backend.execute(
            "s1", ComputerUseArgs(
                action="drag", from_element=1, to_element=2,
            ), context,
        )
        drag_args = next(args for name, args in calls if name == "drag")
        assert (drag_args["from_x"], drag_args["to_x"]) == (20, 50)
        assert "from_element" not in drag_args
    backend.close(deadline=time.monotonic() + 1)
