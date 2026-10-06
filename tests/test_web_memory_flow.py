"""Web HTTP -> real agent/review/tool loops -> temporary SQLite memory.

Only model transports are scripted. No user config, credentials or live database
is used; foreground and review models have separate scripts and request logs.
"""
from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from agentcore import Message, NormalizedModelResponse, ToolCall
from conftest import ScriptedTransport, scripted_provider_stack
from gateway.web import WebHost, WebServer
from memory import MemoryConfig, MemoryFeature, SqliteConfig
from memory.review import history_fingerprint
from memory.sqlite import SqliteMemoryStore
from test_web import wait_for
from tools.runtime import get_tool_registry


def reply(text):
    return NormalizedModelResponse(content=text, finish_reason="stop")


def memory_call(call_id, **arguments):
    return NormalizedModelResponse(tool_calls=[ToolCall(
        id=call_id, name="memory", arguments=json.dumps(arguments, ensure_ascii=False),
    )])


@pytest.fixture
def memory_web(config_factory, tmp_path, monkeypatch):
    """Assemble the same memory components as CLI Web, with offline providers."""
    resources = []

    def make(*responses, every_turns=0, review_responses=()):
        config = config_factory(system_prompt="你是测试助手。需要历史事件时使用 memory 搜索。")
        foreground = ScriptedTransport(*responses)
        resolver, modes, _ = scripted_provider_stack(config, {"primary": foreground})
        settings = MemoryConfig(
            review_every_turns=every_turns, sqlite=SqliteConfig(tmp_path / "memory.db"),
        )
        feature = MemoryFeature(settings, config)
        feature.install_tool(get_tool_registry())
        reviewer = ScriptedTransport(*review_responses)
        reviews = []
        if feature.runner is not None:
            review_resolver, review_modes, _ = scripted_provider_stack(
                config, {"primary": reviewer}, initialize_tools=False,
            )
            feature.runner.resolver.close()
            feature.runner.resolver, feature.runner.api_modes = review_resolver, review_modes
            submit = feature.queue.submit

            def record_review(snapshot):
                submission = submit(snapshot)
                reviews.append(submission)
                return submission

            monkeypatch.setattr(feature.queue, "submit", record_review)
        host = WebHost(
            config, memory=feature.store, memory_settings=settings,
            prompt_builder=feature.prompt_builder, review_scheduler=feature.review_scheduler,
            provider_resolver=resolver, api_modes=modes,
        )
        host.start()
        server = WebServer(host, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        resources.append((host, feature, server, thread))
        thread.start()
        assert server._serving.wait(3)

        def request(method, path, body=None, expected=200):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
                connection.request(method, path, data, {"Content-Type": "application/json"})
                response = connection.getresponse()
                value = json.loads(response.read())
                assert response.status == expected, value
                return value
            finally:
                connection.close()

        def chat(text, sid="chat", request_id=None):
            dto = request("POST", "/api/turns", {
                "session_id": sid, "text": text,
                "client_request_id": request_id or f"request-{len(foreground.requests)}",
            }, expected=202)
            completed = wait_for(lambda: request("GET", f'/api/turns/{dto["turn_id"]}')["result"])
            return dto, completed

        return SimpleNamespace(
            request=request, chat=chat, feature=feature, foreground=foreground,
            reviewer=reviewer, reviews=reviews, host=host,
        )

    yield make
    for host, feature, server, thread in reversed(resources):
        server.stop()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()
        try:
            assert host.close(3).completed
        finally:
            try:
                assert feature.close(3)
            finally:
                host.events.close()


@pytest.mark.parametrize("every_turns", [1, 2, 6, 10])
def test_web_n_turns_review_persists_memory_without_blocking_reply(memory_web, every_turns):
    started, release = threading.Event(), threading.Event()
    fact = "用户偏好简洁的中文回答。"

    def blocked_review(request):
        started.set()
        assert release.wait(5), "test did not release the background model"
        return memory_call("review-add", action="add", kind="user", content=fact)

    web = memory_web(
        *(reply(f"已确认第 {i} 轮。") for i in range(1, every_turns + 1)),
        every_turns=every_turns, review_responses=(blocked_review, reply("复盘完成。")),
    )
    expected_history = []
    try:
        for i in range(1, every_turns + 1):
            text = f"第 {i} 轮：我偏好简洁的中文回答。"
            dto, result = web.chat(text)
            assert result["status"] == "completed" and result["final_text"] == f"已确认第 {i} 轮。"
            expected_history.extend([("user", text), ("assistant", result["final_text"])])
            if i < every_turns:
                assert web.reviews == [] and web.reviewer.requests == []
            assert web.request("GET", "/api/memory/user")["total"] == 0
        assert started.wait(3)
        assert len(web.reviews) == 1 and not web.reviews[0].future.done()
        # The last foreground HTTP result is already complete while review is blocked.
        release.set()
        web.reviews[0].future.result(timeout=5)
        entry, = web.request("GET", "/api/memory/user")["entries"]
        assert entry["content"] == fact and entry["version"] == 1
        assert entry["source"] == {
            "type": "review", "session_id": "chat", "turn_id": dto["turn_id"],
            "range_start": 0, "range_end": 2 * every_turns,
        }
        messages = web.request("GET", "/api/sessions/chat/messages")["messages"]
        assert [(m["role"], m["content"]) for m in messages] == expected_history
        review = web.reviewer.requests[0].body
        assert [(m["role"], m["content"]) for m in review["messages"][1:-1]] == expected_history
        assert "Review the supplied conversation" in review["messages"][0]["content"]
        assert [tool["function"]["name"] for tool in review["tools"]] == ["memory"]
        progress = web.feature.store.review_state("chat")
        assert progress.completed_count == every_turns and progress.reviewed_position == len(messages)
        canonical = [Message(role=role, content=text).model_dump(mode="json") for role, text in expected_history]
        assert progress.reviewed_fingerprint == history_fingerprint(canonical)
        persisted = SqliteMemoryStore(web.feature.store.path)
        try:
            assert persisted.list().entries[0].id == entry["id"]
        finally:
            persisted.close()
    finally:
        release.set()


def test_web_review_is_incremental_and_duplicate_facts_do_not_create_rows(memory_web):
    add = memory_call("add-fact", action="add", kind="user", content="用户偏好中文。")
    web = memory_web(
        *(reply(f"答复 {i}") for i in range(4)), every_turns=2,
        review_responses=(add, reply("已保存。"), add, reply("已有相同记忆。")),
    )
    for i in range(4):
        _, result = web.chat(f"问题 {i}：请用中文。")
        assert result["status"] == "completed"
        if (i + 1) % 2 == 0:
            web.reviews[-1].future.result(timeout=5)
    assert len(web.reviews) == 2 and len(web.foreground.requests) == 4
    assert [(m["role"], m["content"]) for m in web.reviewer.requests[2].body["messages"][1:-1]] == [
        ("user", "问题 2：请用中文。"), ("assistant", "答复 2"),
        ("user", "问题 3：请用中文。"), ("assistant", "答复 3"),
    ]
    duplicate = json.loads(web.reviewer.requests[3].body["messages"][-1]["content"])
    assert duplicate["ok"] and duplicate["result"]["reason"] == "duplicate"
    assert duplicate["result"]["changed"] is False
    rows = web.request("GET", "/api/memory/user")
    assert rows["total"] == 1 and rows["entries"][0]["version"] == 1
    messages = web.request("GET", "/api/sessions/chat/messages")["messages"]
    assert len(messages) == 8 and all(m["role"] in {"user", "assistant"} for m in messages)
    progress = web.feature.store.review_state("chat")
    assert progress.completed_count == 4 and progress.reviewed_position == 8


def test_web_retry_of_same_request_does_not_count_as_another_turn(memory_web):
    web = memory_web(reply("第一轮。"), reply("第二轮。"), every_turns=2,
                     review_responses=(reply("没有新记忆。"),))
    first, result = web.chat("你好", request_id="stable-request")
    assert result["status"] == "completed"
    duplicate = web.request("POST", "/api/turns", {
        "session_id": "chat", "text": "你好", "client_request_id": "stable-request",
    }, expected=202)
    assert duplicate == first
    assert len(web.foreground.requests) == 1 and web.reviews == []
    assert web.feature.store.review_state("chat").completed_count == 1
    web.chat("继续", request_id="second-request")
    web.reviews[0].future.result(timeout=5)
    assert len(web.reviews) == 1 and web.feature.store.review_state("chat").completed_count == 2


def test_web_review_disabled_keeps_chat_working(memory_web):
    web = memory_web(reply("一"), reply("二"), every_turns=0)
    for text in ("我喜欢中文。", "记住这个偏好。"):
        _, result = web.chat(text)
        assert result["status"] == "completed"
    assert web.feature.runner is None and web.feature.queue is None
    assert web.reviewer.requests == [] and web.request("GET", "/api/memory/user")["total"] == 0


def test_web_failed_foreground_does_not_trigger_review(memory_web):
    web = memory_web(RuntimeError("offline model failure"), every_turns=1)
    _, result = web.chat("这次模型请求会失败。")
    assert result["status"] == "failed"
    assert web.reviews == [] and web.reviewer.requests == []
    assert web.feature.store.review_state("chat").completed_count == 0
    assert web.request("GET", "/api/memory/user")["total"] == 0


def test_web_failed_review_preserves_checkpoint_and_next_review_recovers(memory_web):
    web = memory_web(
        reply("前台正常回答。"), reply("前台继续正常回答。"), every_turns=1,
        review_responses=(RuntimeError("offline review failure"),
                          memory_call("recover-add", action="add", kind="user", content="用户喜欢茶。"),
                          reply("复盘完成。")),
    )
    _, result = web.chat("我喜欢茶。")
    assert result["status"] == "completed"
    with pytest.raises(RuntimeError, match="memory review failed"):
        web.reviews[0].future.result(timeout=5)
    assert web.feature.store.review_state("chat").reviewed_position == 0
    assert web.request("GET", "/api/memory/user")["total"] == 0
    _, result = web.chat("后续回答请考虑这个偏好。")
    assert result["status"] == "completed"
    web.reviews[1].future.result(timeout=5)
    entry, = web.request("GET", "/api/memory/user")["entries"]
    assert entry["source"]["range_start"] == 0 and entry["source"]["range_end"] == 4
    assert web.feature.store.review_state("chat").reviewed_position == 4
    assert len(web.reviewer.requests[1].body["messages"][1:-1]) == 4


def test_web_new_session_injects_core_memory_and_existing_prompt_stays_frozen(memory_web):
    web = memory_web(reply("首次回答。"), reply("继续回答。"), reply("新会话回答。"))
    user = web.request("PATCH", "/api/memory/user", {
        "action": "add", "content": "用户偏好简洁中文。",
    })["entry"]
    note = web.request("PATCH", "/api/memory/note", {
        "action": "add", "content": "项目使用 SQLite。",
    })["entry"]
    web.request("PATCH", "/api/memory/episode", {
        "action": "add", "content": "历史事件：曾讨论缓存迁移。", "event_at": "2026-10-06",
    })
    web.chat("介绍一下项目。", sid="existing")
    first = web.foreground.requests[0].body["messages"][0]["content"]
    assert "<bootstrap_memory>" in first and "untrusted context" in first
    assert user["content"] in first and note["content"] in first
    assert f'id={note["id"]} v1' in first and "历史事件：曾讨论缓存迁移。" not in first
    web.request("PATCH", "/api/memory/note", {
        "action": "replace", "id": note["id"], "expected_version": 1, "content": "项目改用 Supabase。",
    })
    web.chat("继续。", sid="existing")
    web.chat("介绍一下项目。", sid="new")
    assert web.foreground.requests[1].body["messages"][0]["content"] == first
    current = web.foreground.requests[2].body["messages"][0]["content"]
    assert "项目改用 Supabase。" in current and "项目使用 SQLite。" not in current
    assert f'id={note["id"]} v2' in current


@pytest.mark.parametrize("query, hit", [("缓存迁移", True), ("不存在的火星基地", False)])
def test_web_search_result_enters_next_llm_context_without_mutating_memory(memory_web, query, hit):
    captured = []

    def answer_after_search(request):
        messages = request.body["messages"]
        assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "recall"
        payload = json.loads(messages[-1]["content"])
        assert payload["ok"]
        captured.extend(payload["result"]["entries"])
        return reply("找到了缓存迁移记录。" if captured else "没有找到相关记忆。")

    web = memory_web(
        memory_call("recall", action="search", kind="episode", query=query, limit=5), answer_after_search,
    )
    related = web.request("PATCH", "/api/memory/episode", {
        "action": "add", "content": "缓存迁移：决定先保留 Redis，以减少迁移风险。", "event_at": "2026-10-06",
    })["entry"]
    web.request("PATCH", "/api/memory/episode", {
        "action": "add", "content": "周末旅行：计划参观博物馆。", "event_at": "2026-10-05",
    })
    before = web.request("GET", "/api/memory/episode")["entries"]
    _, result = web.chat(f"查一下我们之前的{query}记录。")
    assert result["status"] == "completed"
    assert result["final_text"] == ("找到了缓存迁移记录。" if hit else "没有找到相关记忆。")
    assert [entry["id"] for entry in captured] == ([related["id"]] if hit else [])
    first = web.foreground.requests[0].body["messages"][0]["content"]
    assert related["content"] not in first  # Episodes arrive through search, not bootstrap.
    assert web.request("GET", "/api/memory/episode")["entries"] == before
    history = web.request("GET", "/api/sessions/chat/messages")["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2]["tool_name"] == "memory" and history[2]["tool_call_id"] == "recall"


def test_web_reviewed_fact_is_available_in_next_session(memory_web):
    fact = "用户喜欢无糖绿茶。"
    web = memory_web(
        reply("记下你的饮品偏好。"), reply("推荐无糖绿茶。"), every_turns=1,
        review_responses=(memory_call("remember", action="add", kind="user", content=fact),
                          reply("复盘完成。"), reply("没有新事实。")),
    )
    web.chat("我喜欢无糖绿茶。", sid="source")
    web.reviews[0].future.result(timeout=5)
    assert fact not in web.foreground.requests[0].body["messages"][0]["content"]
    _, result = web.chat("推荐一杯适合我的饮品。", sid="fresh")
    assert result["status"] == "completed"
    assert fact in web.foreground.requests[1].body["messages"][0]["content"]
    web.reviews[1].future.result(timeout=5)
