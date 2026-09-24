from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import BaseModel

from agentcore import (
    ApprovalDecision,
    MemoryItem,
    NormalizedModelResponse,
    ToolCall,
    ToolRegistry,
    ToolContext,
    ToolEntry,
)

from conftest import ScriptedTransport


class EchoArgs(BaseModel):
    text: str


class ApprovalArgs(BaseModel):
    value: str


def _registry_with(*entries: ToolEntry) -> ToolRegistry:
    registry = ToolRegistry()
    for entry in entries:
        registry.register(entry)
    return registry


def _tool_entry(
    name: str, args_model: type[BaseModel], handler, **kwargs,
) -> ToolEntry:
    return ToolEntry(
        tool_id=f"test.{name}", implementation_version="1", name=name,
        description=f"Test tool {name}.", args_model=args_model,
        handler=handler, **kwargs,
    )


@pytest.mark.asyncio
async def test_final_text_persists_user_before_provider(
    config_factory, agent_factory, final_response,
):
    config = config_factory()

    def assert_user_is_durable(request):
        with sqlite3.connect(config.database_path) as connection:
            row = connection.execute(
                "SELECT role, content FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row == ("user", "hello")
        return final_response

    transport = ScriptedTransport(assert_user_is_durable)
    async with agent_factory(config, transports={"primary": transport}) as agent:
        result = await agent.run_turn("hello", session_id="s1")

    with sqlite3.connect(config.database_path) as connection:
        roles = [row[0] for row in connection.execute(
            "SELECT role FROM messages WHERE session_id = 's1' ORDER BY id"
        )]
    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.usage["total_tokens"] == 5
    assert roles == ["user", "assistant"]
    assert transport.requests[0].body["messages"][0] == {
        "role": "system", "content": "test system",
    }


@pytest.mark.asyncio
async def test_tool_call_is_persisted_before_handler_and_replayed(
    config_factory, agent_factory,
):
    config = config_factory()
    handler_observations = []

    def echo(args: EchoArgs, context: ToolContext):
        with sqlite3.connect(config.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'assistant' "
                "AND tool_calls_json IS NOT NULL", (context.session_id,)
            ).fetchone()[0]
        handler_observations.append(count)
        return {"echo": args.text}

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="call-1", name="echo", arguments='{"text":"hi"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="finished", finish_reason="stop"),
    )
    async with agent_factory(
        config, tool_registry=_registry_with(_tool_entry("echo", EchoArgs, echo)),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("use echo", session_id="tools")

    assert result.status == "completed"
    assert handler_observations == [1]
    messages = transport.requests[1].body["messages"]
    assert [message["role"] for message in messages] == [
        "system", "user", "assistant", "tool",
    ]
    assert json.loads(messages[-1]["content"]) == {"ok": True, "result": {"echo": "hi"}}


@pytest.mark.asyncio
async def test_current_time_supports_iana_timezone_on_windows(config_factory, agent_factory):
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="tz1", name="current_time", arguments='{"timezone":"Asia/Shanghai"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="time returned", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(), transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("time", session_id="timezone")

    payload = json.loads(transport.requests[1].body["messages"][-1]["content"])
    assert result.status == "completed"
    assert payload["ok"] is True
    assert payload["result"]["timezone"] == "Asia/Shanghai"
    assert payload["result"]["iso8601"].endswith("+08:00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "expected_type"),
    [
        (ToolCall(id="u1", name="missing", arguments="{}"), "unknown_tool"),
        (ToolCall(id="u2", name="calculator", arguments="not-json"), "invalid_arguments"),
        (ToolCall(id="u3", name="calculator", arguments='{"expression":"name + 1"}'), "tool_execution_error"),
    ],
)
async def test_tool_failures_become_tool_results(
    config_factory, agent_factory, call, expected_type,
):
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[call], finish_reason="tool_calls"),
        NormalizedModelResponse(content="recovered", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(), transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("test", session_id=f"errors-{call.id}")

    payload = json.loads(transport.requests[1].body["messages"][-1]["content"])
    assert result.status == "completed"
    assert payload["ok"] is False
    assert payload["error"]["type"] == expected_type


@pytest.mark.asyncio
async def test_approval_required_defaults_to_deny(config_factory, agent_factory):
    called = False

    def sensitive(args: ApprovalArgs, context: ToolContext):
        nonlocal called
        called = True
        return args.value

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="a1", name="sensitive", arguments='{"value":"x"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="denied safely", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(),
        tool_registry=_registry_with(_tool_entry(
            "sensitive", ApprovalArgs, sensitive, requires_approval=True,
        )),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("sensitive", session_id="approval")

    assert result.status == "completed"
    assert result.tool_outcomes[0].state == "denied"
    assert result.tool_outcomes[0].effect_disposition == "not_applied"
    assert called is False
    assert json.loads(transport.requests[1].body["messages"][-1]["content"])["error"]["type"] == "approval_required"


@pytest.mark.asyncio
async def test_approved_tool_executes(config_factory, agent_factory):
    class Approver:
        def approve(self, invocation):
            return ApprovalDecision(approved=True)

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="a2", name="sensitive", arguments='{"value":"ok"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="approved", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(),
        tool_registry=_registry_with(_tool_entry(
            "sensitive", ApprovalArgs,
            lambda args, context: {"value": args.value}, requires_approval=True,
        )),
        transports={"primary": transport}, approver=Approver(),
    ) as agent:
        result = await agent.run_turn("sensitive", session_id="approval-ok")

    assert result.status == "completed"
    assert json.loads(transport.requests[1].body["messages"][-1]["content"])["ok"] is True


@pytest.mark.asyncio
async def test_tool_result_is_bounded(config_factory, agent_factory):
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="big", name="large", arguments='{"value":"x"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="done", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(tool_result_max_chars=256),
        tool_registry=_registry_with(_tool_entry(
            "large", ApprovalArgs, lambda args, context: "x" * 10_000,
        )),
        transports={"primary": transport},
    ) as agent:
        await agent.run_turn("large", session_id="large")

    content = transport.requests[1].body["messages"][-1]["content"]
    assert len(content) <= 256
    assert json.loads(content)["truncated"] is True


@pytest.mark.asyncio
async def test_memory_and_review_are_isolated(config_factory, agent_factory, final_response):
    events = []

    class Memory:
        def recall(self, user_input, session_id):
            events.append("memory")
            return [MemoryItem(id="m1", content="remember this")]

    class Review:
        def schedule(self, snapshot):
            events.append("review")
            raise RuntimeError("queue down")

    transport = ScriptedTransport(final_response)
    async with agent_factory(
        config_factory(), transports={"primary": transport}, memory=Memory(),
        review_scheduler=Review(),
    ) as agent:
        result = await agent.run_turn("raw text", session_id="hooks")

    assert result.status == "completed"
    assert events == ["memory", "review"]
    assert "remember this" in transport.requests[0].body["messages"][0]["content"]
    assert any("review scheduler failed" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_multiple_tools_execute_sequentially(config_factory, agent_factory):
    execution_order = []

    def echo(args: EchoArgs, context: ToolContext):
        execution_order.append(args.text)
        return args.text

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="m1", name="echo", arguments='{"text":"one"}'),
            ToolCall(id="m2", name="echo", arguments='{"text":"two"}'),
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="both done", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(), tool_registry=_registry_with(_tool_entry("echo", EchoArgs, echo)),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("two tools", session_id="multi")

    assert result.status == "completed"
    assert execution_order == ["one", "two"]
    assert [message["role"] for message in transport.requests[1].body["messages"]][-3:] == [
        "assistant", "tool", "tool",
    ]


@pytest.mark.asyncio
async def test_guard_failure_is_fail_closed(config_factory, agent_factory):
    called = False

    def echo(args: EchoArgs, context: ToolContext):
        nonlocal called
        called = True
        return args.text

    class BrokenGuard:
        def check(self, invocation):
            raise RuntimeError("guard unavailable")

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="g1", name="echo", arguments='{"text":"blocked"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(content="safe", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(), tool_registry=_registry_with(_tool_entry("echo", EchoArgs, echo)),
        transports={"primary": transport}, guard=BrokenGuard(),
    ) as agent:
        result = await agent.run_turn("guard", session_id="guard")

    assert result.status == "completed"
    assert result.tool_outcomes[0].state == "denied"
    assert called is False
    assert json.loads(transport.requests[1].body["messages"][-1]["content"])["error"]["type"] == "guard_error"


@pytest.mark.asyncio
async def test_max_iterations_returns_failed_result(config_factory, agent_factory):
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="c1", name="calculator", arguments='{"expression":"1+1"}')
        ], finish_reason="tool_calls"),
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="c2", name="calculator", arguments='{"expression":"2+2"}')
        ], finish_reason="tool_calls"),
    )
    config = config_factory(max_iterations=2)
    async with agent_factory(
        config, transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("loop", session_id="limit")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("limit",),
        ).fetchone()

    assert result.status == "failed"
    assert result.error.code == "max_iterations"
    assert result.iterations == 2
    assert lease == (None,)
