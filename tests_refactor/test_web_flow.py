"""P6 production startup, real HTTP, SQLite, CLI, human tools and memory workers."""
from io import StringIO
from pathlib import Path
from threading import Event, enumerate as threads
import json
import time
import pytest

from funagent import global_server
from funagent.api.cli import CLIController
from funagent.api.http import HttpServer
from funagent.conf import ConfMgr
from funagent.contracts import DomainError, RunContext
from funagent.interaction import InteractionMgr
from funagent.memory import MemoryMgr, MemoryListQuery
from funagent.model import ModelError, ModelRuntime
from funagent.persistence import DbMgr
from funagent.review import ReviewScheduler
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from funagent.tool import ToolMgr
from fakes import ScriptedProvider, ChatAdapter, final
from test_http import request
from test_cli_interaction import p3_conf, pending
from test_task_flow import wait_task


@pytest.fixture
def web_app(p3_conf, monkeypatch):
    servers = []
    def server(host, port):
        resource = HttpServer(host, 0)
        servers.append(resource)
        return resource
    monkeypatch.setattr(global_server, "HttpServer", server)
    def start(mode="web", provider=None):
        serv = p3_conf / "serv.conf"
        serv.write_text(serv.read_text().replace('mode = "cli"', f'mode = "{mode}"'))
        if provider is not None:
            monkeypatch.setitem(global_server.PROVIDERS, "demo", lambda settings: provider)
            monkeypatch.setitem(global_server.ADAPTERS, "demo", ChatAdapter)
        manager = global_server.Start(p3_conf)
        return manager, servers[-1] if servers else None
    return start


def create(server, text=None):
    status, session, _ = request(server, "POST", "/sessions", {"title": "P6 会话"})
    assert status == 201
    session_id = session["session_id"]
    if text is None:
        return session_id
    status, task, _ = request(server, "POST", f"/sessions/{session_id}/tasks", {"text": text})
    assert status == 202
    return session_id, task["task_id"]


def answer(server, interaction, value, **overrides):
    body = {"task_id": interaction.task_id, "run_id": interaction.run_id, "answer": value, **overrides}
    return request(server, "POST", f"/interactions/{interaction.interaction_id}/answer", body)


@pytest.mark.parametrize("mode", ["cli", "web", "both"])
def test_modes_share_managers_and_only_select_access_resources(web_app, mode):
    mgr, server = web_app(mode)
    assert mgr is TaskMgr.GetInstance()
    output = StringIO()
    CLIController.Execute("new shared", output)
    session_id = output.getvalue().strip()
    if mode == "cli":
        assert server is None
        CLIController.Execute(f"submit {session_id} hello", output)
        task_id = output.getvalue().splitlines()[-1]
    else:
        sessions = request(server, "GET", "/sessions")[1]["items"]
        assert any(s["session_id"] == session_id for s in sessions)
        task_id = request(server, "POST", f"/sessions/{session_id}/tasks", {"text": "tool: shared tool"})[1]["task_id"]
    task = wait_task(mgr, task_id)
    assert task.status == "completed"
    if server:
        out = StringIO()
        CLIController.Execute(f"get {task_id}", out)
        assert task.result.final in out.getvalue()
        assert request(server, "GET", "/tasks/" + task_id)[1]["result"]["final"] == task.result.final
    global_server.Stop()
    assert not any(t.name.startswith("funagent-") for t in threads())


@pytest.mark.parametrize("text,value,expected,kind", [
    ("ask: 请选择颜色", "蓝色", "蓝色", "ask_user"),
    ("read: note.txt", True, "real file contents", "approval"),
    ("read: note.txt", False, "Approval declined", "approval"),
])
def test_browser_answers_resume_original_turn_with_tool_history(web_app, text, value, expected, kind):
    mgr, server = web_app()
    session_id, task_id = create(server, text)
    interaction = pending(mgr, task_id)
    status, values, _ = request(server, "GET", f"/tasks/{task_id}/interactions")
    assert status == 200 and values["items"][0]["kind"] == kind
    status, result, _ = answer(server, interaction, value)
    assert status == 200 and result == {"answered": True}
    task = wait_task(mgr, task_id)
    assert task.status == "completed" and expected in task.result.final
    history = request(server, "GET", f"/sessions/{session_id}/messages?limit=1")[1]
    assert [m["role"] for m in history["items"]] == ["user", "assistant", "tool", "assistant"]
    assert sum(m["role"] == "user" for m in history["items"]) == 1
    assert history["items"][1]["calls"][0]["call_id"] == history["items"][2]["call_id"]
    assert request(server, "GET", f"/tasks/{task_id}/interactions")[1]["items"] == []
    assert answer(server, interaction, value)[0] == 409


def test_wrong_run_task_answer_type_and_privileges_cannot_resolve(web_app):
    mgr, server = web_app()
    _, task_id = create(server, "read: note.txt")
    interaction = pending(mgr, task_id)
    _, other = create(server, "other")
    for overrides, value, code in [({"run_id": "other"}, True, 409), ({"task_id": other}, True, 409),
        ({}, "yes", 400), ({"access": ["admin"]}, True, 400), ({"handler": "read_workspace"}, True, 400)]:
        assert answer(server, interaction, value, **overrides)[0] == code
        assert InteractionMgr.GetInstance().Pending(task_id)
    assert answer(server, interaction, True)[0] == 200
    assert wait_task(mgr, task_id).result.final == "real file contents"
    assert wait_task(mgr, other).status == "completed"


def test_cancel_wakes_browser_pending_and_queued_turn_never_enters_history(web_app):
    mgr, server = web_app()
    session_id, task_id = create(server, "ask: waiting")
    interaction = pending(mgr, task_id)
    second = request(server, "POST", f"/sessions/{session_id}/tasks", {"text": "queued"})[1]["task_id"]
    result = request(server, "POST", f"/tasks/{second}/cancel", {})
    assert result[0] == 200 and result[1]["status"] == "cancelled"
    assert request(server, "POST", f"/tasks/{task_id}/cancel", {})[1]["requested"]
    assert wait_task(mgr, task_id).status == "cancelled"
    assert answer(server, interaction, "late")[0] == 409
    assert request(server, "GET", f"/tasks/{task_id}/interactions")[1]["items"] == []
    history = request(server, "GET", f"/sessions/{session_id}/messages")[1]["items"]
    assert [m["content"] for m in history if m["role"] == "user"] == ["ask: waiting"]
    assert request(server, "GET", f"/tasks/{task_id}")[1]["result"]["reason"] == "CANCELLED"


def test_expired_browser_answer_is_stale_and_has_no_side_effect(web_app, monkeypatch):
    now = [time.monotonic()]
    monkeypatch.setattr("funagent.interaction.time.monotonic", lambda: now[0])
    mgr, server = web_app()
    _, task_id = create(server, "read: note.txt")
    interaction = pending(mgr, task_id)
    now[0] = interaction.expires_at + 1
    status, value, _ = answer(server, interaction, True)
    assert status == 409 and value["error"]["code"] == "INTERACTION_STALE"
    task = wait_task(mgr, task_id)
    assert task.result.tool_results[0].status == "error" and task.result.tool_results[0].effect == "none"


def test_failed_task_result_is_public_and_history_is_readable(web_app):
    provider = ScriptedProvider(ModelError("AUTH"))
    mgr, server = web_app(provider=provider)
    session_id, task_id = create(server, "failure")
    assert wait_task(mgr, task_id).status == "failed"
    status, snapshot, _ = request(server, "GET", f"/tasks/{task_id}")
    assert status == 200 and snapshot["status"] == "failed" and snapshot["result"]["error_code"] == "AUTH"
    assert request(server, "GET", f"/sessions/{session_id}/messages")[1]["items"][0]["content"] == "failure"


def test_web_recall_and_review_keep_background_text_out_of_main_history(web_app, p3_conf, monkeypatch):
    source = Path(__file__).parents[1] / "examples/refactor-review/memory.conf"
    (p3_conf / "memory.conf").write_text(source.read_text())
    # Observe actual worker completion without arbitrary timing sleeps.
    reviewed = Event()
    from funagent.review import ReviewProcess
    original = ReviewProcess.Run
    def run(*args):
        try:
            return original(*args)
        finally:
            reviewed.set()
    monkeypatch.setattr(ReviewProcess, "Run", run)
    mgr, server = web_app()
    session_id, first = create(server, "remember: 我喜欢蓝色")
    assert wait_task(mgr, first).status == "completed" and reviewed.wait(3)
    records = MemoryMgr.GetInstance().List(MemoryListQuery("facts"), RunContext(access=frozenset({"memory.facts.read"})))
    assert [r.content for r in records.items] == ["我喜欢蓝色"]
    task_id = request(server, "POST", f"/sessions/{session_id}/tasks", {"text": "memory: 蓝色"})[1]["task_id"]
    task = wait_task(mgr, task_id)
    assert "我喜欢蓝色" in task.result.final
    history = request(server, "GET", f"/sessions/{session_id}/messages")[1]["items"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert "Review complete" not in json.dumps(history) and "Retrieval complete" not in json.dumps(history)
    assert SessionMgr.Load(session_id).session.prompt not in json.dumps(request(server, "GET", "/sessions")[1])


@pytest.mark.parametrize("fault", ["bind", "thread"])
def test_http_start_failure_releases_every_resource_then_can_restart(web_app, monkeypatch, fault):
    with monkeypatch.context() as patch:
        if fault == "bind":
            patch.setattr(global_server, "HttpServer", lambda *args: (_ for _ in ()).throw(OSError("SECRET_TOKEN")))
        else:
            patch.setattr(HttpServer, "Start", lambda self: (_ for _ in ()).throw(RuntimeError("SECRET_TOKEN")))
        with pytest.raises(DomainError) as error:
            web_app()
        assert error.value.code == "STARTUP_FAILED" and "SECRET_TOKEN" not in str(error.value)
    for resource in (ConfMgr, ModelRuntime, DbMgr, MemoryMgr, InteractionMgr, TaskMgr, ToolMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()
    assert not any(t.name.startswith("funagent-") for t in threads())
    mgr, server = web_app()
    assert request(server, "GET", "/sessions")[0] == 200


@pytest.mark.parametrize("mode", ["web", "both"])
def test_main_selects_web_wait_or_cli_and_closes_listener(web_app, p3_conf, monkeypatch, capsys, mode):
    # web_app supplies the ephemeral server factory; main owns orchestration.
    serv = p3_conf / "serv.conf"
    serv.write_text(serv.read_text().replace('mode = "cli"', f'mode = "{mode}"'))
    called = []
    def wait(self):
        called.append("web")
        assert request(self, "GET", "/sessions")[0] == 200
    def cli():
        called.append("cli")
        return 0
    monkeypatch.setattr(HttpServer, "Wait", wait)
    monkeypatch.setattr(CLIController, "Run", cli)
    assert global_server.main(["--conf-dir", str(p3_conf)]) == 0
    assert called == (["web"] if mode == "web" else ["cli"])
    assert "http://127.0.0.1:" in capsys.readouterr().out
    assert not any(t.name.startswith("funagent-") for t in threads())
