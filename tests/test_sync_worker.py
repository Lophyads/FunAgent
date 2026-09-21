from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest
from pydantic import BaseModel

from agentcore import (
    AgentCore,
    Message,
    ModelResponse,
    SQLiteStore,
    TimeoutPolicy,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    TurnQueueFullError,
    WorkerPolicy,
    default_tool_registry,
)
from conftest import ScriptedTransport


@pytest.mark.asyncio
async def test_async_facade_runs_complete_turn_off_caller_event_loop(
    config_factory, final_response
):
    caller_thread = threading.get_ident()
    observations: list[tuple[int, bool]] = []

    class InspectingTransport(ScriptedTransport):
        def complete(self, request):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                has_loop = False
            else:
                has_loop = True
            observations.append((threading.get_ident(), has_loop))
            time.sleep(0.08)
            return super().complete(request)

    heartbeat = 0

    async def beat():
        nonlocal heartbeat
        while True:
            heartbeat += 1
            await asyncio.sleep(0.005)

    ticker = asyncio.create_task(beat())
    try:
        async with AgentCore(
            config_factory(),
            default_tool_registry(),
            transports={"primary": InspectingTransport(final_response)},
        ) as agent:
            result = await agent.run_turn("hello", session_id="off-loop")
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker

    assert result.status == "completed"
    assert observations == [(observations[0][0], False)]
    assert observations[0][0] != caller_thread
    assert heartbeat >= 5


def test_blocking_api_still_dispatches_turn_to_worker(config_factory, final_response):
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    def inspect(request):
        worker_threads.append(threading.get_ident())
        return final_response

    with AgentCore(
        config_factory(),
        default_tool_registry(),
        transports={"primary": ScriptedTransport(inspect)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="blocking")

    assert result.status == "completed"
    assert worker_threads and worker_threads[0] != caller_thread


@pytest.mark.asyncio
async def test_same_session_is_fifo_without_occupying_second_turn_worker(config_factory):
    entered = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    class BlockingFirstTransport:
        def complete(self, request):
            text = next(
                message["content"] for message in reversed(request.messages)
                if message["role"] == "user"
            )
            seen.append(text)
            if text == "first":
                entered.set()
                release.wait(2)
            return ModelResponse(content=f"done:{text}", finish_reason="stop")

        def close(self):
            return None

    async with AgentCore(
        config_factory(workers=WorkerPolicy(turn_workers=2, operation_workers=4, queue_capacity=8)),
        default_tool_registry(),
        transports={"primary": BlockingFirstTransport()},
    ) as agent:
        first = asyncio.create_task(agent.run_turn("first", session_id="fifo"))
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(agent.run_turn("second", session_id="fifo"))
        await asyncio.sleep(0.05)
        assert seen == ["first"]
        third = await asyncio.wait_for(
            agent.run_turn("third", session_id="other-session"), timeout=1
        )
        assert third.final_text == "done:third"
        assert seen == ["first", "third"]
        release.set()
        results = await asyncio.gather(first, second)

    assert seen == ["first", "third", "second"]
    assert [result.final_text for result in results] == ["done:first", "done:second"]


@pytest.mark.asyncio
async def test_turn_queue_capacity_is_enforced(config_factory):
    entered = threading.Event()
    release = threading.Event()

    def block(request):
        entered.set()
        release.wait(2)
        return ModelResponse(content="done", finish_reason="stop")

    agent = AgentCore(
        config_factory(workers=WorkerPolicy(turn_workers=1, operation_workers=2, queue_capacity=1)),
        default_tool_registry(),
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
async def test_async_cancellation_propagates_and_worker_finalizes(config_factory):
    entered = threading.Event()
    release = threading.Event()
    transport = ScriptedTransport(
        lambda request: (
            entered.set(),
            release.wait(2),
            ModelResponse(content="late", finish_reason="stop"),
        )[-1]
    )
    agent = AgentCore(
        config_factory(), default_tool_registry(), transports={"primary": transport}
    )
    await agent.start()
    task = asyncio.create_task(agent.run_turn("cancel me", session_id="cancel"))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    status = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with sqlite3.connect(agent.config.database_path) as connection:
            row = connection.execute(
                "SELECT status FROM turns WHERE session_id = 'cancel' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        status = row[0] if row else None
        if status == "interrupted":
            break
        await asyncio.sleep(0.02)
    await agent.aclose()
    assert status == "interrupted"


class SlowArgs(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_tool_timeout_is_unknown_and_does_not_block_event_loop(config_factory):
    handler_started = threading.Event()

    def slow_tool(args: SlowArgs, context: ToolContext):
        handler_started.set()
        time.sleep(0.2)
        return {"value": args.value}

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="slow", description="A slow side effect.", args_model=SlowArgs,
        handler=slow_tool, side_effects="possible", timeout_seconds=0.03,
    ))
    transport = ScriptedTransport(
        ModelResponse(
            tool_calls=[ToolCall(id="slow-1", name="slow", arguments='{"value":"x"}')],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="reported unknown", finish_reason="stop"),
    )
    async with AgentCore(
        config_factory(timeouts=TimeoutPolicy(tool_seconds=0.03)),
        registry,
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("run", session_id="timeout")

    assert handler_started.is_set()
    assert result.status == "completed"
    assert result.completion_reason == "model_stop"
    assert result.tool_outcomes[0].state == "unknown"
    assert result.tool_outcomes[0].effect_disposition == "unknown"
    assert result.tool_outcomes[0].may_still_be_running is True
    payload = json.loads(transport.requests[1].messages[-1]["content"])
    assert payload["error"]["type"] == "tool_timeout"


def test_async_extensions_are_rejected(config_factory, final_response):
    registry = ToolRegistry()

    async def async_tool(args: SlowArgs, context: ToolContext):
        return None

    with pytest.raises(TypeError, match="async tool"):
        registry.register(ToolSpec(
            name="async_tool", description="Invalid.", args_model=SlowArgs,
            handler=async_tool,
        ))

    class AsyncTransport:
        async def complete(self, request):
            return final_response

    with pytest.raises(TypeError, match="transport"):
        AgentCore(
            config_factory(), default_tool_registry(),
            transports={"primary": AsyncTransport()},
        )

    class AsyncMiddleware:
        async def before_model(self, event):
            return None

    with pytest.raises(TypeError, match="middleware"):
        AgentCore(
            config_factory(), default_tool_registry(), middleware=[AsyncMiddleware()],
            transports={"primary": ScriptedTransport(final_response)},
        )


def test_recovery_repairs_pending_and_running_calls_even_after_turn_is_failed(config_factory):
    config = config_factory()
    registry = default_tool_registry()
    registry.freeze()
    store = SQLiteStore(config.database_path)
    store.open()
    state = store.ensure_session_context("repair-all", config.system_prompt, registry.definitions())
    store.begin_turn(
        turn_id="old", session_id="repair-all", context_version=state.version,
        user_message=Message(role="user", content="old"),
    )
    assistant = Message(
        role="assistant",
        tool_calls=[
            ToolCall(id="pending", name="current_time", arguments="{}"),
            ToolCall(id="running", name="current_time", arguments="{}"),
        ],
        finish_reason="tool_calls",
    )
    store.stage_tool_calls(
        "repair-all", "old", assistant,
        [(assistant.tool_calls[0], "none"), (assistant.tool_calls[1], "possible")],
    )
    store.mark_tool_running("old", "running")
    store.finish_turn(
        session_id="repair-all", turn_id="old", status="failed", error=None,
        route_name="primary", iterations=1, usage={}, completion_reason="simulated_crash",
    )

    recovery = store.recover_incomplete_turns("repair-all")
    messages = store.load_messages("repair-all")
    outcomes = store.get_tool_outcomes("old")
    store.close()

    assert len(recovery.messages) == 2
    assert [message.role for message in messages] == ["user", "assistant", "tool", "tool"]
    assert [outcome.state for outcome in outcomes] == ["not_executed", "unknown"]
    assert [outcome.effect_disposition for outcome in outcomes] == ["not_applied", "unknown"]


def test_v1_database_migrates_to_v2_without_losing_rows(tmp_path):
    database = tmp_path / "v1.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, current_context_version INTEGER NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE session_contexts (
            session_id TEXT NOT NULL, version INTEGER NOT NULL,
            system_prompt TEXT NOT NULL, tools_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (session_id, version)
        );
        CREATE TABLE turns (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, context_version INTEGER NOT NULL,
            status TEXT NOT NULL, started_at REAL NOT NULL, completed_at REAL,
            final_message_id INTEGER, error_json TEXT, route_name TEXT,
            iterations INTEGER NOT NULL DEFAULT 0, usage_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT, api_content TEXT, tool_calls_json TEXT, tool_call_id TEXT,
            tool_name TEXT, finish_reason TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL, UNIQUE(session_id, seq)
        );
        INSERT INTO sessions VALUES ('kept', 1, 1, 1);
        INSERT INTO session_contexts VALUES ('kept', 1, 'prompt', '[]', 'fp', 1);
        INSERT INTO turns VALUES ('turn', 'kept', 1, 'completed', 1, 2, NULL, NULL, 'p', 1, '{}');
        INSERT INTO messages(
            session_id, turn_id, seq, role, content, metadata_json, created_at
        ) VALUES ('kept', 'turn', 1, 'user', 'preserve me', '{}', 1);
        """)

    store = SQLiteStore(database)
    store.open()
    messages = store.load_messages("kept")
    store.close()

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        turn_columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        ledger_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_executions'"
        ).fetchone()

    assert version == "2"
    assert messages[0].content == "preserve me"
    assert {"completion_reason", "provider_attempts", "route_attempts_json"} <= turn_columns
    assert "effect_disposition" in message_columns
    assert ledger_exists == (1,)


def test_sync_transaction_rolls_back_base_exception_and_store_remains_usable(tmp_path):
    store = SQLiteStore(tmp_path / "rollback.db")
    store.open()
    with pytest.raises(KeyboardInterrupt):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES ('rolled-back', 1, 1, 1)"
            )
            raise KeyboardInterrupt

    state = store.ensure_session_context("healthy", "prompt", [])
    with sqlite3.connect(tmp_path / "rollback.db") as connection:
        rolled_back = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 'rolled-back'"
        ).fetchone()[0]
    store.close()

    assert rolled_back == 0
    assert state.version == 1

@pytest.mark.asyncio
async def test_storage_and_lifecycle_hooks_have_no_running_event_loop(
    config_factory, final_response
):
    caller_thread = threading.get_ident()
    observations: list[tuple[str, int, bool]] = []

    def observe(label: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            has_loop = False
        else:
            has_loop = True
        observations.append((label, threading.get_ident(), has_loop))

    class RecordingStore(SQLiteStore):
        def begin_turn(self, **kwargs):
            observe("storage")
            return super().begin_turn(**kwargs)

    class Middleware:
        def before_model(self, event):
            observe("middleware")

    config = config_factory()
    async with AgentCore(
        config,
        default_tool_registry(),
        middleware=[Middleware()],
        transports={"primary": ScriptedTransport(final_response)},
        store=RecordingStore(config.database_path),
    ) as agent:
        await agent.run_turn("hello", session_id="thread-assertions")

    assert {item[0] for item in observations} == {"storage", "middleware"}
    assert all(thread_id != caller_thread and not has_loop for _, thread_id, has_loop in observations)

@pytest.mark.asyncio
async def test_run_budget_expiry_during_tool_keeps_budget_completion_reason(config_factory):
    def slow(args: SlowArgs, context: ToolContext):
        time.sleep(0.15)
        return args.value

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="budget_slow", description="Slow operation.", args_model=SlowArgs,
        handler=slow, side_effects="possible", timeout_seconds=1.0,
    ))
    transport = ScriptedTransport(ModelResponse(
        tool_calls=[ToolCall(
            id="budget-tool", name="budget_slow", arguments='{"value":"x"}'
        )],
        finish_reason="tool_calls",
    ))
    async with AgentCore(
        config_factory(timeouts=TimeoutPolicy(run_budget_seconds=0.03, tool_seconds=1.0)),
        registry,
        transports={"primary": transport},
    ) as agent:
        result = await agent.run_turn("run", session_id="budget-tool")

    assert result.status == "interrupted"
    assert result.completion_reason == "run_budget_exhausted"
    assert result.error.code == "run_budget_exhausted"
    assert result.tool_outcomes[0].state == "unknown"