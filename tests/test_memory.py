from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace
from typing import Literal

import pytest

from agentcore import Message, NormalizedModelResponse, SessionRuntimeSnapshot, SystemPromptSnapshot, ToolCall, TurnResult, TurnSnapshot
from agentcore.background_review import ReviewTask
from agentcore.interfaces import ToolInvocation, TurnCancelled
from agentcore.prompt_builder import PromptBuildRequest, StaticSystemPromptBuilder
from conftest import ScriptedTransport, builtin_registry, scripted_provider_stack
from memory.feature import MemoryConfig, MemoryFeature, SqliteConfig
from memory.prompt import MemoryPromptBuilder, format_entry
from memory.review import MemoryOnlyGuard, MemoryReviewRunner, MemoryReviewScheduler, history_fingerprint
from memory.sqlite import SqliteMemoryStore
from memory.store import MemoryDraft, MemoryError, MemoryPatch, MemoryQuery, MemorySearch
from memory.tool import MemoryArgs, memory_entry
from tools.file_tool import ReadFileArgs, read_file
from tools.runtime import get_tool_registry


def snapshot(store, messages, *, turn="t", status="completed", trigger="user"):
    from agentcore.models import TurnError
    prompt = b"original prompt"
    return TurnSnapshot(
        result=TurnResult(status=status, session_id="s", turn_id=turn,
                          error=TurnError(code="failed", message="failed") if status != "completed" else None),
        messages=tuple(message.model_dump(mode="json") for message in messages),
        context=SessionRuntimeSnapshot(SystemPromptSnapshot(prompt.decode(), prompt, hashlib.sha256(prompt).hexdigest()),
                                       {"memory": memory_entry(store)}, b"[]"), trigger_kind=trigger,
    )


def test_prompt_budget_prefers_user_and_never_reads_markdown(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    user = store.add(MemoryDraft("user", "prefers tea")).entry
    store.add(MemoryDraft("note", "project knowledge"))
    store.add(MemoryDraft("episode", "historical event"))
    (tmp_path / "USER.md").write_text("malicious file instructions", encoding="utf-8")
    builder = MemoryPromptBuilder(StaticSystemPromptBuilder("System"), store, len(format_entry(user)))
    prompt = builder.build(PromptBuildRequest(()))
    assert "prefers tea" in prompt.text
    assert "project knowledge" not in prompt.text and "historical event" not in prompt.text
    assert "malicious" not in prompt.text and "untrusted context" in prompt.text
    assert prompt.fingerprint == hashlib.sha256(prompt.content_bytes).hexdigest()
    store.close()
    assert "prefers tea" not in builder.build(PromptBuildRequest(())).text


def test_tool_policies_review_guard_and_record_versions(tmp_path, context_factory):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    front, review = memory_entry(store), memory_entry(store, review=True)
    assert front.policy_resolver(MemoryArgs(action="search", query="tea")).side_effects == "none"
    assert front.policy_resolver(MemoryArgs(action="add", kind="user", content="x")).approval_scopes == ()
    remove = MemoryArgs(action="remove", id="opaque:uuid", expected_version=1)
    assert front.policy_resolver(remove).approval_scopes == ("memory:edit",)
    assert review.args_model.model_fields["action"].annotation == Literal["list", "search", "add"]
    with context_factory() as context:
        added = front.handler(MemoryArgs(action="add", kind="user", content="prefers tea"), context)
        assert added["entry"]["source"]["turn_id"] == context.turn_id
        assert review.handler(MemoryArgs(action="search", query="tea"), context)["entries"][0]["id"] == added["entry"]["id"]
        with pytest.raises(PermissionError):
            review.handler(remove, context)
    assert not MemoryOnlyGuard().check(ToolInvocation("s", "t", "c", "read_file", {}, False)).allowed
    store.close()


def test_feature_protects_only_database_and_sidecars(tmp_path, context_factory, config_factory):
    path = tmp_path / "memory.db"
    settings = MemoryConfig(review_every_turns=0, sqlite=SqliteConfig(path))
    with context_factory() as context:
        feature = MemoryFeature(settings, config_factory())
        feature.install_tool(get_tool_registry())
        try:
            assert get_tool_registry().get_entry("memory") is not None
            for suffix in ("", "-wal", "-shm", "-journal"):
                assert context.services.is_protected_path(str(path) + suffix)
            assert not context.services.is_protected_path(tmp_path / "other.txt")
            with pytest.raises(ValueError, match="protected"):
                read_file(ReadFileArgs(path="memory.db"), context)
        finally:
            assert feature.close()
        assert not context.services.is_protected_path(path)


def test_new_session_reads_database_existing_prompt_stays_frozen(tmp_path, config_factory, agent_factory):
    config = config_factory()
    feature = MemoryFeature(MemoryConfig(review_every_turns=0, sqlite=SqliteConfig(tmp_path / "memory.db")), config)
    registry = builtin_registry()
    registry.replace_all((*registry.entries(), memory_entry(feature.store)))
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(id="c", name="memory", arguments=json.dumps({"action":"add", "kind":"user", "content":"prefers tea"}))]),
        NormalizedModelResponse(content="saved"), NormalizedModelResponse(content="old"), NormalizedModelResponse(content="new"),
    )
    agent = agent_factory(config, transports={"primary":transport}, tool_registry=registry, prompt_builder=feature.prompt_builder)
    try:
        assert agent.run_turn_blocking("remember", session_id="first").status == "completed"
        assert feature.store.search(MemorySearch("tea"))
        assert agent.run_turn_blocking("again", session_id="first").status == "completed"
        assert agent.run_turn_blocking("hello", session_id="second").status == "completed"
        prompts = [request.body["messages"][0]["content"] for request in transport.requests]
        assert "prefers tea" not in prompts[0]
        assert prompts[0] == prompts[1] == prompts[2] and "prefers tea" in prompts[3]
    finally:
        assert agent.close_blocking().completed
        assert feature.close()


def test_review_cadence_survives_restart_and_memory_reads_do_not_reset(tmp_path):
    path = tmp_path / "memory.db"
    store = SqliteMemoryStore(path)
    queued = []
    scheduler = MemoryReviewScheduler(SimpleNamespace(submit=queued.append), 2, store)
    first = (Message.user("one"), Message.assistant_text("ok"))
    scheduler.schedule(snapshot(store, first, turn="one"))
    scheduler.schedule(snapshot(store, first, turn="one"))
    assert not queued and store.review_state("s").completed_count == 1
    store.close(); store = SqliteMemoryStore(path)
    scheduler = MemoryReviewScheduler(SimpleNamespace(submit=queued.append), 2, store)
    second = (*first, Message.user("two"), Message.assistant_tools([ToolCall(id="c", name="memory", arguments='{"action":"list"}')]),
              Message.tool_result("[]", tool_call_id="c", tool_name="memory", effect_disposition="none", tool_outcome_state="succeeded"), Message.assistant_text("ok"))
    scheduler.schedule(snapshot(store, second, turn="two", trigger="internal_recovery"))
    scheduler.schedule(snapshot(store, second, turn="two", status="failed"))
    scheduler.schedule(snapshot(store, second, turn="two"))
    assert len(queued) == 1 and store.review_state("s").completed_count == 2
    store.close()


def test_review_runner_only_unreviewed_range_without_persistence(tmp_path, config_factory):
    config = config_factory()
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.add(MemoryDraft("user", "existing preference"))
    first = (Message.user("old turn"), Message.assistant_text("old answer"))
    store.mark_reviewed("s", 2, history_fingerprint([m.model_dump() for m in first]))
    recent = (Message.user("I prefer tea"), Message.assistant_text("noted"))
    snap = snapshot(store, (*first, *recent), turn="foreground")
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(id="r", name="memory", arguments=json.dumps({"action":"add", "kind":"user", "content":"prefers tea"}))]),
        NormalizedModelResponse(content="review done"),
    )
    resolver, modes, _ = scripted_provider_stack(config, {"primary":transport})
    runner = MemoryReviewRunner(config, store, store)
    runner.resolver.close(); runner.resolver, runner.api_modes = resolver, modes
    try:
        runner(ReviewTask("review-id", snap, threading.Event()))
        body = transport.requests[0].body
        assert "Review the supplied conversation" in body["messages"][0]["content"]
        assert "original prompt" not in body["messages"][0]["content"]
        assert body["messages"][1:3] == [m.to_wire() for m in recent]
        assert "existing preference" in body["messages"][-1]["content"]
        assert [tool["function"]["name"] for tool in body["tools"]] == ["memory"]
        assert store.review_state("s").reviewed_position == 4
        entry = store.search(MemorySearch("tea"))[0]
        assert entry.source["turn_id"] == "foreground" and entry.source["range_start"] == 2
        assert len(snap.messages) == 4 and snap.context.prompt.text == "original prompt"
        assert not config.database_path.exists()
        runner(ReviewTask("replay", snap, threading.Event()))
        assert len(transport.requests) == 2
    finally:
        assert runner.close(3)
        store.close()


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "mismatch", "tool_failed"])
def test_review_failure_cancellation_and_history_mismatch_do_not_advance(tmp_path, config_factory, outcome):
    config = config_factory()
    store = SqliteMemoryStore(tmp_path / "memory.db")
    messages = (Message.user("one"), Message.assistant_text("ok"))
    snap = snapshot(store, messages)
    transport = ScriptedTransport(RuntimeError("offline failure"))
    if outcome == "tool_failed":
        transport = ScriptedTransport(
            NormalizedModelResponse(tool_calls=[ToolCall(id="bad", name="memory", arguments='{"action":"replace","id":"x","expected_version":1,"content":"x"}')]),
            NormalizedModelResponse(content="done despite denied edit"),
        )
    resolver, modes, _ = scripted_provider_stack(config, {"primary":transport})
    runner = MemoryReviewRunner(config, store, store)
    runner.resolver.close(); runner.resolver, runner.api_modes = resolver, modes
    cancel = threading.Event()
    if outcome == "cancelled":
        cancel.set()
    if outcome == "mismatch":
        store.mark_reviewed("s", 1, "wrong fingerprint")
    before = store.review_state("s").reviewed_position
    try:
        with pytest.raises((RuntimeError, TurnCancelled, MemoryError)):
            runner(ReviewTask("review", snap, cancel))
        assert store.review_state("s").reviewed_position == before
    finally:
        assert runner.close(3)
        store.close()


def test_operation_preserves_callback_timeout(tmp_path, config_factory):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    runner = MemoryReviewRunner(config_factory(), store, store)
    error = TimeoutError("backend request timed out")
    def fail():
        raise error
    try:
        with pytest.raises(TimeoutError) as raised:
            runner.call_operation(fail, timeout=0.5, cancel_event=None, deadline=None)
        assert raised.value is error
    finally:
        assert runner.close(3)
        store.close()


def test_tool_and_http_use_injected_adapter_with_opaque_ids(context_factory):
    from dataclasses import replace
    from gateway.web.host import WebHost
    from memory.store import MemoryEntry, MemoryPage, MemoryWriteResult
    class Adapter:
        def __init__(self):
            self.entry = MemoryEntry("remote:550e8400-e29b-41d4-a716-446655440000", "user", "", "tea", 1, {}, None, 1, 1)
            self.calls = []
        def add(self, draft):
            self.calls.append("add"); return MemoryWriteResult(True, self.entry)
        def list(self, query):
            self.calls.append("list"); return MemoryPage((self.entry,), 1)
        def search(self, query):
            self.calls.append("search"); return [self.entry]
        def update(self, entry_id, patch, *, expected_version):
            assert entry_id == self.entry.id and expected_version == 1
            self.calls.append("update"); self.entry = replace(self.entry, content=patch.content, version=2)
            return MemoryWriteResult(True, self.entry)
        def delete(self, entry_id, *, expected_version):
            assert entry_id == self.entry.id and expected_version == 2
            self.calls.append("delete"); return MemoryWriteResult(True, self.entry)
        def close(self):
            self.calls.append("close")
    adapter = Adapter()
    host = object.__new__(WebHost); host.memory = adapter
    with context_factory() as context:
        tool = memory_entry(adapter)
        tool.handler(MemoryArgs(action="add", kind="user", content="tea"), context)
        tool.handler(MemoryArgs(action="search", query="tea"), context)
        assert host.memory_records("user")["entries"][0]["id"] == adapter.entry.id
        host.edit_memory("user", {"action":"replace", "id":adapter.entry.id, "expected_version":1, "content":"coffee"})
        host.edit_memory("user", {"action":"remove", "id":adapter.entry.id, "expected_version":2})
    assert adapter.calls == ["add", "search", "list", "update", "delete"]
