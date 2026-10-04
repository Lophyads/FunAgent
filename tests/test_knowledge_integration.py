from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from types import MappingProxyType

import pytest

from agentcore import (
    AgentCore, GuardDecision, KnowledgeConfig, KnowledgeMcpConfig, Message, NormalizedModelResponse,
    SessionDefaults, StaticProviderResolver, ToolCall, SessionBuildRequest, UserInput,
)
from agentcore.conversation_loop import ConversationLoop
from conftest import ScriptedTransport, configure_tools, scripted_provider_stack
from tools.config import McpServerConfig, ToolServiceConfig
from tools.services import ToolServices
from knowledge.models import KnowledgeContext, KnowledgeItem


def planning_response(need=True):
    return NormalizedModelResponse(content=json.dumps({
        "need_knowledge": need, "query": "项目 A 报销额度" if need else "",
        "query_variants": ["项目 A 最高报销金额"] if need else [],
        "reason": "需要内部资料" if need else "无需检索",
    }, ensure_ascii=False), usage={"total_tokens": 7})


class FakeMcp:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.results = [{"chunk_id": "policy-1", "text": "项目 A 报销上限为 200 元。",
                         "source": "policy.pdf", "title": "报销制度", "page": 3}]

    def discover(self):
        from mcp.types import Tool
        schema = {"type": "object", "properties": {
            "query": {"type": "string"}, "top_k": {"type": "integer"},
            "collection": {"type": "string"},
        }, "required": ["query", "top_k", "collection"], "additionalProperties": False}
        return {"rag": (
            Tool(name="query_knowledge_hub", inputSchema=schema),
            Tool(name="list_collections", inputSchema={"type": "object"}),
            Tool(name="get_document_summary", inputSchema={"type": "object"}),
            Tool(name="echo", inputSchema={"type": "object", "properties": {"text": {"type": "string"}}}),
        )}

    def is_available(self, server):
        return not self.closed

    def call_tool(self, server, tool, arguments, *, check_active):
        from mcp.types import CallToolResult, TextContent
        check_active()
        self.calls.append((server, tool, arguments))
        if tool == "echo":
            return CallToolResult(content=[TextContent(type="text", text="echoed")])
        return CallToolResult(content=[], structuredContent={
            "version": 1, "collection": "docs", "result_count": len(self.results), "results": self.results,
        })

    def close(self, *, deadline):
        self.closed = True

    def close_session(self, key, *, deadline):
        pass


@pytest.fixture
def knowledge_stack(config_factory, monkeypatch):
    pytest.importorskip("mcp")
    pytest.importorskip("jsonschema")
    client = FakeMcp()
    monkeypatch.setattr(ToolServices, "_new_mcp", lambda self: client)
    config = config_factory().model_copy(update={
        "session_defaults": SessionDefaults(enabled_toolsets=("default", "mcp")),
        "knowledge": KnowledgeConfig(
            enabled=True, source_description="项目内部制度",
            mcp=KnowledgeMcpConfig(server="rag", collection="docs"),
        ),
        "tools": ToolServiceConfig(mcp_servers={
            "rag": McpServerConfig(command="unused", requires_approval=False),
        }),
    })
    return config, client


def test_same_model_plans_once_and_keeps_evidence_out_of_history(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    transport = ScriptedTransport(
        planning_response(),
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="echo-1", name="mcp__rag__echo", arguments='{"text":"hi"}',
        )], finish_reason="tool_calls"),
        NormalizedModelResponse(content="上限为 200 元 [K1]"),
        planning_response(False), NormalizedModelResponse(content="你好"),
    )
    events = []
    class Sink:
        def emit(self, event):
            events.append(event)
        def close(self):
            pass
    with agent_factory(config, transports={"primary": transport}, event_sinks=(Sink(),)) as agent:
        first = agent.run_turn_blocking("项目 A 报销上限是多少", session_id="s")
        second = agent.run_turn_blocking("你好", session_id="s")
    assert first.status == second.status == "completed"
    assert first.provider_attempts == 2 and first.usage == {}
    assert transport.requests[0].timeout_seconds == 5
    assert transport.requests[1].timeout_seconds == config.routes[0].timeout_seconds
    assert transport.requests[0].body["tools"] == []
    for request in transport.requests[1:3]:
        names = {tool["function"]["name"] for tool in request.body["tools"]}
        assert "mcp__rag__echo" in names and "mcp__rag__query_knowledge_hub" not in names
        assert "200 元" in request.body["messages"][0]["content"]
    assert transport.requests[1].body["messages"][0] == transport.requests[2].body["messages"][0]
    assert transport.requests[4].body["messages"][0]["content"] == config.system_prompt
    assert [call[1] for call in client.calls].count("query_knowledge_hub") == 3
    assert transport.requests[3].body["messages"][0]["content"] != config.system_prompt
    with sqlite3.connect(config.database_path) as db:
        prompt, = db.execute("SELECT system_prompt_bytes FROM sessions WHERE id='s'").fetchone()
        assert prompt.decode() == config.system_prompt
        persisted = db.execute("SELECT content FROM messages WHERE session_id='s'").fetchall()
        assert all("need_knowledge" not in (content or "") for content, in persisted)
    plan_events = [event for event in events if event.name == "knowledge.plan_completed"]
    assert len(plan_events) == 2 and all(event.payload["role"] == "knowledge_planner" for event in plan_events)
    assert all(event.payload["usage"]["total"] == 7 for event in plan_events)
    assert not any("项目 A 报销上限是多少" in str(event.payload) for event in events
                   if event.name.startswith("knowledge."))


def test_guessed_hidden_tool_cannot_execute(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    transport = ScriptedTransport(
        planning_response(False),
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="hidden", name="mcp__rag__query_knowledge_hub", arguments='{"query":"q","top_k":5,"collection":"docs"}',
        )], finish_reason="tool_calls"),
        NormalizedModelResponse(content="done"),
    )
    with agent_factory(config, transports={"primary": transport}) as agent:
        result = agent.run_turn_blocking("hello", session_id="hidden")
    assert result.status == "completed" and not client.calls
    assert "unknown_tool" in json.dumps(result.model_dump(), ensure_ascii=False)


def test_knowledge_denies_override_explicit_enables_without_hiding_other_mcp_tools(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    hidden = {"mcp__rag__query_knowledge_hub", "mcp__rag__list_collections", "mcp__rag__get_document_summary"}
    config = config.model_copy(update={"session_defaults": SessionDefaults(
        enabled_tools=tuple(sorted(hidden | {"mcp__rag__echo"})),
    )})
    transport = ScriptedTransport(
        planning_response(),
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="echo", name="mcp__rag__echo", arguments='{"text":"hi"}',
        )], finish_reason="tool_calls"),
        NormalizedModelResponse(content="done"),
    )
    with agent_factory(config, transports={"primary": transport}) as agent:
        result = agent.run_turn_blocking("报销额度")
        from tools.runtime import get_tool_registry
        assert all(get_tool_registry().get_entry(name) is not None for name in hidden)
    assert result.status == "completed"
    assert [call[1] for call in client.calls] == ["query_knowledge_hub"] * 3 + ["echo"]
    for request in transport.requests[1:]:
        names = {item["function"]["name"] for item in request.body["tools"]}
        assert not hidden.intersection(names) and "mcp__rag__echo" in names
        assert "200 元" in request.body["messages"][0]["content"]


def test_dedicated_same_model_route_is_not_a_main_fallback(knowledge_stack):
    config, client = knowledge_stack
    route = config.routes[0].model_copy(update={"name": "knowledge-planner"})
    config = config.model_copy(update={"knowledge": config.knowledge.model_copy(update={"planner": route})})
    main = ScriptedTransport(NormalizedModelResponse(content="answer"))
    planner = ScriptedTransport(planning_response())
    configure_tools(config)
    resolver, api_modes, _ = scripted_provider_stack(config, {"primary": main})
    binding = resolver.resolve(config.routes[0])
    resolver = StaticProviderResolver({
        "primary": binding, "knowledge-planner": replace(binding, transport=planner),
    })
    with AgentCore(config, provider_resolver=resolver, api_modes=api_modes) as agent:
        result = agent.run_turn_blocking("项目 A 报销额度")
    assert result.status == "completed"
    assert len(main.requests) == len(planner.requests) == 1
    assert planner.requests[0].body["model"] == main.requests[0].body["model"]
    assert planner.contexts[0].route_name == "knowledge-planner" and result.route_used == "primary"
    assert main.closed and planner.closed


def test_planner_error_falls_back_once_without_main_retries(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    config = config.model_copy(update={"routes": (config.routes[0].model_copy(update={"max_attempts": 3}),)})
    transport = ScriptedTransport(NormalizedModelResponse(content="invalid json"),
                                  NormalizedModelResponse(content="answer"))
    with agent_factory(config, transports={"primary": transport}) as agent:
        result = agent.run_turn_blocking("原始问题")
    assert result.status == "completed" and result.provider_attempts == 1
    assert len(transport.requests) == 2 and len(client.calls) == 1
    assert client.calls[0][2]["query"] == "原始问题"


def test_guard_denial_prevents_background_mcp_call(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    class Deny:
        def check(self, invocation):
            return GuardDecision(False)
    transport = ScriptedTransport(planning_response(), NormalizedModelResponse(content="缺少资料"))
    with agent_factory(config, transports={"primary": transport}, guard=Deny()) as agent:
        result = agent.run_turn_blocking("q")
    assert result.status == "completed" and not client.calls
    assert "failed" in transport.requests[1].body["messages"][0]["content"]


def test_transient_loop_defaults_to_noop_even_when_config_enabled(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    transport = ScriptedTransport(NormalizedModelResponse(content="review done"))
    with agent_factory(config, transports={"primary": transport}) as agent:
        from tools.lifecycle import SessionResourceKey
        from agentcore.interfaces import NoopMemoryUpdater, NoopReviewScheduler, TurnHostContext
        from agentcore.provider_runner import RouteState
        from agentcore.prompt_builder import StaticSystemPromptBuilder, PromptBuildRequest
        from agentcore.session_context import SessionRuntimeSnapshot
        prompt = StaticSystemPromptBuilder("review prompt").build(PromptBuildRequest(()))
        loop = ConversationLoop(
            config=config, context=SessionRuntimeSnapshot(prompt, MappingProxyType({}), b"[]"),
            history=(Message.user("用户事实"), Message.assistant_text("收到")),
            user_input=UserInput(text="复盘"),
            session_id="review", turn_id="review-turn", owner_id="review-owner",
            services=agent._tool_services, cancel_event=threading.Event(),
            provider_resolver=agent._provider_resolver, api_modes=agent._api_modes,
            route_states=(RouteState(),), guard=agent._guard, host_context=TurnHostContext(),
            memory_updater=NoopMemoryUpdater(), review_scheduler=NoopReviewScheduler(),
            observability=agent._observability, call_operation=agent.call_operation,
            resource_key=SessionResourceKey("review-turn"),
        )
        result = loop.run()
    assert result.status == "completed" and len(transport.requests) == 1 and not client.calls


def test_transient_prepared_view_applies_knowledge_denies_without_reloading_tools(knowledge_stack, agent_factory):
    from tools.lifecycle import SessionResourceKey
    from tools.runtime import get_tool_registry
    from agentcore.interfaces import NoopMemoryUpdater, NoopReviewScheduler, TurnHostContext
    from agentcore.provider_runner import RouteState
    from agentcore.prompt_builder import StaticSystemPromptBuilder, PromptBuildRequest
    from agentcore.session_context import SessionRuntimeSnapshot

    config, client = knowledge_stack
    hidden_name = "mcp__rag__query_knowledge_hub"
    class Provider:
        hidden_model_tools = frozenset({hidden_name})
        def prepare(self, request, runtime):
            return KnowledgeContext()

    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="hidden", name=hidden_name, arguments='{"query":"q","top_k":5,"collection":"docs"}',
        )], finish_reason="tool_calls"),
        NormalizedModelResponse(content="done"),
    )
    with agent_factory(config, transports={"primary": transport}) as agent:
        registry = get_tool_registry()
        entries = {name: registry.get_entry(name) for name in (hidden_name, "mcp__rag__echo")}
        definitions = [entry.definition() for entry in entries.values()]
        definitions[1]["function"]["description"] = "prepared description"
        prompt = StaticSystemPromptBuilder("transient prompt").build(PromptBuildRequest(()))
        snapshot = SessionRuntimeSnapshot(prompt, MappingProxyType(entries), json.dumps(definitions).encode())
        loop = ConversationLoop(
            config=config, context=snapshot, user_input=UserInput(text="q"),
            session_id="transient", turn_id="transient-turn", owner_id="transient-owner",
            services=agent._tool_services, cancel_event=threading.Event(),
            provider_resolver=agent._provider_resolver, api_modes=agent._api_modes,
            route_states=(RouteState(),), guard=agent._guard, host_context=TurnHostContext(),
            memory_updater=NoopMemoryUpdater(), review_scheduler=NoopReviewScheduler(),
            observability=agent._observability, call_operation=agent.call_operation,
            resource_key=SessionResourceKey("transient-turn"), knowledge_provider=Provider(),
        )
        result = loop.run()
        assert registry.get_entry(hidden_name) is entries[hidden_name]
    assert result.status == "completed" and not client.calls
    assert "unknown_tool" in json.dumps(result.model_dump())
    for request in transport.requests:
        assert request.body["tools"] == [definitions[1]]
    assert hidden_name in snapshot.entries_by_name  # The caller's supplied view stays intact.
    assert len(json.loads(snapshot.tool_schema_bytes)) == 2


def test_effective_permissions_are_checked_each_turn(knowledge_stack, agent_factory):
    from tools.runtime import get_tool_registry
    config, client = knowledge_stack
    configure_tools(config)
    registry = get_tool_registry()
    registry.replace_all(tuple(
        replace(entry, required_permissions=frozenset({"knowledge.read"}))
        if entry.name == "mcp__rag__query_knowledge_hub" else entry
        for entry in registry.entries()
    ))
    transport = ScriptedTransport(planning_response(), NormalizedModelResponse(content="无访问权限"),
                                  planning_response(), NormalizedModelResponse(content="200 元"))
    with agent_factory(config, transports={"primary": transport}) as agent:
        denied = agent.run_turn_blocking("报销额度", session_id="policy")
        assert not client.calls
        allowed = agent.run_turn_blocking(
            "报销额度", session_id="policy",
            session_request=SessionBuildRequest(permissions=frozenset({"knowledge.read"})),
        )
    assert denied.status == allowed.status == "completed"
    assert len(client.calls) == 3
    assert "failed" in transport.requests[1].body["messages"][0]["content"]
    assert "200 元" in transport.requests[3].body["messages"][0]["content"]


def test_cancel_during_planning_never_starts_retrieval(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    entered, release = threading.Event(), threading.Event()
    def blocked(request):
        entered.set()
        assert release.wait(3)
        return planning_response()
    transport = ScriptedTransport(blocked)
    with agent_factory(config, transports={"primary": transport}) as agent:
        submission = agent.submit_turn("报销额度")
        assert entered.wait(2)
        submission.cancel()
        try:
            result = submission.future.result(timeout=2)
        finally:
            release.set()
    assert result.status == "interrupted" and not client.calls
    assert len(transport.requests) == 1


def test_guard_wait_is_bounded_by_knowledge_phase(knowledge_stack, agent_factory):
    config, client = knowledge_stack
    config = config.model_copy(update={"knowledge": config.knowledge.model_copy(update={
        "timeout_seconds": 1, "planner_timeout_seconds": 1,
    })})
    release = threading.Event()
    class BlockedGuard:
        def check(self, invocation):
            release.wait(3)
            return GuardDecision(True)
    transport = ScriptedTransport(planning_response(), NormalizedModelResponse(content="资料获取超时"))
    with agent_factory(config, transports={"primary": transport}, guard=BlockedGuard()) as agent:
        started = time.monotonic()
        try:
            result = agent.run_turn_blocking("报销额度")
        finally:
            release.set()
        elapsed = time.monotonic() - started
    assert result.status == "completed" and elapsed < 2.5 and not client.calls
    assert "failed" in transport.requests[1].body["messages"][0]["content"]


def test_explicit_provider_needs_no_default_model_or_mcp(config_factory, agent_factory):
    config = config_factory().model_copy(update={"knowledge": KnowledgeConfig(enabled=True)})
    requests = []
    class Provider:
        hidden_model_tools = frozenset()
        def prepare(self, request, runtime):
            requests.append(request)
            return KnowledgeContext(status="retrieved", items=(KnowledgeItem("a", "docs", "自定义证据"),))
    transport = ScriptedTransport(NormalizedModelResponse(content="answer"))
    with agent_factory(config, transports={"primary": transport}, knowledge_provider=Provider()) as agent:
        result = agent.run_turn_blocking("q")
    assert result.status == "completed" and len(requests) == len(transport.requests) == 1
    assert "自定义证据" in transport.requests[0].body["messages"][0]["content"]


def test_concurrent_turns_do_not_share_knowledge(config_factory, agent_factory):
    barrier = threading.Barrier(2)
    class Provider:
        hidden_model_tools = frozenset()
        def prepare(self, request, runtime):
            barrier.wait(timeout=3)
            return KnowledgeContext(status="retrieved", items=(
                KnowledgeItem(request.session_id, "docs", f"evidence:{request.question}"),
            ))
    def answer(request):
        question = request.body["messages"][-1]["content"]
        prompt = request.body["messages"][0]["content"]
        assert f"evidence:{question}" in prompt
        other = "first" if question == "second" else "second"
        assert f"evidence:{other}" not in prompt
        return NormalizedModelResponse(content=question)
    transport = ScriptedTransport(answer, answer)
    with agent_factory(config_factory(), transports={"primary": transport}, knowledge_provider=Provider()) as agent:
        first = agent.submit_turn("first", session_id="first")
        second = agent.submit_turn("second", session_id="second")
        results = [submission.future.result(timeout=5) for submission in (first, second)]
    assert [result.final_text for result in results] == ["first", "second"]
    assert all(result.status == "completed" for result in results)
