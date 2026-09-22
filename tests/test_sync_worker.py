from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from pydantic import BaseModel

from agentcore import (
    NormalizedModelResponse, TimeoutPolicy, ToolCall, ToolCatalog, ToolContext,
    ToolSpec, TurnQueueFullError, WorkerPolicy,
)
from conftest import ScriptedTransport


class SlowArgs(BaseModel):
    value: str


def _catalog_with(spec: ToolSpec) -> ToolCatalog:
    catalog = ToolCatalog()
    catalog.register(spec)
    return catalog


def _tool_spec(name: str, handler, **kwargs) -> ToolSpec:
    return ToolSpec(
        tool_id=f"test.{name}", implementation_version="1", name=name,
        description=f"Test tool {name}.", args_model=SlowArgs, handler=handler,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_async_facade_runs_provider_off_caller_event_loop(
    config_factory, agent_factory, final_response,
):
    caller_thread = threading.get_ident()
    observations = []

    class InspectingTransport(ScriptedTransport):
        def send(self, request, context):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                has_loop = False
            else:
                has_loop = True
            observations.append((threading.get_ident(), has_loop))
            time.sleep(0.04)
            return super().send(request, context)

    heartbeat = 0
    async def beat():
        nonlocal heartbeat
        while True:
            heartbeat += 1
            await asyncio.sleep(0.005)

    ticker = asyncio.create_task(beat())
    try:
        async with agent_factory(
            config_factory(), transports={"primary": InspectingTransport(final_response)},
        ) as agent:
            result = await agent.run_turn("hello", session_id="off-loop")
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker

    assert result.status == "completed"
    assert observations[0][0] != caller_thread
    assert observations[0][1] is False
    assert heartbeat >= 4


def test_blocking_api_dispatches_turn_to_worker(config_factory, agent_factory, final_response):
    caller_thread = threading.get_ident()
    worker_threads = []

    def inspect(request):
        worker_threads.append(threading.get_ident())
        return final_response

    with agent_factory(
        config_factory(), transports={"primary": ScriptedTransport(inspect)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="blocking")

    assert result.status == "completed"
    assert worker_threads[0] != caller_thread


@pytest.mark.asyncio
async def test_same_session_is_fifo_without_blocking_other_sessions(config_factory, agent_factory):
    entered = threading.Event()
    release = threading.Event()
    seen = []

    class BlockingTransport:
        def send(self, request, context):
            text = next(message["content"] for message in reversed(request.body["messages"]) if message["role"] == "user")
            seen.append(text)
            if text == "first":
                entered.set()
                release.wait(2)
            return type("Wire", (), {"raw": NormalizedModelResponse(content=f"done:{text}")})()
        def close(self):
            return None

    async with agent_factory(
        config_factory(workers=WorkerPolicy(turn_workers=2, operation_workers=4, queue_capacity=8)),
        transports={"primary": BlockingTransport()},
    ) as agent:
        first = asyncio.create_task(agent.run_turn("first", session_id="fifo"))
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(agent.run_turn("second", session_id="fifo"))
        await asyncio.sleep(0.03)
        assert seen == ["first"]
        third = await asyncio.wait_for(agent.run_turn("third", session_id="other"), timeout=1)
        assert third.final_text == "done:third"
        release.set()
        results = await asyncio.gather(first, second)

    assert seen == ["first", "third", "second"]
    assert [result.final_text for result in results] == ["done:first", "done:second"]


@pytest.mark.asyncio
async def test_turn_queue_capacity_is_enforced(config_factory, agent_factory):
    entered = threading.Event()
    release = threading.Event()

    def block(request):
        entered.set()
        release.wait(2)
        return NormalizedModelResponse(content="done")

    agent = agent_factory(
        config_factory(workers=WorkerPolicy(turn_workers=1, operation_workers=2, queue_capacity=1)),
        transports={"primary": ScriptedTransport(block)},
    )
    await agent.start()
    first = asyncio.create_task(agent.run_turn("first", session_id="one"))
    assert await asyncio.to_thread(entered.wait, 1)
    with pytest.raises(TurnQueueFullError):
        await agent.run_turn("second", session_id="two")
    release.set()
    await first
    await agent.aclose()


@pytest.mark.asyncio
async def test_tool_timeout_is_unknown_and_does_not_block_event_loop(config_factory, agent_factory):
    handler_started = threading.Event()

    def slow_tool(args: SlowArgs, context: ToolContext):
        handler_started.set()
        time.sleep(0.15)
        return {"value": args.value}

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(id="slow-1", name="slow", arguments='{"value":"x"}')], finish_reason="tool_calls"),
        NormalizedModelResponse(content="reported unknown", finish_reason="stop"),
    )
    async with agent_factory(
        config_factory(timeouts=TimeoutPolicy(tool_seconds=0.02)),
        tool_catalog=_catalog_with(_tool_spec("slow", slow_tool, side_effects="possible", timeout_seconds=0.02)),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("run", session_id="timeout")

    assert handler_started.is_set()
    assert result.status == "completed"
    assert handler_started.is_set()
    assert result.tool_outcomes[0].state == "unknown"
    assert result.tool_outcomes[0].effect_disposition == "unknown"
    assert result.tool_outcomes[0].may_still_be_running is True
    payload = json.loads(transport.requests[1].body["messages"][-1]["content"])
    assert payload["error"]["type"] == "outcome_unknown"


def test_async_extensions_are_rejected(config_factory, agent_factory, final_response):
    catalog = ToolCatalog()
    async def async_tool(args: SlowArgs, context: ToolContext):
        return None
    with pytest.raises(TypeError, match="async tool"):
        catalog.register(_tool_spec("async_tool", async_tool))

    class AsyncTransport:
        async def send(self, request, context):
            return None
        def close(self):
            return None

    with pytest.raises(TypeError, match="Transport"):
        agent_factory(config_factory(), transports={"primary": AsyncTransport()})

    class AsyncObserver:
        async def on_event(self, event):
            return None

    with pytest.raises(TypeError, match="observer"):
        agent_factory(
            config_factory(), transports={"primary": ScriptedTransport(final_response)},
            observers=[AsyncObserver()],
        )


@pytest.mark.asyncio
async def test_run_budget_expiry_during_tool_keeps_budget_completion_reason(config_factory, agent_factory):
    handler_started = threading.Event()

    def slow(args: SlowArgs, context: ToolContext):
        handler_started.set()
        time.sleep(0.25)
        return args.value

    transport = ScriptedTransport(NormalizedModelResponse(tool_calls=[ToolCall(id="budget-tool", name="budget_slow", arguments='{"value":"x"}')], finish_reason="tool_calls"))
    async with agent_factory(
        config_factory(timeouts=TimeoutPolicy(run_budget_seconds=0.1, tool_seconds=1.0)),
        tool_catalog=_catalog_with(_tool_spec("budget_slow", slow, side_effects="possible", timeout_seconds=1.0)),
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("run", session_id="budget-tool")

    assert result.status == "interrupted"
    assert result.completion_reason == "run_budget_exhausted"
    assert result.error.code == "run_budget_exhausted"
    assert handler_started.is_set()
    assert result.tool_outcomes[0].state == "unknown"