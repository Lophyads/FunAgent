from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

pytest.importorskip("mcp")
pytest.importorskip("jsonschema")
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from agentcore import HostCapabilities, NormalizedModelResponse, ToolCall, TurnHostContext
from agentcore.interfaces import ApprovalDecision, GuardDecision
from conftest import ScriptedTransport
from test_interactive_tools import QueuePort
from test_mcp_client import events, stdio_config
from tools import runtime
from tools.config import McpServerConfig, ToolServiceConfig, ToolSystemConfig
from tools.mcp_adapter import McpToolError, build_mcp_entries, normalize_mcp_result
from tools.model_tools import load_tools
from tools.registry import ToolRegistry
from tools.runtime import get_tool_registry, initialize_tool_system, shutdown_tool_system
from tools.toolsets import DefaultToolsetResolver, ToolsetRequest


@pytest.mark.parametrize("values", [
    {}, {"command": "python", "url": "https://example.com/mcp"},
    {"command": " "}, {"url": "file:///tmp/server"},
    {"url": "https://example.com", "args": ["bad"]},
    {"url": "https://example.com", "env": {"BAD": "1"}},
    {"command": "python", "headers": {"BAD": "1"}},
    {"command": "python", "tool_timeout_seconds": float("inf")},
    {"command": "python", "connect_timeout_seconds": 0},
])
def test_invalid_server_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        McpServerConfig(**values)


def test_schema_semantics_and_raw_arguments_are_preserved():
    schema = {"type": "object", "$defs": {"item": {
        "type": "object", "properties": {"count": {"type": "integer"}},
        "required": ["count"], "additionalProperties": False,
    }}, "properties": {
        "kind": {"enum": ["a", "b"]}, "item": {"$ref": "#/$defs/item"},
        "nullable": {"type": ["string", "null"]},
        "defaulted": {"type": "integer", "default": 7},
    }, "required": ["kind", "item"], "additionalProperties": True}
    remote = Tool(name="schema", inputSchema=copy.deepcopy(schema))
    calls = []
    client = SimpleNamespace(is_available=lambda _: True, call_tool=lambda *a, **kw: calls.append(a) or CallToolResult(content=[]))
    config = McpServerConfig(command="unused", requires_approval=False)
    entry, = build_mcp_entries(client, {"s": config}, {"s": (remote,)})
    valid = {"kind": "a", "item": {"count": 2}, "nullable": None,
             "model_dump": "raw", "_schema": "raw", "model_config": {"raw": True}}
    args = entry.args_model.model_validate(valid)
    assert args.model_dump(mode="json") == valid
    assert entry.definition()["function"]["parameters"] == schema
    exported = entry.args_model.model_json_schema()
    exported["properties"].clear()
    remote.input_schema.clear()
    assert entry.args_model.model_json_schema() == schema
    for invalid in ({"kind": "other", "item": {"count": 2}},
                    {"kind": "a", "item": {"count": "2"}},
                    {"kind": "a", "item": {"count": 2, "extra": 1}}):
        with pytest.raises(ValidationError):
            entry.args_model.model_validate(invalid)
    entry.handler(args, SimpleNamespace(check_active=lambda: None))
    assert calls[0][:2] == ("s", "schema") and calls[0][2] == valid


def test_filtering_names_collisions_and_bad_schemas():
    client = SimpleNamespace(is_available=lambda _: True)
    configs = {"one": McpServerConfig(command="unused", include=["get*"], exclude=["get_secret"]),
               "two": McpServerConfig(command="unused")}
    tool = lambda name, schema=None: Tool(name=name, inputSchema=schema or {"type": "object"})
    discovered = {"one": (tool("get"), tool("get_secret"), tool("ignored")), "two": (
        tool("get"), tool("a b"), tool("a?b"), tool("x" * 100),
        tool("external", {"$ref": "https://example.com/schema"}),
        tool("broken", {"type": "not-a-type"}),
    )}
    entries = build_mcp_entries(client, configs, discovered)
    assert {e.name for e in entries if "get" in e.name} == {"mcp__one__get", "mcp__two__get"}
    assert len(entries) == 3 and all(len(e.name) <= 64 for e in entries)
    assert [e.name for e in entries] == [e.name for e in build_mcp_entries(client, configs, discovered)]
    assert not build_mcp_entries(client, {"one": configs["one"]}, {"one": (tool("get"),)}, existing_entries=entries)
    registry = ToolRegistry()
    registry.replace_all(entries)


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"value": {"$dynamicRef": "https://example.com/schema"}}},
    {"type": "object", "$defs": {"unused": {"$ref": "file:///tmp/schema"}}},
    {"type": "object", "properties": {"value": {"$ref": "#/$defs/missing"}}},
])
def test_external_references_are_not_fetched(schema):
    client = SimpleNamespace(is_available=lambda _: True)
    config = McpServerConfig(command="unused")
    assert not build_mcp_entries(client, {"s": config}, {"s": (Tool(name="bad", inputSchema=schema),)})


def test_reference_shaped_instance_data_does_not_get_rejected():
    schema = {"type": "object", "default": {"$ref": "https://example.com/instance-data"}}
    config = McpServerConfig(command="unused")
    entries = build_mcp_entries(SimpleNamespace(is_available=lambda _: True), {"s": config},
                                {"s": (Tool(name="valid", inputSchema=schema),)})
    assert len(entries) == 1


def test_result_conversion_and_business_error():
    image = ImageContent(type="image", data="do-not-render-base64", mimeType="image/png")
    assert normalize_mcp_result(CallToolResult(content=[], structuredContent={})) == {}
    result = normalize_mcp_result(CallToolResult(content=[image], structuredContent={"x": 1}))
    assert result == {"result": {"x": 1}, "unsupported_content": ["image"]}
    assert "do-not-render" not in json.dumps(result)
    assert normalize_mcp_result(CallToolResult(content=[TextContent(type="text", text="text")], structuredContent={"duplicate": 1})) == "text"
    with pytest.raises(McpToolError, match="failed"):
        normalize_mcp_result(CallToolResult(content=[TextContent(type="text", text="failed")], isError=True))


def _mcp_agent_config(config_factory, tmp_path, *, approval=False):
    config = config_factory()
    return config.model_copy(update={
        "tools": ToolServiceConfig(mcp_servers={"local": stdio_config(tmp_path, requires_approval=approval)}),
        "session_defaults": config.session_defaults.model_copy(update={"enabled_toolsets": ("mcp",)}),
    })


def test_agent_validates_calls_and_records_mcp_failures(config_factory, agent_factory, tmp_path, final_response):
    config = _mcp_agent_config(config_factory, tmp_path)
    response = NormalizedModelResponse(tool_calls=[
        ToolCall(id="ok", name="mcp__local__echo", arguments='{"text":"hello"}'),
        ToolCall(id="invalid", name="mcp__local__echo", arguments='{"text":123}'),
        ToolCall(id="error", name="mcp__local__error", arguments='{}'),
    ], finish_reason="tool_calls")
    transport = ScriptedTransport(response, final_response)
    with agent_factory(config, transports={"primary": transport}) as agent:
        result = agent.run_turn_blocking("go", "mcp-session")
        assert result.status == "completed"
        assert [o.state for o in result.tool_outcomes] == ["succeeded", "failed", "failed"]
        assert [o.error_type for o in result.tool_outcomes] == [None, "invalid_arguments", "tool_execution_error"]
        messages = agent._store.load_messages("mcp-session")
        assert "hello" in next(m.content for m in messages if m.role == "tool")
        assert any(t["function"]["name"] == "mcp__local__echo" for t in transport.requests[0].body["tools"])
    assert [e["name"] for e in events(tmp_path) if e["event"] == "call"] == ["echo", "error"]


def test_missing_approval_capability_hides_tools(availability):
    config = McpServerConfig(command="unused")
    entries = build_mcp_entries(SimpleNamespace(is_available=lambda _: True), {"s": config},
                                {"s": (Tool(name="echo", inputSchema={"type": "object"}),)})
    registry = ToolRegistry()
    registry.replace_all(entries)
    selected, _ = load_tools(ToolsetRequest(("mcp",)), registry, availability=availability)
    assert selected == {}
    selected, _ = load_tools(ToolsetRequest(("mcp",)), registry,
                            availability=replace(availability, capabilities=HostCapabilities(approval=True)))
    assert tuple(selected) == ("mcp__s__echo",)
    assert DefaultToolsetResolver().resolve(ToolsetRequest(("mcp",)), ()) == ()


@pytest.mark.parametrize("denial", ["guard", "approval"])
def test_guard_and_approval_denial_do_not_call_server(config_factory, agent_factory, tmp_path, final_response, denial):
    class DenyGuard:
        def check(self, invocation):
            return GuardDecision(allowed=False, reason="denied")
    class DenyApprover:
        def approve(self, invocation):
            return ApprovalDecision(approved=False, reason="denied")
    config = _mcp_agent_config(config_factory, tmp_path, approval=True)
    response = NormalizedModelResponse(tool_calls=[ToolCall(id="deny", name="mcp__local__echo", arguments='{}')], finish_reason="tool_calls")
    options = {"guard": DenyGuard()} if denial == "guard" else {"approver": DenyApprover()}
    with agent_factory(config, transports={"primary": ScriptedTransport(response, final_response)}, **options) as agent:
        result = agent.run_turn_blocking("go", "denied", host_context=TurnHostContext(HostCapabilities(approval=True), QueuePort()))
        assert result.tool_outcomes[0].state == "denied"
    assert not [e for e in events(tmp_path) if e["event"] == "call"]


def test_initialization_failure_rolls_back_and_can_be_retried(tmp_path):
    broken = McpServerConfig(command="funagent-nonexistent-command", required=True)
    config = ToolSystemConfig(tools=ToolServiceConfig(mcp_servers={"broken": broken, "local": stdio_config(tmp_path)}))
    with pytest.raises(RuntimeError, match="required MCP"):
        initialize_tool_system(config)
    assert runtime._state is runtime.ToolSystemState.UNINITIALIZED
    assert [e["event"] for e in events(tmp_path)].count("closed") == 1
    initialize_tool_system(ToolSystemConfig())
    assert get_tool_registry().entries()


def test_unfinished_init_cleanup_is_retained(monkeypatch):
    from tools.lifecycle import CleanupIssue, CleanupReport
    from tools.services import ToolServices
    from tools.runtime import ToolSystemNotInitialized
    original_close = ToolServices.close
    monkeypatch.setattr(ToolServices, "mcp", lambda self: (_ for _ in ()).throw(RuntimeError("init failed")))
    monkeypatch.setattr(ToolServices, "close", lambda self, **kw: CleanupReport((CleanupIssue("test", "process", "close", "pending", True),)))
    config = ToolSystemConfig(tools=ToolServiceConfig(mcp_servers={"s": McpServerConfig(command="unused")}))
    with pytest.raises(RuntimeError, match="init failed"):
        initialize_tool_system(config)
    assert runtime._state is runtime.ToolSystemState.CLOSING and runtime._services is not None
    with pytest.raises(ToolSystemNotInitialized):
        initialize_tool_system(config)
    monkeypatch.setattr(ToolServices, "close", original_close)
    assert shutdown_tool_system(timeout=3).completed


def test_without_mcp_extra_no_sdk_is_imported():
    code = "from tools.runtime import initialize_tool_system,shutdown_tool_system; from tools.config import ToolSystemConfig; import sys; initialize_tool_system(ToolSystemConfig()); assert 'mcp' not in sys.modules; assert shutdown_tool_system(timeout=3).completed"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=15)
