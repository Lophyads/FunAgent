from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from functools import partial
from types import MappingProxyType, SimpleNamespace

import pytest
from pydantic import BaseModel

from agentcore import (
    AllowAllGuard, ConversationLoop, GuardDecision, LeaseScope,
    LeaseLostError, Message, NoopMemoryUpdater, NoopReviewScheduler,
    NormalizedModelResponse, SessionRuntimeSnapshot,
    SQLiteStore, SQLiteTurnLeaseStore, SystemPromptSnapshot, ToolCall,
    ToolEntry, StorageError, TurnHostContext, UserInput,
)
from agentcore.turn_persistence import TurnPersistence
from agentcore.turn_preparation import prepare_stored_turn
from agentcore.turn_phase import RouteState
from tools.runtime import get_tool_services
from conftest import ScriptedTransport, scripted_provider_stack


class EchoArgs(BaseModel):
    text: str


def tool_response(*ids):
    return NormalizedModelResponse(
        tool_calls=[ToolCall(id=key, name="echo", arguments='{"text":"hello"}') for key in ids],
        finish_reason="tool_calls",
    )


def memory_loop(config, transport, *, handler=lambda args, context: args.text,
                history=(), guard=None, cancel=None, persistence=None, prepare=None,
                workspace=None, model_context=None, turn_id="review-turn", services=None, resource_key=None):
    resolver, adapters, adapter = scripted_provider_stack(
        config, {"primary": transport}, initialize_tools=services is None,
    )
    entry = ToolEntry(
        tool_id="test.echo", implementation_version="1", name="echo",
        description="Echo text", args_model=EchoArgs, handler=handler,
        side_effects="none",
    )
    prompt = "frozen parent prompt\nincluding memory"
    context = model_context if model_context is not None else SessionRuntimeSnapshot(
        prompt=SystemPromptSnapshot(prompt, prompt.encode(), hashlib.sha256(prompt.encode()).hexdigest()),
        entries_by_name=MappingProxyType({entry.name: entry}),
        tool_schema_bytes=json.dumps([entry.definition()]).encode(),
    )
    events = []

    def publish(name, **kwargs):
        events.append(name)
        return ()

    loop = ConversationLoop(
        config=config, user_input=UserInput(text="review"),
        session_id="parent-session", turn_id=turn_id,
        owner_id="review-owner", services=services or get_tool_services(), resource_key=resource_key,
        cancel_event=cancel or threading.Event(), persistence=persistence, workspace=workspace,
        **({"context": context, "history": history} if prepare is None else {"prepare": prepare}),
        provider_resolver=resolver, api_modes=adapters,
        route_states=tuple(RouteState() for _ in config.routes),
        guard=guard or AllowAllGuard(), host_context=TurnHostContext(),
        memory_updater=NoopMemoryUpdater(), review_scheduler=NoopReviewScheduler(),
        observability=SimpleNamespace(publish=publish),
        call_operation=lambda callback, **kwargs: callback(),
    )
    return loop, context, adapter, events


def test_same_loop_runs_tools_without_opening_database_or_acquiring_lease(config_factory, monkeypatch):
    config = config_factory()
    seen = []

    def echo(args, context):
        seen.append(context)
        context.check_active()
        return args.text

    transport = ScriptedTransport(tool_response("call-1"), NormalizedModelResponse(content="done"))
    loop, source, adapter, events = memory_loop(config, transport, handler=echo)

    def forbidden(*args, **kwargs):
        pytest.fail("in-memory conversation touched database/lease")

    with monkeypatch.context() as patches:
        patches.setattr(sqlite3, "connect", forbidden)
        patches.setattr(SQLiteTurnLeaseStore, "acquire", forbidden)
        result = loop.run()

    assert result.status == "completed"
    assert result.iterations == 2
    assert result.tool_outcomes[0].state == "succeeded"
    assert [m.role for m in loop.messages] == ["user", "assistant", "tool", "assistant"]
    assert adapter.canonical_requests[0].system_prompt == source.prompt.text
    assert adapter.canonical_requests[0].tool_schema_bytes == source.tool_schema_bytes
    assert "lease.acquired" not in events and "execution.released" in events
    assert not config.database_path.exists()
    assert loop._state.context == source
    assert seen[0].workspace.cwd == get_tool_services().config.workspace.default_cwd
    assert not get_tool_services()._providers
    assert seen[0].session_id == "parent-session"
    with pytest.raises(Exception, match="no longer active|closed"):
        seen[0].check_active()
    with pytest.raises(RuntimeError, match="only run once"):
        loop.run()


def test_memory_loop_isolates_nested_history_and_keeps_completed_transcript(config_factory):
    history = [Message.user("original", metadata={"nested": {"value": "original"}})]
    transport = ScriptedTransport(NormalizedModelResponse(content="done"))
    loop, _, adapter, _ = memory_loop(config_factory(), transport, history=history)
    history[0].metadata["nested"]["value"] = "parent changed"
    assert loop.run().status == "completed"
    assert adapter.canonical_requests[0].messages[0].metadata["nested"]["value"] == "original"
    snapshot = loop.messages
    snapshot[0].metadata["nested"]["value"] = "reader changed"
    assert loop.messages[0].metadata["nested"]["value"] == "original"
    assert len(history) == 1
    assert loop.messages[-1].content == "done"


def test_memory_loop_keeps_tool_guard_and_returns_denial_to_model(config_factory):
    called = []

    class Deny:
        def check(self, invocation):
            return GuardDecision(allowed=False, reason="review tool restriction")

    transport = ScriptedTransport(tool_response("denied"), NormalizedModelResponse(content="done"))
    loop, _, adapter, _ = memory_loop(
        config_factory(), transport,
        handler=lambda *args: called.append(True), guard=Deny(),
    )
    result = loop.run()
    assert result.status == "completed"
    assert result.tool_outcomes[0].state == "denied"
    assert not called
    assert "review tool restriction" in adapter.canonical_requests[1].messages[-1].content


def test_cancelled_memory_tool_batch_repairs_only_unexecuted_calls(config_factory):
    cancel = threading.Event()
    called = []

    def echo(args, context):
        called.append(context.tool_call_id)
        cancel.set()
        return args.text

    loop, _, _, _ = memory_loop(
        config_factory(), ScriptedTransport(tool_response("one", "two")),
        handler=echo, cancel=cancel,
    )
    result = loop.run()
    assert result.status == "interrupted"
    assert result.completion_reason == "cancelled"
    assert called == ["one"]
    replies = [m for m in loop.messages if m.role == "tool"]
    assert [m.tool_call_id for m in replies] == ["one", "two"]
    assert replies[0].tool_outcome_state == "succeeded"
    assert replies[1].tool_outcome_state == "unknown"
    assert replies[0].reply_to_message_id is replies[1].reply_to_message_id is None
    assert replies[1].effect_disposition == "none"


def test_memory_loop_honors_iteration_limit(config_factory):
    loop, _, _, _ = memory_loop(
        config_factory(max_iterations=1), ScriptedTransport(tool_response("one")),
    )
    result = loop.run()
    assert result.status == "failed" and result.completion_reason == "max_iterations"
    assert loop.messages[-1].role == "tool"
    assert result.tool_outcomes[0].state == "succeeded"


def test_cancel_before_memory_preparation_does_not_admit_messages(config_factory):
    cancel = threading.Event()
    cancel.set()
    transport = ScriptedTransport()
    loop, _, _, _ = memory_loop(config_factory(), transport, cancel=cancel)
    result = loop.run()
    assert result.completion_reason == "cancelled"
    assert not loop.messages and not transport.requests


def test_memory_loop_requires_a_complete_tool_history(config_factory):
    with pytest.raises(ValueError, match="unresolved tool calls"):
        memory_loop(
            config_factory(), ScriptedTransport(),
            history=[Message.assistant_tools(tool_response("pending").tool_calls)],
        )


def lease_scope(config):
    store = SQLiteStore(config.database_path)
    store.open()
    leases = SQLiteTurnLeaseStore(store)
    heartbeat = SimpleNamespace(register=lambda *args: None, unregister=lambda *args: None)
    scope = LeaseScope(
        store=leases, heartbeat=heartbeat, session_id="session",
        owner_id="old-owner", turn_id="turn", policy=config.lease,
    )
    scope.acquire(lambda: None)
    return scope, store


def test_renewed_lease_is_valid_even_when_original_handle_expiry_is_old(config_factory, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("agentcore.storage.time.time", lambda: now[0])
    scope, store = lease_scope(config_factory())
    handle = scope._lease
    try:
        now[0] = handle.expires_at - 1
        renewed = store.renew_lease(handle, ttl_seconds=60)
        now[0] = handle.expires_at + 1
        assert handle.expires_at < now[0] < renewed.expires_at
        scope.assert_current()  # checks authoritative storage, not cached expiry
    finally:
        scope.close()
        store.close()


def test_stale_owner_cannot_execute_or_write_after_a_successful_prior_check(config_factory):
    scope, store = lease_scope(config_factory())
    try:
        scope.assert_current()
        store.release_lease(scope._lease)
        successor = store.acquire_lease(
            session_id="session", owner_id="new-owner", turn_id="new-turn", ttl_seconds=60,
        )
        assert successor is not None
        with pytest.raises(LeaseLostError):
            scope.assert_current()
        # Even bypassing the preflight check cannot bypass the atomic write fence.
        with pytest.raises(LeaseLostError):
            TurnPersistence("session", store, fence=scope.fence).save_tool_calls(Message.assistant_tools(
                tool_response("blocked").tool_calls,
                tool_call_effects={"blocked": "none"},
            ))
        with pytest.raises(LeaseLostError):
            scope.close()
        store.assert_lease(successor)  # old cleanup must not release the successor
    finally:
        scope.close()
        store.close()


def test_disabled_writer_skips_all_writes_even_with_a_store(config_factory, monkeypatch):
    store = SQLiteStore(config_factory().database_path)
    store.open()
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("disabled persistence opened a write transaction")

        monkeypatch.setattr(store, "_transaction", forbidden)
        writer = TurnPersistence("branch", store, persist_disabled=True)
        writer.save_new_session(
            system_prompt_bytes=b"prompt", user_message=Message.user("question"),
        )
        writer.save_user_message(Message.user("next"))
        writer.save_tool_calls(Message.assistant_tools(tool_response("one").tool_calls))
        writer.save_tool_result(Message.tool_result(
            "ok", tool_call_id="one", tool_name="echo",
            tool_outcome_state="succeeded", effect_disposition="none",
        ))
        writer.save_final_response(
            Message.assistant_text("done"), delivery_destination="channel", recovery=True,
        )
        assert not writer.save_recovery_claim(max_attempts=3)
        writer.save_recovery_failure(max_attempts=3)
        assert not writer.repair_stored_tool_batch().messages
        assert store.load_messages("branch") == []
        assert store.load_session("branch") is None
    finally:
        store.close()


def test_disabled_loop_can_read_existing_history_without_writing(
    config_factory, agent_factory, monkeypatch,
):
    config = config_factory()
    with agent_factory(config, transports={"primary": ScriptedTransport(
        NormalizedModelResponse(content="parent answer"),
    )}) as agent:
        assert agent.run_turn_blocking("parent question", session_id="parent-session").status == "completed"
        store = agent._store
        before = store.load_messages("parent-session")
        with sqlite3.connect(config.database_path) as connection:
            durable_before = list(connection.iterdump())
        writer = TurnPersistence("parent-session", store, persist_disabled=True)
        reads = []
        load = store.load_messages

        def read_history(session_id):
            reads.append(session_id)
            return load(session_id)

        def forbidden(*args, **kwargs):
            pytest.fail("read-only branch attempted a database mutation or lease acquisition")

        monkeypatch.setattr(store, "load_messages", read_history)
        monkeypatch.setattr(store, "_transaction", forbidden)
        monkeypatch.setattr(SQLiteTurnLeaseStore, "acquire", forbidden)
        prepare = partial(
            prepare_stored_turn, store=store, contexts=agent._session_contexts,
            persistence=writer, user_input=UserInput(text="review"), request=None,
            host_context=TurnHostContext(), recovery=False,
        )
        loop, _, adapter, events = memory_loop(
            config, ScriptedTransport(NormalizedModelResponse(content="review answer")),
            persistence=writer, prepare=prepare,
        )
        result = loop.run()
        assert result.status == "completed"
        assert reads == ["parent-session"]
        assert adapter.canonical_requests[0].messages == tuple(before + [Message.user("review")])
        assert loop.messages[-1].content == "review answer"
        assert "lease.acquired" not in events
        with sqlite3.connect(config.database_path) as connection:
            assert list(connection.iterdump()) == durable_before


def test_enabled_writer_requires_a_fence_before_any_database_access(config_factory):
    # Intentionally unopened: missing credentials must fail before opening storage.
    writer = TurnPersistence("session", SQLiteStore(config_factory().database_path))
    with pytest.raises(RuntimeError, match="requires a fence"):
        writer.save_user_message(Message.user("question"))


def test_persistence_maps_reused_tool_ids_per_batch_without_mutating_messages(config_factory):
    scope, store = lease_scope(config_factory())
    writer = TurnPersistence("session", store, fence=scope.fence)
    try:
        writer.save_new_session(
            system_prompt_bytes=b"prompt", user_message=Message.user("question"),
        )
        message = Message.tool_result(
            "ok", tool_call_id="reused", tool_name="echo",
            tool_outcome_state="succeeded", effect_disposition="none",
        )
        for _ in range(2):
            writer.save_tool_calls(Message.assistant_tools(
                tool_response("reused").tool_calls, tool_call_effects={"reused": "none"},
            ))
            writer.save_tool_result(message)
            assert message.reply_to_message_id is None
        replies = [m for m in store.load_messages("session") if m.role == "tool"]
        assert len(replies) == 2
        assert replies[0].reply_to_message_id != replies[1].reply_to_message_id
        assert all(m.reply_to_message_id > 0 for m in replies)
        # Memory messages may omit the DB foreign key; direct storage may not.
        with pytest.raises(StorageError, match="database parent ID"):
            store.append_tool_result("session", message, fence=scope.fence)
    finally:
        scope.close()
        store.close()


def test_tool_call_write_failure_stops_before_tool_execution(
    config_factory, agent_factory, monkeypatch,
):
    from agentcore.tool_execution import ToolExecutor

    config = config_factory()

    class FailingStore(SQLiteStore):
        def append_tool_calls(self, *args, **kwargs):
            raise StorageError("cannot stage tool call")

    def forbidden(*args, **kwargs):
        pytest.fail("tool executor entered after failed durable staging")

    monkeypatch.setattr(ToolExecutor, "execute", forbidden)
    store = FailingStore(config.database_path)
    with agent_factory(
        config, transports={"primary": ScriptedTransport(tool_response("blocked"))}, store=store,
    ) as agent:
        result = agent.run_turn_blocking("question", session_id="stage-failure")
        assert result.status == "failed"
        assert "cannot stage tool call" in result.error.message
        assert [m.role for m in store.load_messages("stage-failure")] == ["user"]
        assert not result.tool_outcomes
