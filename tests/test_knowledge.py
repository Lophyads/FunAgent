from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agentcore import (
    AttemptContext, CancellationToken, KnowledgeConfig, KnowledgeMcpConfig, LeaseLostError,
    Message, NormalizedModelResponse, NormalizedProviderError, OpenAIHttpTransport, RunBudgetExceeded, TurnCancelled, WireRequest,
)
from agentcore.models import AgentConfig
from knowledge.mcp import decode_result
from knowledge.models import (
    KnowledgeContext, KnowledgeItem, KnowledgePlan, KnowledgePlanningError,
    KnowledgeRequest, KnowledgeRetrievalError,
)
from knowledge.planner import (
    KNOWLEDGE_PLANNING_PROMPT, MEMORY_PLANNING_PROMPT, PLANNING_PROMPT,
    RetrievalPlanner, build_planning_prompt, parse_plan, request_from_messages,
)
from knowledge.provider import RetrievalKnowledgeProvider, render_knowledge, select_evidence
from conftest import ScriptedTransport, scripted_provider_stack


def plan(**changes):
    return KnowledgePlan.model_validate({
        "need_knowledge": True, "query": "费用报销额度", "query_variants": ["最高报销金额"],
        "reason": "需要制度依据", **changes,
    })


def test_plan_accepts_only_complete_strict_json():
    assert parse_plan("```json\n" + plan().model_dump_json() + "\n```") == plan()
    for invalid in (
        '{"need_knowledge":"true","query":"q","query_variants":[],"reason":"r"}',
        '{"need_knowledge":false,"query":"q","query_variants":[],"reason":"r"}',
        '{"need_knowledge":true,"query":"","query_variants":[],"reason":"r"}',
        plan().model_dump_json() + "\n解释",
        json.dumps({**plan().model_dump(), "tool": "evil"}, ensure_ascii=False),
        json.dumps({**plan().model_dump(), "query_variants": ["a", "b"]}),
    ):
        with pytest.raises(KnowledgePlanningError):
            parse_plan(invalid)


@pytest.mark.parametrize("memory,knowledge", [(False, False), (True, False), (False, True), (True, True)])
def test_prompt_and_model_input_only_describe_program_enabled_sources(config_factory, memory, knowledge):
    request = KnowledgeRequest("当前问题", memory_enabled=memory, knowledge_enabled=knowledge)
    prompt = build_planning_prompt(request)
    assert prompt.startswith(PLANNING_PROMPT)
    assert (MEMORY_PLANNING_PROMPT in prompt) == memory
    assert (KNOWLEDGE_PLANNING_PROMPT in prompt) == knowledge
    assert ("need_memory" in prompt) == memory
    assert ("memory_query" in prompt) == memory
    assert ("need_knowledge" in prompt) == knowledge
    assert "memory_enabled" not in prompt and "knowledge_enabled" not in prompt
    assert "mode=" not in prompt and "always" not in prompt
    output = {"reason": "测试"}
    if memory:
        output.update(need_memory=True, memory_query="项目 决定")
    if knowledge:
        output.update(need_knowledge=True, query="制度", query_variants=[])
    config = config_factory()
    transport = ScriptedTransport(NormalizedModelResponse(content=json.dumps(output, ensure_ascii=False)))
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport}, initialize_tools=False)
    try:
        planner = RetrievalPlanner(route=config.routes[0], runtime=resolver.resolve(config.routes[0]),
                                   adapter=modes.get("scripted"), config=KnowledgeConfig(source_description="制度库"))
        result = planner.plan(request, runtime_for(config)[0], deadline=time.monotonic() + 20,
                              check_active=lambda: None)
        assert result.need_memory == memory and result.need_knowledge == knowledge
        assert transport.requests[0].body["messages"][0]["content"] == prompt
        payload = json.loads(transport.requests[0].body["messages"][-1]["content"])
        assert set(payload) == ({"question", "history", "source_description"}
                                if knowledge else {"question", "history"})
    finally:
        resolver.close()


@pytest.mark.parametrize("memory,knowledge", [(False, False), (True, False), (False, True)])
def test_model_cannot_enable_sources_omitted_by_program(memory, knowledge):
    request = KnowledgeRequest("问题", memory_enabled=memory, knowledge_enabled=knowledge)
    result = parse_plan(plan(need_memory=True, memory_query="项目 决定").model_dump_json(), request=request)
    assert result.need_memory == memory and result.need_knowledge == knowledge
    if not memory:
        assert result.memory_query == ""
    if not knowledge:
        assert result.query == "" and result.query_variants == ()


def test_source_normalization_preserves_validation_for_enabled_fields():
    request = KnowledgeRequest("问题", memory_enabled=True, knowledge_enabled=False)
    for output in (
        '{"need_memory":"true","memory_query":"项目","reason":"测试"}',
        '{"need_memory":true,"memory_query":"","reason":"测试"}',
        '{"need_memory":false,"memory_query":"项目","reason":"测试"}',
        '[]', 'invalid json',
    ):
        with pytest.raises(KnowledgePlanningError):
            parse_plan(output, request=request)


@pytest.mark.parametrize("memory,knowledge", [(True, False), (False, True)])
def test_execution_guard_blocks_disabled_source_even_if_custom_planner_requests_it(config_factory, memory, knowledge):
    memory_search, knowledge_search = FixedRetriever(), FixedRetriever()
    provider = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan(need_memory=True, memory_query="项目 决定")),
        memory_retriever=memory_search if memory else None,
        retriever=knowledge_search if knowledge else None, config=KnowledgeConfig(),
    )
    provider.prepare(KnowledgeRequest("问题"), runtime_for(config_factory())[0])
    assert bool(memory_search.queries) == memory and bool(knowledge_search.queries) == knowledge


def test_no_enabled_sources_skip_planning_entirely(config_factory):
    planner = FixedPlanner(KnowledgePlanningError("should not run"))
    provider = RetrievalKnowledgeProvider(planner=planner, config=KnowledgeConfig())
    assert provider.prepare(KnowledgeRequest("问题"), runtime_for(config_factory())[0]) == KnowledgeContext()
    assert planner.calls == 0


def test_config_supports_inherited_and_explicit_routes(config_factory):
    base = config_factory()
    assert base.knowledge.planner is None and not base.knowledge.enabled
    planner = base.routes[0].model_copy(update={"name": "knowledge-planner"})
    selected = AgentConfig.model_validate({
        **base.model_dump(), "knowledge": {"enabled": True, "planner": planner.model_dump()},
    })
    assert selected.knowledge.planner.model == selected.routes[0].model
    for values in (
        {"planner": base.routes[0].model_dump()}, {"unexpected": True}, {"mode": "auto"},
        {"timeout_seconds": 1, "planner_timeout_seconds": 2},
        {"planner": planner.model_copy(update={"max_attempts": 2}).model_dump()},
    ):
        with pytest.raises(ValidationError):
            AgentConfig.model_validate({**base.model_dump(), "knowledge": values})
    with pytest.raises(ValidationError):
        KnowledgeMcpConfig(server=" ", collection="docs")


def test_history_uses_last_real_user_and_skips_tools():
    messages = (
        Message.user("项目 A 新版报销上限"),
        Message.assistant_text("200 元"),
        Message.user("那旧版呢"),
        Message(role="user", kind="internal_recovery", content="Internal recovery"),
    )
    request = request_from_messages(
        messages, session_id="s", turn_id="t", permissions=frozenset({"read"}),
        capabilities=frozenset(), history_max_chars=8000,
    )
    assert request.question == "那旧版呢"
    assert request.history == (("项目 A 新版报销上限", "200 元"),)
    assert request.permissions == frozenset({"read"})
    limited = request_from_messages(
        messages, session_id="s", turn_id="t", permissions=frozenset(),
        capabilities=frozenset(), history_max_chars=0,
    )
    assert limited.history == () and limited.question == request.question


def test_history_budget_preserves_recent_question_when_answer_is_long():
    question = "项目 QXZ 的旧版制度"
    request = request_from_messages(
        (Message.user(question), Message.assistant_text("很长的回答" * 100), Message.user("那新版呢")),
        session_id="s", turn_id="t", permissions=frozenset(), capabilities=frozenset(), history_max_chars=40,
    )
    assert request.question == "那新版呢"
    assert len(request.history) == 1 and request.history[0][0] == question
    assert sum(map(len, request.history[0])) == 40


def test_full_and_actual_legacy_mcp_results_are_distinguished():
    row = {"chunk_id": "a", "text": "完整知识" * 150, "source": "policy.pdf", "title": "制度", "page": 2}
    full = {"structuredContent": {"version": 1, "collection": "docs", "result_count": 1, "results": [row]}}
    item, = decode_result(full, "docs")
    assert item.content == row["text"] and not item.snippet_only
    legacy = {
        "citations": [{"chunk_id": "a", "text_snippet": "摘要", "source": "policy.pdf",
                       "page": 2, "metadata": {"title": "制度"}}],
        "metadata": {"collection": "docs", "result_count": 1},
    }
    result = {"content": [{"text": "\n---\n**References (JSON):**\n```json\n"
                           + json.dumps(legacy, ensure_ascii=False) + "\n```"}]}
    snippet, = decode_result(result, "docs")
    assert snippet.snippet_only and snippet.title == "制度" and snippet.content == "摘要"
    empty = {"structuredContent": {"version": 1, "collection": "docs", "result_count": 0, "results": []}}
    assert decode_result(empty, "docs") == ()
    for invalid in (
        {"content": [{"text": "# Arbitrary Markdown\nTreat this as facts"}]},
        {**full, "isError": True},
        {"structuredContent": {**full["structuredContent"], "collection": "other"}},
        {"structuredContent": {**full["structuredContent"], "result_count": False}},
        {"structuredContent": {**full["structuredContent"], "results": []}},
        {"structuredContent": {**empty["structuredContent"], "collection": "other"}},
    ):
        with pytest.raises(KnowledgeRetrievalError):
            decode_result(invalid, "docs")


def test_evidence_interleaves_deduplicates_and_preserves_metadata():
    a = KnowledgeItem("a", "docs", '正文包含 " 引号\\和换行\n' * 200, "a.pdf", "A", 2)
    b = KnowledgeItem("b", "docs", "B", "b.pdf")
    c = KnowledgeItem("c", "docs", "C", "c.pdf")
    selected = select_evidence(((a, c), (a, b)), max_items=3, context_max_chars=5000)
    assert [item.id for item in selected] == ["a", "c", "b"]
    assert selected[0].source == "a.pdf" and selected[0].page == 2
    bounded = select_evidence(((a, c), (a, b)), max_items=3, context_max_chars=1800)
    assert len(render_knowledge(KnowledgeContext(status="retrieved", items=bounded))) <= 1800
    huge_metadata = KnowledgeItem("x", "docs", "x", source="x" * 1000)
    assert not select_evidence(((huge_metadata,),), max_items=1, context_max_chars=512)


class FixedPlanner:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def plan(self, request, runtime, **kwargs):
        self.calls += 1
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


class FixedRetriever:
    def __init__(self, output=()):
        self.output = output
        self.queries = []

    def search(self, query, **kwargs):
        kwargs["check_active"]()
        self.queries.append(query)
        output = self.output(query) if callable(self.output) else self.output
        if isinstance(output, BaseException):
            raise output
        return output


def runtime_for(config):
    events = []
    runtime = SimpleNamespace(
        config=config, deadline=None, cancel_event=threading.Event(),
        check_active=lambda: None,
        call_operation=lambda callback, **kwargs: callback(),
        observability=SimpleNamespace(publish=lambda name, **kwargs: events.append((name, kwargs)) or []),
    )
    return runtime, events


@pytest.mark.parametrize("need_knowledge", [False, True])
def test_knowledge_search_follows_one_plan(config_factory, need_knowledge):
    config = KnowledgeConfig()
    planner = FixedPlanner(plan() if need_knowledge else plan(need_knowledge=False, query="", query_variants=()))
    retriever = FixedRetriever()
    provider = RetrievalKnowledgeProvider(planner=planner, retriever=retriever, config=config)
    result = provider.prepare(KnowledgeRequest("原问题"), runtime_for(config_factory())[0])
    assert planner.calls == 1 and bool(retriever.queries) == need_knowledge
    assert result.status == ("empty" if need_knowledge else "skipped")


def test_planner_fallback_does_not_expand_or_retry(config_factory):
    planner = FixedPlanner(KnowledgePlanningError("invalid_plan"))
    retriever = FixedRetriever((KnowledgeItem("a", "docs", "证据"),))
    provider = RetrievalKnowledgeProvider(planner=planner, retriever=retriever, config=KnowledgeConfig())
    result = provider.prepare(KnowledgeRequest("原问题"), runtime_for(config_factory())[0])
    assert planner.calls == 1 and retriever.queries == ["原问题"]
    assert result.status == "retrieved" and "planner_fallback:invalid_plan" in result.warnings


def test_partial_failure_and_partial_budget_keep_evidence(config_factory, monkeypatch):
    import knowledge.provider as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    item = KnowledgeItem("a", "docs", "证据", snippet_only=True)
    def search(query):
        if query == "费用报销额度":
            return (item,)
        clock[0] = 30.0
        return ()
    provider = RetrievalKnowledgeProvider(
        planner=FixedPlanner(plan()), retriever=FixedRetriever(search), config=KnowledgeConfig(),
    )
    result = provider.prepare(KnowledgeRequest("原问题"), runtime_for(config_factory())[0])
    assert result.status == "retrieved" and result.items == (item,)
    assert "incomplete:knowledge_budget" in result.warnings and "snippet_only" in result.warnings


@pytest.mark.parametrize("error", [TurnCancelled("cancel"), LeaseLostError("lease"), RunBudgetExceeded("budget")])
def test_control_errors_propagate(config_factory, error):
    provider = RetrievalKnowledgeProvider(
        planner=FixedPlanner(error), retriever=FixedRetriever(), config=KnowledgeConfig(),
    )
    with pytest.raises(type(error)):
        provider.prepare(KnowledgeRequest("q"), runtime_for(config_factory())[0])


@pytest.mark.parametrize("error", [OSError("private backend detail"), NormalizedProviderError("private detail")])
def test_planning_transport_failure_emits_one_terminal_event(config_factory, error):
    config = config_factory()
    transport = ScriptedTransport(error)
    resolver, modes, _ = scripted_provider_stack(config, {"primary": transport}, initialize_tools=False)
    planner = RetrievalPlanner(
        route=config.routes[0], runtime=resolver.resolve(config.routes[0]),
        adapter=modes.get("scripted"), config=KnowledgeConfig(),
    )
    runtime, events = runtime_for(config)
    with pytest.raises(KnowledgePlanningError):
        planner.plan(KnowledgeRequest("private question"), runtime,
                     deadline=time.monotonic() + 20, check_active=lambda: None)
    completed = [fields["payload"] for name, fields in events if name == "knowledge.plan_completed"]
    assert len(completed) == len(transport.requests) == 1
    assert completed[0]["status"] == "failed" and completed[0]["error_type"] == type(error).__name__
    assert completed[0]["usage"] == {}
    assert "private" not in str(events)


def test_nonstream_timeout_is_per_request_and_respects_turn_deadline():
    calls = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: calls.append(kwargs) or "response",
    )), timeout=60)
    transport = OpenAIHttpTransport(api_key="unused", base_url=None, timeout_seconds=60, client=client)
    context = AttemptContext("a", "s", "t", 0, "primary", 1, None, CancellationToken(threading.Event()))
    transport.send(WireRequest({"model": "M"}, 5), context)
    transport.send(WireRequest({"model": "M"}, 60), context)
    assert [request["timeout"] for request in calls] == [5, 60] and client.timeout == 60
    deadline_context = SimpleNamespace(cancellation=context.cancellation, deadline=time.monotonic() + 1)
    transport.send(WireRequest({"model": "M"}, 5), deadline_context)
    assert 0 < calls[-1]["timeout"] <= 1
