from contextlib import closing
"""The main task and CLI use the same memory APIs, with isolated DB histories."""
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path

import pytest

from funagent.conf import ConfMgr
from funagent.contracts import DomainError, SessionCreate, RunContext, TurnRequest
from funagent.global_server import Start, Stop
from funagent.memory import MemoryMgr, MemoryDraft, MemoryPatch, MemoryQuery
from funagent.model import ModelRuntime
from funagent.model.openai_chat import OpenAIProvider
from funagent.persistence import DbMgr
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from funagent.api.cli import CLIController
from fakes import ScriptedProvider, ChatAdapter, final
from test_task_flow import wait_task
from test_memory_prepare import search_call


@pytest.fixture
def memory_cli_conf(cli_conf):
    file = cli_conf / "memory.conf"
    content = file.read_text().replace('sources = ["facts"]', 'sources = ["facts", "kb"]', 1)
    content = content.replace('enabled = false', 'enabled = true')
    content += '''
[sources.kb]
kind = "semantic"
store = "callback"
callback_id = "kb_query"
capabilities = ["search"]
'''
    file.write_text(content, encoding="utf8")
    return cli_conf


def bind_query(monkeypatch):
    from funagent import global_server
    monkeypatch.setitem(global_server.MEMORY_QUERIES, "kb_query", lambda settings: lambda query, context: [{
        "record_id": "doc-1", "type": "document", "content": "外部知识正文", "citation": "https://kb.example/1",
    }])


def admin():
    return RunContext(access=frozenset({"memory.facts.read", "memory.facts.write", "memory.kb.read"}))


def test_one_prepare_per_turn_latest_profile_and_recall_never_enter_raw_history(memory_cli_conf, monkeypatch):
    bind_query(monkeypatch)
    Start(memory_cli_conf)
    provider = ScriptedProvider(search_call(), final("chat", "伪造召回摘要"), final("chat", "第一轮完整回复"),
                                final("chat", "跳过检索"), final("chat", "第二轮完整回复"))
    ModelRuntime.Close()
    conf = ConfMgr.GetInstance()
    ModelRuntime.Initialize({name: replace(conf.Model().Profile(name), provider_id="test", adapter_id="chat") for name in ("main", "fallback")},
                            {"test": provider}, {"chat": ChatAdapter})
    saved = MemoryMgr.GetInstance().Write(MemoryDraft("facts", "用户喜欢中文", "language", ("manual:1",), "profile"), admin())
    prepares = []
    original = MemoryMgr.GetInstance().Prepare
    def prepare(request, context):
        prepares.append((request, context))
        return original(request, context)
    monkeypatch.setattr(MemoryMgr.GetInstance(), "Prepare", prepare)
    session = SessionMgr.Create(SessionCreate())
    mgr = TaskMgr.GetInstance()
    first = wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "查偏好与文档")))
    assert first.status == "completed" and first.result.final == "第一轮完整回复"
    frozen = SessionMgr.Load(session.session_id).session.prompt
    main_prompt = provider.requests[2].payload["messages"][0]["content"]
    assert "用户喜欢中文" in main_prompt and "外部知识正文" in main_prompt and "https://kb.example/1" in main_prompt
    assert "伪造召回摘要" not in main_prompt
    MemoryMgr.GetInstance().Update(saved.ref, MemoryPatch("用户现在喜欢英文", ("manual:2",)), saved.version, admin())
    second = wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "你好")))
    assert second.result.final == "第二轮完整回复"
    assert SessionMgr.Load(session.session_id).session.prompt == frozen
    second_prompt = provider.requests[-1].payload["messages"][0]["content"]
    assert "用户现在喜欢英文" in second_prompt and "用户喜欢中文" not in second_prompt
    history = SessionMgr.ReadHistory(session.session_id).items
    assert [(m.role, m.content) for m in history] == [("user", "查偏好与文档"), ("assistant", "第一轮完整回复"),
                                                   ("user", "你好"), ("assistant", "第二轮完整回复")]
    assert len(prepares) == 2
    assert [c.purpose for c in provider.contexts] == ["recall", "recall", "main", "recall", "main"]
    assert len(provider.requests[2].payload["messages"]) == 2
    assert all(m["role"] != "tool" for m in provider.requests[-1].payload["messages"])
    # The raw SQLite main history also contains no retrieval calls or evidence.
    import sqlite3
    with closing(sqlite3.connect(memory_cli_conf / "app.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
    Stop()
    for resource in (MemoryMgr, DbMgr, ModelRuntime, TaskMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()


def test_cli_memory_management_and_complete_reply_with_both_kinds(memory_cli_conf, monkeypatch):
    bind_query(monkeypatch)
    Start(memory_cli_conf)
    output = StringIO()
    CLIController.Execute("memory write facts profile 用户喜欢中文", output)
    record = json.loads(output.getvalue())
    assert record["source_id"] == "facts" and record["version"]
    output = StringIO()
    CLIController.Execute("memory search * 中文", output)
    records = json.loads(output.getvalue())["records"]
    assert {r["kind"] for r in records} == {"episodic", "semantic"}
    session = SessionMgr.Create(SessionCreate())
    output = StringIO()
    CLIController.Execute(f"submit {session.session_id} memory: 中文", output)
    task_id = output.getvalue().strip()
    output = StringIO()
    CLIController.Execute(f"wait {task_id}", output)
    assert "用户喜欢中文" in output.getvalue() and "外部知识正文" in output.getvalue()
    output = StringIO()
    CLIController.Execute(f"memory update facts {record['record_id']} {record['version']} 用户喜欢英文", output)
    updated = json.loads(output.getvalue())
    assert updated["version"] != record["version"]
    with pytest.raises(DomainError) as error:
        CLIController.Execute(f"memory delete facts {record['record_id']} {record['version']}", StringIO())
    assert error.value.code == "CONFLICT"
    CLIController.Execute(f"memory delete facts {record['record_id']} {updated['version']}", StringIO())
    output = StringIO()
    CLIController.Execute("memory list facts", output)
    assert json.loads(output.getvalue())["records"] == []


def test_memory_startup_fault_closes_owned_stores_and_can_restart(memory_cli_conf, monkeypatch):
    from funagent import global_server
    from funagent.memory import SQLiteMemoryStore
    bound = []
    def factory(source_id, settings, conf_dir):
        store = SQLiteMemoryStore(conf_dir / settings["path"], source_id)
        bound.append(store)
        return store
    monkeypatch.setitem(global_server.MEMORY_STORES, "sqlite", factory)
    with pytest.raises(DomainError) as error:
        Start(memory_cli_conf)
    assert error.value.code == "DRIVER_UNAVAILABLE"
    assert len(bound) == 1
    with pytest.raises(DomainError) as error:
        bound[0].Search(MemoryQuery(), RunContext())
    assert error.value.code == "NOT_READY"
    for resource in (MemoryMgr, ConfMgr, DbMgr, ModelRuntime, TaskMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()
    bind_query(monkeypatch)
    Start(memory_cli_conf)
