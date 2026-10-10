from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace
from typing import Literal

import pytest

from agentcore import (
    KnowledgeConfig, LeaseLostError, Message, NormalizedModelResponse, NormalizedProviderError,
    RetryPolicy, RunBudgetExceeded, SessionDefaults, SessionRuntimeSnapshot, SystemPromptSnapshot,
    ToolCall, TurnResult, TurnSnapshot,
)
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
from pydantic import ValidationError
from knowledge.models import KnowledgeContext, KnowledgeItem, KnowledgePlan, KnowledgePlanningError, KnowledgeRequest, KnowledgeRetrievalError
from knowledge.provider import RetrievalKnowledgeProvider, render_memory, with_retrieved_context
from memory.retrieval import MemoryKnowledgeRetriever
from test_knowledge import FixedPlanner, FixedRetriever, runtime_for
import sqlite3


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
    full = MemoryPromptBuilder(StaticSystemPromptBuilder("System"), store, 8000).build(PromptBuildRequest(()))
    assert "prefers tea" in full.text and "project knowledge" not in full.text
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
        NormalizedModelResponse(content="saved"), NormalizedModelResponse(content="old"), NormalizedModelResponse(content="new"),
    )
    agent = agent_factory(config, transports={"primary":transport}, tool_registry=registry, prompt_builder=feature.prompt_builder)
    try:
        assert agent.run_turn_blocking("remember", session_id="first").status == "completed"
        feature.store.add(MemoryDraft("user", "prefers tea"))
        assert feature.store.search(MemorySearch("tea"))
        assert agent.run_turn_blocking("again", session_id="first").status == "completed"
        assert agent.run_turn_blocking("hello", session_id="second").status == "completed"
        prompts = [request.body["messages"][0]["content"] for request in transport.requests]
        assert "prefers tea" not in prompts[0]
        assert prompts[0] == prompts[1] and "prefers tea" in prompts[2]
        assert all("memory" not in [tool["function"]["name"] for tool in request.body["tools"]]
                   for request in transport.requests)
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
    store.add(MemoryDraft("note", "existing project fact"))
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
        assert "existing project fact" in body["messages"][-1]["content"]
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


# Turn-local factual recall and request/history isolation.
def plan(memory=False, knowledge=False):
    return KnowledgePlan(
        need_memory=memory, memory_query="缓存 迁移" if memory else "",
        need_knowledge=knowledge, query="报销额度" if knowledge else "",
        query_variants=(), reason="需要依据" if memory or knowledge else "闲聊",
    )


def planned(memory=False, knowledge=False):
    return NormalizedModelResponse(content=plan(memory, knowledge).model_dump_json())


MEMORY_ITEM = KnowledgeItem("m1", "memory:note", "缓存迁移结论：暂时保留 Redis。")
KNOWLEDGE_ITEM = KnowledgeItem("k1", "docs", "报销上限 200 元。")


@pytest.mark.parametrize("memory,knowledge", [(False, False), (True, False), (False, True), (True, True)])
def test_one_plan_independently_gates_both_sources(config_factory, memory, knowledge):
    planner = FixedPlanner(plan(memory, knowledge))
    memory_search, knowledge_search = FixedRetriever((MEMORY_ITEM,)), FixedRetriever((KNOWLEDGE_ITEM,))
    result = RetrievalKnowledgeProvider(
        planner=planner, memory_retriever=memory_search, retriever=knowledge_search, config=KnowledgeConfig(),
    ).prepare(KnowledgeRequest("原问题"), runtime_for(config_factory())[0])
    assert planner.calls == 1
    assert memory_search.queries == (["缓存 迁移"] if memory else [])
    assert knowledge_search.queries == (["报销额度", "原问题"] if knowledge else [])
    assert result.memory_status == ("retrieved" if memory else "skipped")
    assert result.status == ("retrieved" if knowledge else "skipped")


def test_skipped_memory_and_zero_budget_disable_search(config_factory):
    memory_search = FixedRetriever((MEMORY_ITEM,))
    provider = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan()), memory_retriever=memory_search, config=KnowledgeConfig(),
    )
    assert provider.prepare(KnowledgeRequest("你好"), runtime_for(config_factory())[0]) == KnowledgeContext(plan=plan())
    assert not memory_search.queries
    provider.planner = FixedPlanner(plan(True))
    config = config_factory().model_copy(update={"memory_recall_max_chars": 0})
    assert provider.prepare(KnowledgeRequest("q"), runtime_for(config)[0]) == KnowledgeContext()
    assert provider.planner.calls == 0


@pytest.mark.parametrize("failed_source", ["memory", "knowledge"])
def test_source_failure_does_not_discard_other_source(config_factory, failed_source):
    memory_search = FixedRetriever(KnowledgeRetrievalError("backend") if failed_source == "memory" else (MEMORY_ITEM,))
    knowledge_search = FixedRetriever(KnowledgeRetrievalError("backend") if failed_source == "knowledge" else (KNOWLEDGE_ITEM,))
    result = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan(True, True)), memory_retriever=memory_search,
        retriever=knowledge_search, config=KnowledgeConfig(),
    ).prepare(KnowledgeRequest("q"), runtime_for(config_factory())[0])
    assert result.memory_items == (() if failed_source == "memory" else (MEMORY_ITEM,))
    assert result.items == (() if failed_source == "knowledge" else (KNOWLEDGE_ITEM,))
    assert result.memory_status == ("failed" if failed_source == "memory" else "retrieved")
    assert result.status == ("failed" if failed_source == "knowledge" else "retrieved")


def test_planning_failure_queries_each_enabled_source_once(config_factory):
    memory_search, knowledge_search = FixedRetriever(), FixedRetriever()
    planner = FixedPlanner(KnowledgePlanningError("invalid_plan"))
    result = RetrievalKnowledgeProvider(
        planner=planner, memory_retriever=memory_search, retriever=knowledge_search, config=KnowledgeConfig(),
    ).prepare(KnowledgeRequest("原问题"), runtime_for(config_factory())[0])
    assert planner.calls == 1 and memory_search.queries == knowledge_search.queries == ["原问题"]
    assert result.memory_status == result.status == "empty"


def test_memory_timeout_is_a_source_failure_and_preserves_knowledge(config_factory, monkeypatch):
    import knowledge.provider as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    config = config_factory()
    def slow_memory(query):
        clock[0] = config.timeouts.memory_seconds + 0.1
        return (MEMORY_ITEM,)
    result = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan(True, True)), memory_retriever=FixedRetriever(slow_memory),
        retriever=FixedRetriever((KNOWLEDGE_ITEM,)), config=KnowledgeConfig(),
    ).prepare(KnowledgeRequest("q"), runtime_for(config)[0])
    assert result.memory_status == "failed" and result.status == "retrieved"


@pytest.mark.parametrize("error", [TurnCancelled("cancel"), LeaseLostError("lease"), RunBudgetExceeded("budget")])
def test_memory_search_propagates_control_interruptions(config_factory, error):
    provider = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan(True)), memory_retriever=FixedRetriever(error), config=KnowledgeConfig(),
    )
    with pytest.raises(type(error)):
        provider.prepare(KnowledgeRequest("q"), runtime_for(config_factory())[0])


def test_memory_candidates_are_deduplicated_and_bounded_independently(config_factory):
    config = config_factory().model_copy(update={"memory_recall_top_k": 2, "memory_recall_max_chars": 650})
    items = tuple(KnowledgeItem(f"m{i}", "memory:note", "正文含引号\"和换行\n" * 500) for i in range(3))
    result = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan(True, True)), memory_retriever=FixedRetriever((items[0], *items)),
        retriever=FixedRetriever((KNOWLEDGE_ITEM,)), config=KnowledgeConfig(),
    ).prepare(KnowledgeRequest("q"), runtime_for(config)[0])
    assert 0 < len(result.memory_items) <= 2
    assert len({item.id for item in result.memory_items}) == len(result.memory_items)
    assert len(render_memory(result)) <= 650 and "片段已截断" in render_memory(result)
    assert result.items == (KNOWLEDGE_ITEM,)


def test_request_copy_preserves_api_text_and_skips_internal_recovery():
    original = Message(role="user", content="显示的原文", api_content="原始 API 文本")
    internal = Message(role="user", kind="internal_recovery", content="恢复执行")
    history = (Message.user("上轮问题"), Message.assistant_text("上轮回复"), original, internal)
    before = [message.model_dump() for message in history]
    enriched = with_retrieved_context(history, KnowledgeContext(
        status="retrieved", items=(KNOWLEDGE_ITEM,), memory_status="retrieved", memory_items=(MEMORY_ITEM,),
    ))
    assert enriched[2] is not original and enriched[2].content == original.content
    assert enriched[2].api_content.startswith("原始 API 文本\n\n<retrieved_memory>")
    assert "<retrieved_knowledge>" in enriched[2].api_content
    assert enriched[0] is history[0] and enriched[-1] is internal
    assert [message.model_dump() for message in history] == before


@pytest.mark.parametrize("status", ["empty", "failed", "retrieved"])
def test_tiny_memory_budget_keeps_request_within_budget(status):
    history = (Message.user("问题"),)
    context = KnowledgeContext(memory_status=status, memory_items=(MEMORY_ITEM,) if status == "retrieved" else ())
    assert with_retrieved_context(history, context, memory_max_chars=1) == history


@pytest.mark.parametrize("changes", [{"need_memory": True}, {"memory_query": "孤立查询"}])
def test_memory_plan_requires_consistent_boolean_and_query(changes):
    with pytest.raises(ValidationError):
        KnowledgePlan.model_validate({**plan().model_dump(), **changes})


def test_sqlite_keywords_interleave_note_and_episode_and_exclude_user(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    try:
        store.add(MemoryDraft("user", "用户负责缓存迁移。"))
        note = store.add(MemoryDraft("note", "缓存迁移采用双写方案。", "项目甲")).entry
        older = store.add(MemoryDraft("episode", "缓存迁移提出保留 Redis。", event_at="2026-10-05")).entry
        newer = store.add(MemoryDraft("episode", "缓存迁移确认保留 Redis。", event_at="2026-10-06")).entry
        values = MemoryKnowledgeRetriever(store).search("缓存 迁移", request=KnowledgeRequest("q"),
                                                       top_k=3, runtime=None, check_active=lambda: None)
        assert [item.id for item in values] == [note.id, newer.id, older.id]
        assert all(item.collection != "memory:user" for item in values)
        assert "v1" in values[0].content and "2026-10-06" in values[1].content
    finally:
        store.close()


def test_adapter_limits_each_kind_and_deduplicates_ids():
    entry = SimpleNamespace(id="same", kind="note", content="fact", version=3, event_at=None, source={}, subject="")
    episode = SimpleNamespace(**{**vars(entry), "id": "episode", "kind": "episode"})
    queries = []
    def search(query):
        queries.append(query)
        return [entry, entry] if query.kind == "note" else [entry, episode]
    values = MemoryKnowledgeRetriever(SimpleNamespace(search=search)).search(
        "关键词", request=KnowledgeRequest("q"), top_k=2, runtime=None, check_active=lambda: None,
    )
    assert [(q.kind, q.limit) for q in queries] == [("note", 2), ("episode", 2)]
    assert [item.id for item in values] == ["same", "episode"]


def test_memory_only_turn_reuses_recall_in_retries_and_tool_loop(config_factory, agent_factory):
    config = config_factory(retry=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0,
                                            jitter_ratio=0, max_retry_after_seconds=0))
    config = config.model_copy(update={"session_defaults": SessionDefaults(enabled_tools=("memory", "current_time"))})
    memory_search = FixedRetriever((MEMORY_ITEM,))
    registry = builtin_registry()
    # A registered memory handler must remain unreachable even after explicit enabling.
    store = SqliteMemoryStore(config.database_path.parent / "memory.db")
    registry.replace_all((*registry.entries(), memory_entry(store)))
    snapshots = []
    transport = ScriptedTransport(
        planned(True), NormalizedProviderError("retry", retryable=True, allow_fallback=True),
        NormalizedModelResponse(tool_calls=[ToolCall(id="clock", name="current_time", arguments="{}")]),
        NormalizedModelResponse(content="正常回复"), planned(), NormalizedModelResponse(content="你好"),
    )
    try:
        with agent_factory(config, transports={"primary": transport}, tool_registry=registry,
                           memory_retriever=memory_search,
                           review_scheduler=SimpleNamespace(schedule=snapshots.append)) as agent:
            assert agent.run_turn_blocking("缓存如何迁移", session_id="s").status == "completed"
            assert agent.run_turn_blocking("你好", session_id="s").status == "completed"
        assert memory_search.queries == ["缓存 迁移"]
        payload = json.loads(transport.requests[0].body["messages"][-1]["content"])
        assert set(payload) == {"question", "history"}
        prompt = transport.requests[0].body["messages"][0]["content"]
        assert "need_memory" in prompt and "need_knowledge" not in prompt
        recalled = [r.body["messages"][1]["content"] for r in transport.requests[1:4]]
        assert recalled[0] == recalled[1] == recalled[2] and MEMORY_ITEM.content in recalled[0]
        assert transport.requests[-1].body["messages"][1]["content"] == "缓存如何迁移"
        assert "<retrieved_memory>" not in str(transport.requests[-1].body["messages"])
        for request in transport.requests[1:4]:
            assert "memory" not in [tool["function"]["name"] for tool in request.body["tools"]]
            assert request.body["messages"][0]["content"] == config.system_prompt
        assert snapshots and "<retrieved_memory>" not in str(snapshots[0].messages)
        with sqlite3.connect(config.database_path) as db:
            assert all("<retrieved_memory>" not in str(row) for row in db.execute("SELECT content,api_content FROM messages"))
    finally:
        store.close()


def test_guessed_memory_call_cannot_execute(config_factory, agent_factory):
    config = config_factory().model_copy(update={"session_defaults": SessionDefaults(enabled_tools=("memory",))})
    store = SqliteMemoryStore(config.database_path.parent / "memory.db")
    registry = builtin_registry()
    registry.replace_all((*registry.entries(), memory_entry(store)))
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(id="guess", name="memory", arguments='{"action":"add","kind":"user","content":"bad"}')]),
        NormalizedModelResponse(content="完成"),
    )
    try:
        with agent_factory(config, transports={"primary": transport}, tool_registry=registry) as agent:
            result = agent.run_turn_blocking("你好")
        assert result.status == "completed" and "unknown_tool" in result.model_dump_json()
        assert store.list().total == 0
        assert all("memory" not in [tool["function"]["name"] for tool in r.body["tools"]]
                   for r in transport.requests)
    finally:
        store.close()


def test_cancel_blocked_memory_operation_never_starts_main_model(config_factory, agent_factory):
    entered, release = threading.Event(), threading.Event()
    class BlockedRetriever:
        def search(self, query, **kwargs):
            entered.set()
            release.wait(3)
            kwargs["check_active"]()
            return (MEMORY_ITEM,)
    transport = ScriptedTransport(planned(True))
    with agent_factory(config_factory(), transports={"primary": transport}, memory_retriever=BlockedRetriever()) as agent:
        submission = agent.submit_turn("缓存迁移")
        try:
            assert entered.wait(2)
            submission.cancel()
            assert submission.future.result(timeout=2).status == "interrupted"
        finally:
            release.set()
    assert len(transport.requests) == 1


def test_restart_loads_raw_history_frozen_profile_and_fresh_facts(tmp_path, config_factory, agent_factory):
    config = config_factory()
    store = SqliteMemoryStore(tmp_path / "memory.db")
    try:
        profile = store.add(MemoryDraft("user", "用户偏好中文。" )).entry
        fact = store.add(MemoryDraft("note", "缓存迁移旧结论。" )).entry
        builder = MemoryPromptBuilder(StaticSystemPromptBuilder(config.system_prompt), store, 4000)
        first = ScriptedTransport(planned(True), NormalizedModelResponse(content="回答"))
        with agent_factory(config, transports={"primary": first}, prompt_builder=builder,
                           memory_retriever=MemoryKnowledgeRetriever(store)) as agent:
            assert agent.run_turn_blocking("缓存迁移", session_id="s").status == "completed"
        store.update(fact.id, MemoryPatch("缓存迁移新结论。"), expected_version=1)
        store.update(profile.id, MemoryPatch("用户偏好英文。"), expected_version=1)
        second = ScriptedTransport(planned(True), NormalizedModelResponse(content="回答"))
        with agent_factory(config, transports={"primary": second}, prompt_builder=builder,
                           memory_retriever=MemoryKnowledgeRetriever(store)) as agent:
            assert agent.run_turn_blocking("缓存迁移", session_id="s").status == "completed"
        body = second.requests[1].body
        assert body["messages"][0] == first.requests[1].body["messages"][0]
        assert "用户偏好中文。" in body["messages"][0]["content"]
        assert body["messages"][1] == {"role": "user", "content": "缓存迁移"}
        assert "缓存迁移新结论。" in body["messages"][-1]["content"]
        assert "缓存迁移旧结论。" not in str(body["messages"])
    finally:
        store.close()


def test_recovery_retrieves_from_latest_real_question(config_factory, agent_factory):
    from agentcore import SQLiteStore
    from test_gateway_recovery_delivery import RecordingAdapter, _gateway, _expire_lease
    config = config_factory()
    first = ScriptedTransport(planned(True), NormalizedModelResponse(content="回答"))
    memory_search = FixedRetriever((MEMORY_ITEM,))
    with agent_factory(config, transports={"primary": first}, memory_retriever=memory_search) as agent:
        agent.run_turn_blocking("缓存迁移", session_id="recovery")
    store = SQLiteStore(config.database_path)
    store.open()
    lease = store.acquire_lease(session_id="recovery", owner_id="crashed", turn_id="crash", ttl_seconds=30)
    store.append_user_message("recovery", Message.user("最新缓存迁移决定"), fence=lease.fence)
    store.close()
    _expire_lease(config.database_path, "recovery")
    memory_search.output = (KnowledgeItem("new", "memory:episode", "本次重新检索的资料。"),)
    recovered = ScriptedTransport(planned(True), NormalizedModelResponse(content="恢复完成"))
    with _gateway(config, RecordingAdapter(), recovered, memory_retriever=memory_search) as gateway:
        assert gateway.attach_session("recovery").future.result(timeout=3).status == "completed"
        messages = gateway.agent._store.load_messages("recovery")
    assert json.loads(recovered.requests[0].body["messages"][-1]["content"])["question"] == "最新缓存迁移决定"
    assert len(memory_search.queries) == 2
    assert "本次重新检索的资料。" in recovered.requests[1].body["messages"][-1]["content"]
    assert MEMORY_ITEM.content not in str(recovered.requests[1].body["messages"])
    assert "<retrieved_memory>" not in str([m.model_dump() for m in messages])
