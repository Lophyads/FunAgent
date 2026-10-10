from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import json
import time

import pytest

from funagent.conf import ConfMgr
from funagent.contracts import ControlError, DomainError, RunContext, ToolCall, ToolDefinition
from funagent.memory import (MemoryMgr, SQLiteMemoryStore, CallbackMemoryStore, MemoryDraft,
                             MemoryPatch, MemoryQuery, MemoryListQuery, MemoryRef, MemoryPrepareRequest)
from funagent.memory.retrieval import MemorySearchHandler, SEARCH_DEFINITION
from funagent.model import ModelRuntime
from funagent.tool import ToolMgr
from fakes import ScriptedProvider, SpyToolHandler, ChatAdapter, BlocksAdapter, final


@pytest.fixture
def memory_app(conf_dir, tmp_path):
    file = conf_dir / "memory.conf"
    file.write_text(file.read_text().replace("enabled = false", "enabled = true").replace('sources = ["facts"]', 'sources = ["facts", "kb"]', 1) + '''
[sources.kb]
kind = "semantic"
store = "callback"
callback_id = "kb_query"
capabilities = ["search"]
''', encoding="utf8")
    def bind(provider=None, query=None, policy=None, protocol=None):
        if policy:
            content = file.read_text(encoding="utf8")
            # Read this table independently; brackets in source arrays are data.
            import re
            section = re.search(r"\[policies.recall\]\n(.*?)(?=\n\[|\Z)", content, re.S)
            body = section.group(1)
            for name, value in policy.items():
                body = re.sub(r"(?m)^" + name + r"\s*=.*$", name + " = " + value, body)
            content = content[:section.start(1)] + body + content[section.end(1):]
            file.write_text(content, encoding="utf8")
        ConfMgr.Initialize(conf_dir)
        conf = ConfMgr.GetInstance()
        provider = provider or ScriptedProvider()
        ModelRuntime.Initialize({name: replace(conf.Model().Profile(name), adapter_id=protocol or conf.Model().Profile(name).adapter_id) for name in ("main", "fallback")},
                                {"fake": provider}, {"chat": ChatAdapter, "blocks": BlocksAdapter})
        spy = SpyToolHandler()
        ToolMgr.Close()
        ToolMgr.Initialize({"memory_search": SEARCH_DEFINITION, "echo": ToolDefinition("echo", "Echo", {"type": "object"})},
                           {"memory_search": MemorySearchHandler, "echo": spy})
        facts = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
        facts.Write(MemoryDraft("facts", "用户喜欢中文", "language", ("session:s1/message:1",), "profile"), RunContext())
        kb = CallbackMemoryStore("kb", query or (lambda query, ctx: [{
            "record_id": "doc-1", "type": "document", "content": "真实语义证据", "citation": "https://kb.example/1", "score": -900,
        }]))
        MemoryMgr.Initialize({"facts": facts, "kb": kb})
        return MemoryMgr.GetInstance(), facts, provider
    try:
        yield bind
    finally:
        MemoryMgr.Close()


def reader(**kwargs):
    return RunContext(run_id="main-run", access=frozenset({"memory.facts.read", "memory.kb.read"}), **kwargs)


def search_call(id="search", **kwargs):
    return {"calls": [{"id": id, "name": "memory_search", "args": {"text": "中文", **kwargs}}], "finish": "tool_calls"}


def test_direct_query_profile_and_crud_do_not_run_model(memory_app):
    provider = ScriptedProvider()
    mgr, _, _ = memory_app(provider)
    result = mgr.Search(MemoryQuery("中文"), reader())
    assert {r.kind for r in result.records} == {"episodic", "semantic"}
    assert len(mgr.Search(MemoryQuery("中文", kind="episodic"), reader()).records) == 1
    assert {r.ref.source_id for r in mgr.Search(MemoryQuery("中文", ("kb",)), reader()).records} == {"kb"}
    assert mgr.GetProfile(reader())[0].type == "profile"
    assert mgr.List(MemoryListQuery("facts"), reader()).items
    writer = replace(reader(), access=frozenset({"memory.facts.write", "memory.facts.read"}))
    saved = mgr.Write(MemoryDraft("facts", "一条事实", "turn2/fact1", ("session:s1/message:3",)), writer)
    updated = mgr.Update(saved.ref, MemoryPatch("新事实", ("session:s1/message:5",)), saved.version, writer)
    with pytest.raises(DomainError) as error:
        mgr.Delete(saved.ref, saved.version, writer)
    assert error.value.code == "CONFLICT"
    mgr.Delete(saved.ref, updated.version, writer)
    assert not provider.requests


def test_authorization_preflight_readonly_capability_and_tool_args_cannot_grant_access(memory_app):
    queries = []
    mgr, _, _ = memory_app(query=lambda q, ctx: queries.append(q) or [])
    with pytest.raises(DomainError) as error:
        mgr.Search(MemoryQuery(source_ids=("facts", "kb")), replace(reader(), access=frozenset({"memory.facts.read"})))
    assert error.value.code == "DENIED" and not queries
    with pytest.raises(DomainError) as error:
        mgr.Write(MemoryDraft("kb", "forged", "model-key", ("fake",)), replace(reader(), access=frozenset({"memory.kb.write"})))
    assert error.value.code == "UNSUPPORTED"
    with pytest.raises(DomainError) as error:
        mgr.List(MemoryListQuery("kb"), reader())
    assert error.value.code == "UNSUPPORTED"
    for args in ({"text": "q", "access": ["memory.kb.read"]}, {"text": "q", "evidence": ["fake"]},
                 {"text": "q", "write_key": "fake"}):
        result = ToolMgr.GetInstance().Execute(ToolCall("c", "memory_search", args), ("memory_search",), reader())
        assert result.status == "invalid"
    assert not queries


def test_prepare_uses_real_records_and_ignores_model_facts_scores_and_forged_refs(memory_app):
    provider = ScriptedProvider(search_call(), final("chat", '{"refs":[{"source_id":"kb","record_id":"fake"}],"summary":"伪造事实"}'))
    mgr, _, _ = memory_app(provider)
    result = mgr.Prepare(MemoryPrepareRequest("用户偏好和文档"), reader())
    assert [r.ref.source_id for r in result.records] == ["facts", "kb"]
    assert all(r.ref.record_id != "fake" for r in result.records)
    assert "伪造事实" not in repr(result)
    assert result.queries == (MemoryQuery("中文"),)
    assert result.source_status == {"facts": "ok", "kb": "ok"}
    assert [c.purpose for c in provider.contexts] == ["recall", "recall"]
    assert all(c.record_scope == "ephemeral" for c in provider.contexts)
    assert [t["name"] for t in provider.requests[0].payload["tools"]] == ["memory_search"]


def test_source_failure_preserves_other_source_real_evidence(memory_app):
    def fail(q, context):
        raise RuntimeError("SECRET_TOKEN")
    mgr, _, _ = memory_app(ScriptedProvider(search_call(), final("chat")), fail)
    result = mgr.Prepare(MemoryPrepareRequest("查找"), reader())
    assert len(result.records) == 1 and result.records[0].kind == "episodic"
    assert result.source_status == {"facts": "ok", "kb": "failed"}
    assert result.status == "partial" and result.warnings
    assert "SECRET_TOKEN" not in repr(result)


def test_no_retrieval_returns_skipped_with_one_decision_run(memory_app):
    provider = ScriptedProvider(final("chat", "不需要检索"))
    mgr, _, _ = memory_app(provider)
    result = mgr.Prepare(MemoryPrepareRequest("你好"), reader())
    assert result.status == "skipped" and not result.records and not result.queries
    assert len(provider.requests) == 1


def test_prepare_search_reentry_finishes(memory_app):
    provider = ScriptedProvider(search_call(), final("chat"))
    mgr, _, _ = memory_app(provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(mgr.Prepare, MemoryPrepareRequest("中文"), reader()).result(timeout=3)
    assert len(result.records) == 2


def test_child_deadline_degrades_but_parent_cancel_and_deadline_propagate(memory_app, monkeypatch):
    now = [time.monotonic()]
    def expire(request, context):
        now[0] += 3
        return final_response("done")
    mgr, _, _ = memory_app(ScriptedProvider(search_call(), expire))
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    result = mgr.Prepare(MemoryPrepareRequest("中文"), reader(deadline=now[0] + 30))
    assert len(result.records) == 2 and result.status == "partial" and "recall:DEADLINE" in result.warnings
    cancel = Event(); cancel.set()
    with pytest.raises(ControlError) as error:
        mgr.Prepare(MemoryPrepareRequest("中文"), reader(cancel=cancel))
    assert error.value.code == "CANCELLED"
    with pytest.raises(ControlError) as error:
        mgr.Prepare(MemoryPrepareRequest("中文"), reader(deadline=now[0]))
    assert error.value.code == "DEADLINE"


def final_response(text):
    from funagent.model import ProviderResponse
    return ProviderResponse(final("chat", text))


def test_unknown_sources_and_empty_authorized_context_fail_closed(memory_app):
    mgr, _, _ = memory_app()
    with pytest.raises(DomainError) as error:
        mgr.Search(MemoryQuery(source_ids=("fake",)), reader())
    assert error.value.code == "NOT_FOUND"
    assert not mgr.Search(MemoryQuery(), RunContext()).records
    with pytest.raises(DomainError) as error:
        mgr.GetProfile(RunContext())
    assert error.value.code == "DENIED"



def test_concurrent_prepare_runs_have_isolated_evidence(memory_app):
    from threading import Barrier
    from funagent.model import ProviderResponse
    gate = Barrier(2)
    class RoutedProvider(ScriptedProvider):
        def Send(self, request, context):
            last = request.payload["messages"][-1]
            if last["role"] == "user":
                gate.wait(timeout=3)
                return ProviderResponse(search_call(context.run_id, text=last["content"], sources=["kb"]))
            return ProviderResponse(final("chat", "MODEL_GENERATED_UNTRUSTED"))
    def query(q, ctx):
        return [{"record_id": q.text, "type": "document", "content": q.text + "正文", "citation": "https://kb.example/" + q.text}]
    mgr, _, _ = memory_app(RoutedProvider(), query)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(mgr.Prepare, MemoryPrepareRequest(label), reader()) for label in ("甲", "乙")]
        results = [job.result(timeout=5) for job in jobs]
    assert [[r.content for r in result.records] for result in results] == [["甲正文"], ["乙正文"]]
    assert [result.queries[0].text for result in results] == ["甲", "乙"]


@pytest.mark.parametrize("protocol", ["chat", "blocks"])
def test_actual_retrieval_strategy_runs_with_both_wire_adapters(memory_app, protocol):
    from fakes import response_for
    provider = ScriptedProvider(response_for(protocol, search_call()), response_for(protocol, final("chat")))
    mgr, _, _ = memory_app(provider, protocol=protocol)
    result = mgr.Prepare(MemoryPrepareRequest("中文"), reader())
    assert len(result.records) == 2 and result.status == "ready"
    assert all(request.payload["protocol"] == protocol for request in provider.requests)


def test_parent_cancel_and_total_deadline_during_child_run_propagate(memory_app, monkeypatch):
    parent_cancel = Event()
    def cancel_request(request, context):
        assert context.cancel is parent_cancel
        parent_cancel.set()
        return final_response("not evidence")
    mgr, _, _ = memory_app(ScriptedProvider(cancel_request))
    with pytest.raises(ControlError) as error:
        mgr.Prepare(MemoryPrepareRequest("cancel"), reader(cancel=parent_cancel))
    assert error.value.code == "CANCELLED"
    # Reassemble only the runtime, retaining the physical memory stores.
    ModelRuntime.Close()
    now = [time.monotonic()]
    def expire(request, context):
        now[0] += 2
        return final_response("not evidence")
    provider = ScriptedProvider(expire)
    conf = ConfMgr.GetInstance()
    ModelRuntime.Initialize({name: conf.Model().Profile(name) for name in ("main", "fallback")}, {"fake": provider},
                            {"chat": ChatAdapter, "blocks": BlocksAdapter})
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    with pytest.raises(ControlError) as error:
        mgr.Prepare(MemoryPrepareRequest("deadline"), reader(deadline=now[0] + 0.5))
    assert error.value.code == "DEADLINE"


def test_recall_tool_cannot_select_source_outside_policy_even_when_parent_can(memory_app):
    provider = ScriptedProvider(search_call(sources=["kb"]), final("chat"))
    mgr, _, _ = memory_app(provider, policy={"sources": '["facts"]'})
    result = mgr.Prepare(MemoryPrepareRequest("scope"), reader())
    assert not result.records
    assert provider.requests[1].payload["messages"][-1]["status"] == "denied"
    assert result.warnings == ("recall:TOOL_DENIED",)


def test_evidence_budget_deduplicates_and_preserves_real_citations(memory_app):
    provider = ScriptedProvider(search_call(), search_call("repeat"), final("chat"))
    mgr, _, _ = memory_app(provider, policy={"max_records": "1"})
    result = mgr.Prepare(MemoryPrepareRequest("budget"), reader())
    assert len(result.records) == 1 and result.records[0].citation.startswith("memory://facts/")
    assert result.warnings == ("recall:BUDGET",)
    assert len(result.queries) == 2
    # A source-local limit is never interpreted as a comparable score.
    all_records = mgr.Search(MemoryQuery(limit=2), reader()).records
    assert [r.ref.source_id for r in all_records] == ["facts", "kb"]


def test_child_timeout_during_search_keeps_earlier_actual_source_records(memory_app, monkeypatch):
    now = [time.monotonic()]
    def slow_query(q, ctx):
        now[0] += 3
        return [{"record_id": "late", "type": "document", "content": "late", "citation": "https://kb.example/late"}]
    mgr, _, _ = memory_app(ScriptedProvider(search_call()), slow_query)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    result = mgr.Prepare(MemoryPrepareRequest("中文"), reader(deadline=now[0] + 30))
    assert [r.ref.source_id for r in result.records] == ["facts"]
    assert result.source_status == {"facts": "ok", "kb": "timeout"}
    assert result.status == "partial" and "recall:DEADLINE" in result.warnings
