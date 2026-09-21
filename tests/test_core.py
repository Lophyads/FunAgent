from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import BaseModel

from agentcore import (
    AgentCore,
    ApprovalDecision,
    MemoryItem,
    ModelResponse,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    default_tool_registry,
)

from conftest import ScriptedTransport


@pytest.mark.asyncio
async def test_final_text_persists_user_before_provider(config_factory, final_response):
    config = config_factory()

    def assert_user_is_durable(request):
        with sqlite3.connect(config.database_path) as conn:
            row = conn.execute(
                "SELECT role, content FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row == ("user", "hello")
        return final_response

    transport = ScriptedTransport(assert_user_is_durable)
    async with AgentCore(config, default_tool_registry(), transports={"primary": transport}) as agent:
        result = await agent.run_turn("hello", session_id="s1")
        stored = await agent.store.load_messages("s1")
        turn = await agent.store.get_turn(result.turn_id)
        finalized_again = await agent.store.finish_turn(
            session_id="s1",
            turn_id=result.turn_id,
            status="completed",
            error=None,
            route_name="primary",
            iterations=1,
            usage={},
        )

    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.usage["total_tokens"] == 5
    assert [message.role for message in stored] == ["user", "assistant"]
    assert turn["status"] == "completed"
    assert finalized_again is False
    assert transport.requests[0].messages[0] == {"role": "system", "content": "test system"}


class EchoArgs(BaseModel):
    text: str


@pytest.mark.asyncio
async def test_tool_call_is_persisted_before_handler_and_replayed(config_factory):
    config = config_factory()
    registry = ToolRegistry()
    handler_observations = []

    async def echo(args: EchoArgs, context: ToolContext):
        with sqlite3.connect(config.database_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE turn_id = ? AND role = 'assistant' "
                "AND tool_calls_json IS NOT NULL",
                (context.turn_id,),
            ).fetchone()[0]
        handler_observations.append(count)
        return {"echo": args.text}

    registry.register(
        ToolSpec(name="echo", description="Echo text.", args_model=EchoArgs, handler=echo)
    )
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="call-1", name="echo", arguments='{"text":"hi"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="finished", finish_reason="stop"),
    )

    async with AgentCore(config, registry, transports={"primary": transport}) as agent:
        result = await agent.run_turn("use echo", session_id="tools")

    assert result.status == "completed"
    assert handler_observations == [1]
    second_roles = [message["role"] for message in transport.requests[1].messages]
    assert second_roles == ["system", "user", "assistant", "tool"]
    tool_payload = json.loads(transport.requests[1].messages[-1]["content"])
    assert tool_payload == {"ok": True, "result": {"echo": "hi"}}


@pytest.mark.asyncio
async def test_current_time_supports_iana_timezone_on_windows(config_factory):
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="tz1",
                    name="current_time",
                    arguments='{"timezone":"Asia/Shanghai"}',
                )
            ],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="time returned", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(), default_tool_registry(), transports={"primary": transport}
    ) as agent:
        result = await agent.run_turn("time", session_id="timezone")

    assert result.status == "completed"
    payload = json.loads(transport.requests[1].messages[-1]["content"])
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
async def test_tool_failures_become_tool_results(config_factory, call, expected_type):
    transport = ScriptedTransport(
        ModelResponse(tool_calls=[call], finish_reason="tool_calls"),
        ModelResponse(content="recovered", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(), default_tool_registry(), transports={"primary": transport}
    ) as agent:
        result = await agent.run_turn("test", session_id=f"errors-{call.id}")

    assert result.status == "completed"
    payload = json.loads(transport.requests[1].messages[-1]["content"])
    assert payload["ok"] is False
    assert payload["error"]["type"] == expected_type


class ApprovalArgs(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_approval_required_defaults_to_deny(config_factory):
    called = False

    async def sensitive(args: ApprovalArgs, context: ToolContext):
        nonlocal called
        called = True
        return args.value

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="sensitive",
            description="A sensitive operation.",
            args_model=ApprovalArgs,
            handler=sensitive,
            requires_approval=True,
        )
    )
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="a1", name="sensitive", arguments='{"value":"x"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="denied safely", finish_reason="stop"),
    )
    async with AgentCore(config_factory(), registry, transports={"primary": transport}) as agent:
        result = await agent.run_turn("sensitive", session_id="approval")

    assert result.status == "completed"
    assert called is False
    payload = json.loads(transport.requests[1].messages[-1]["content"])
    assert payload["error"]["type"] == "approval_required"


@pytest.mark.asyncio
async def test_approved_tool_executes(config_factory):
    class Approver:
        async def approve(self, invocation):
            return ApprovalDecision(approved=True)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="sensitive",
            description="A sensitive operation.",
            args_model=ApprovalArgs,
            handler=lambda args, context: {"value": args.value},
            requires_approval=True,
        )
    )
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="a2", name="sensitive", arguments='{"value":"ok"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="approved", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(), registry, approver=Approver(), transports={"primary": transport}
    ) as agent:
        result = await agent.run_turn("sensitive", session_id="approval-ok")

    assert result.status == "completed"
    assert json.loads(transport.requests[1].messages[-1]["content"])["ok"] is True


@pytest.mark.asyncio
async def test_tool_result_is_bounded(config_factory):
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="large",
            description="Return a large value.",
            args_model=ApprovalArgs,
            handler=lambda args, context: "x" * 10_000,
        )
    )
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="big", name="large", arguments='{"value":"x"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="done", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(tool_result_max_chars=256),
        registry,
        transports={"primary": transport},
    ) as agent:
        await agent.run_turn("large", session_id="large")

    content = transport.requests[1].messages[-1]["content"]
    assert len(content) <= 256
    assert json.loads(content)["truncated"] is True


@pytest.mark.asyncio
async def test_memory_middleware_and_review_order(config_factory, final_response):
    events = []

    class Memory:
        async def recall(self, user_input, session_id):
            events.append("memory")
            return [MemoryItem(id="m1", content="remember this")]

    class Middleware:
        async def on_turn_start(self, event):
            events.append("start")

        async def before_model(self, event):
            events.append("before_model")

        async def after_model(self, event):
            events.append("after_model")

        async def on_turn_end(self, event):
            events.append("end")

    class Review:
        async def schedule(self, snapshot):
            events.append("review")

    transport = ScriptedTransport(final_response)
    async with AgentCore(
        config_factory(),
        default_tool_registry(),
        memory=Memory(),
        middleware=[Middleware()],
        review_scheduler=Review(),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("raw text", session_id="hooks")
        stored = await agent.store.load_messages("hooks")

    assert result.status == "completed"
    assert events == ["memory", "start", "before_model", "after_model", "end", "review"]
    assert stored[0].content == "raw text"
    assert "untrusted context" in stored[0].api_content
    assert "remember this" in transport.requests[0].messages[1]["content"]


@pytest.mark.asyncio
async def test_multiple_tools_execute_sequentially_with_middleware(config_factory):
    execution_order = []

    async def echo(args: EchoArgs, context: ToolContext):
        execution_order.append(f"run:{args.text}")
        return args.text

    class Middleware:
        async def before_tool(self, invocation):
            execution_order.append(f"before:{invocation.arguments['text']}")

        async def after_tool(self, event):
            execution_order.append(f"after:{event.invocation.arguments['text']}")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", description="Echo text.", args_model=EchoArgs, handler=echo)
    )
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[
                ToolCall(id="m1", name="echo", arguments='{"text":"one"}'),
                ToolCall(id="m2", name="echo", arguments='{"text":"two"}'),
            ],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="both done", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(),
        registry,
        middleware=[Middleware()],
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("two tools", session_id="multi")

    assert result.status == "completed"
    assert execution_order == [
        "before:one",
        "run:one",
        "after:one",
        "before:two",
        "run:two",
        "after:two",
    ]
    assert [message["role"] for message in transport.requests[1].messages][-3:] == [
        "assistant",
        "tool",
        "tool",
    ]


@pytest.mark.asyncio
async def test_guard_failure_is_fail_closed(config_factory):
    called = False

    async def echo(args: EchoArgs, context: ToolContext):
        nonlocal called
        called = True
        return args.text

    class BrokenGuard:
        async def check(self, invocation):
            raise RuntimeError("guard unavailable")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", description="Echo text.", args_model=EchoArgs, handler=echo)
    )
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="g1", name="echo", arguments='{"text":"blocked"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="safe", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(), registry, guard=BrokenGuard(), transports={"primary": transport}
    ) as agent:
        result = await agent.run_turn("guard", session_id="guard")

    assert result.status == "completed"
    assert called is False
    payload = json.loads(transport.requests[1].messages[-1]["content"])
    assert payload["error"]["type"] == "guard_error"


@pytest.mark.asyncio
async def test_review_failure_only_adds_warning(config_factory, final_response):
    class BrokenReview:
        async def schedule(self, snapshot):
            raise RuntimeError("queue down")

    async with AgentCore(
        config_factory(),
        default_tool_registry(),
        review_scheduler=BrokenReview(),
        transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = await agent.run_turn("review", session_id="review")

    assert result.status == "completed"
    assert any("background review scheduling failed" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_registry_freezes_at_agent_construction(config_factory, final_response):
    registry = ToolRegistry()
    agent = AgentCore(
        config_factory(), registry, transports={"primary": ScriptedTransport(final_response)}
    )
    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(
            ToolSpec(
                name="late",
                description="Too late.",
                args_model=ApprovalArgs,
                handler=lambda args, context: None,
            )
        )
    await agent.aclose()


@pytest.mark.asyncio
async def test_max_iterations_returns_failed_result(config_factory):
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="c1", name="calculator", arguments='{"expression":"1+1"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(
            tool_calls=[ToolCall(id="c2", name="calculator", arguments='{"expression":"2+2"}')],
            finish_reason="tool_calls",
        ),
    )
    async with AgentCore(
        config_factory(max_iterations=2),
        default_tool_registry(),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("loop", session_id="limit")

    assert result.status == "failed"
    assert result.error.code == "max_iterations"
    assert result.iterations == 2
