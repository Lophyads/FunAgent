"""P3 production CLI, SQLite, human replies, and worker ownership."""
from io import StringIO
from pathlib import Path
from threading import Event, Thread
import sqlite3
import time
import pytest

from funagent.api.cli import CLIController
from funagent.conf import ConfMgr
from funagent.contracts import DomainError, SessionCreate, TurnRequest, ToolResult
from funagent.global_server import Start, Stop
from funagent.interaction import InteractionMgr
from funagent.persistence import DbMgr
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from funagent.tool import ToolMgr, ToolFailure
from fakes import ChatAdapter, ScriptedProvider, SpyToolHandler, final
from test_task_flow import wait_task



@pytest.fixture
def p3_conf(cli_conf, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("real file contents", encoding="utf8")
    serv = cli_conf / "serv.conf"
    serv.write_text(serv.read_text().replace('workspace = "."', f'workspace = "{workspace.as_posix()}"').replace("interaction_timeout = 30.0", "interaction_timeout = 20.0"), encoding="utf8")
    return cli_conf


def bind_scripted(conf, provider, monkeypatch):
    from funagent import global_server
    monkeypatch.setitem(global_server.PROVIDERS, "demo", lambda settings: provider)
    monkeypatch.setitem(global_server.ADAPTERS, "demo", ChatAdapter)
    mgr = Start(conf)
    workspace = ConfMgr.GetInstance().Serv().Options()["workspace"]
    session = SessionMgr.Create(SessionCreate(workspace=workspace))
    return mgr, session


def pending(mgr, task_id):
    end = time.perf_counter() + 3
    while time.perf_counter() < end:
        requests = InteractionMgr.GetInstance().Pending(task_id)
        if requests:
            return requests[0]
        assert mgr.Get(task_id).status in {"queued", "running"}
        Event().wait(0.005)
    pytest.fail("No pending interaction")


def batch(*invocations):
    return {"calls": [{"id": key, "name": name, "args": args} for key, name, args in invocations], "finish": "tool_calls"}


def test_demo_cli_wait_answers_approval_and_clarification_same_turn(p3_conf):
    Start(p3_conf)
    output = StringIO()
    CLIController.Execute("new P3", output)
    session_id = output.getvalue().strip()
    for text, answer, kind, expected in [("read: note.txt", "yes", "approval", "real file contents"),
                                         ("ask: Which file?", "note.txt", "ask_user", "note.txt")]:
        output = StringIO()
        CLIController.Execute(f"submit {session_id} {text}", output)
        task_id = output.getvalue().strip()
        output = StringIO()
        def respond(prompt):
            assert kind in output.getvalue()
            return answer
        CLIController.Execute(f"wait {task_id}", output, read_answer=respond)
        task = TaskMgr.GetInstance().Get(task_id)
        assert task.status == "completed" and task.result.final == expected
        assert expected in output.getvalue()
        assert InteractionMgr.GetInstance().Pending(task_id) == ()
    history = SessionMgr.ReadHistory(session_id).items
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"] * 2
    assert len([m for m in history if m.role == "user"]) == 2
    assert [m.status for m in history if m.role == "tool"] == ["ok", "ok"]


def test_manual_answer_bypasses_full_queue_without_new_turn(p3_conf, monkeypatch):
    serv = p3_conf / "serv.conf"
    serv.write_text(serv.read_text().replace("capacity = 16", "capacity = 2"))
    provider = ScriptedProvider(batch(("ask", "ask_user", {"prompt": "Which?"})), final("chat", "answer accepted"), final("chat", "next"))
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    first = mgr.Submit(TurnRequest(session.session_id, "first"))
    request = pending(mgr, first)
    second = mgr.Submit(TurnRequest(session.session_id, "second"))
    assert mgr.Get(first).status == "running" and mgr.Get(second).status == "queued"
    with pytest.raises(DomainError) as error:
        mgr.Submit(TurnRequest(session.session_id, "queue is full"))
    assert error.value.code == "CAPACITY"
    with sqlite3.connect(p3_conf / "app.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    output = StringIO()
    CLIController.Execute(f"pending {first}", output)
    assert request.interaction_id in output.getvalue() and "Which?" in output.getvalue()
    CLIController.Execute(f"answer {first} {request.interaction_id} chosen file", output)
    assert wait_task(mgr, first).result.final == "answer accepted"
    assert wait_task(mgr, second).result.final == "next"
    with sqlite3.connect(p3_conf / "app.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert [m.content for m in SessionMgr.ReadHistory(session.session_id).items if m.role == "user"] == ["first", "second"]
    assert provider.requests[1].payload["messages"][-1]["content"] == "chosen file"
    with pytest.raises(DomainError) as error:
        CLIController.Execute(f"answer {first} {request.interaction_id} duplicate", output)
    assert error.value.code == "INTERACTION_STALE"


def test_declined_approval_never_reads_and_cli_retries_invalid_answer(p3_conf, monkeypatch):
    provider = ScriptedProvider(batch(("read", "read_workspace", {"path": "note.txt"})), final("chat", "declined"))
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    opened = []
    original = Path.open
    def observe(self, *args, **kwargs):
        opened.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observe)
    task_id = mgr.Submit(TurnRequest(session.session_id, "read"))
    answers = iter(["maybe", "no"])
    output = StringIO()
    CLIController.Execute(f"wait {task_id}", output, read_answer=lambda prompt: next(answers))
    assert "INVALID_REQUEST" in output.getvalue()
    assert mgr.Get(task_id).result.tool_results[0].status == "denied"
    assert not opened


def test_cancel_human_wait_releases_original_worker_and_pairs_history(p3_conf, monkeypatch):
    provider = ScriptedProvider(batch(("ask", "ask_user", {"prompt": "Wait?"}), ("later", "echo", {"value": "never"})), final("chat", "next task"))
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    first = mgr.Submit(TurnRequest(session.session_id, "first"))
    request = pending(mgr, first)
    second = mgr.Submit(TurnRequest(session.session_id, "second"))
    assert mgr.Cancel(first).requested
    assert wait_task(mgr, first).status == "cancelled"
    assert wait_task(mgr, second).result.final == "next task"
    results = [m for m in SessionMgr.ReadHistory(session.session_id).items if m.role == "tool"]
    assert [(m.call_id, m.status, m.effect) for m in results] == [("ask", "cancelled", "none"), ("later", "cancelled", "none")]
    with pytest.raises(DomainError):
        InteractionMgr.GetInstance().Resolve(request.interaction_id, "late", task_id=first, run_id=request.run_id)


def test_pending_expiry_is_ordinary_result_and_no_new_turn(p3_conf, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("funagent.interaction.time.monotonic", lambda: now[0])
    provider = ScriptedProvider(batch(("ask", "ask_user", {"prompt": "Wait?"})), final("chat", "expired but continued"))
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    task_id = mgr.Submit(TurnRequest(session.session_id, "first"))
    request = pending(mgr, task_id)
    now[0] = request.expires_at + 1
    assert InteractionMgr.GetInstance().Pending(task_id) == ()
    task = wait_task(mgr, task_id)
    assert task.status == "completed" and task.result.tool_results[0].status == "error"
    assert task.result.tool_results[0].effect == "none"
    assert [m.role for m in SessionMgr.ReadHistory(session.session_id).items] == ["user", "assistant", "tool", "assistant"]


def test_deterministic_batch_pairs_failures_and_model_retry_never_replays(p3_conf, monkeypatch):
    from funagent import global_server
    from funagent.model import ModelError
    tools = (p3_conf / "tool.conf").read_text()
    tools = tools.replace('[purposes]', '[definitions.blocked]\nhandler = "echo"\n[definitions.blocked.parameters]\ntype = "object"\n[definitions.fault]\nhandler = "fault"\n[definitions.fault.parameters]\ntype = "object"\n\n[purposes]').replace('main = ["echo", "read_workspace", "ask_user"]', 'main = ["echo", "read_workspace", "ask_user", "fault"]')
    (p3_conf / "tool.conf").write_text(tools)
    model = p3_conf / "model.conf"
    model.write_text(model.read_text().replace('max_retries = 0', 'max_retries = 1'))
    order = []
    def fail(args, context):
        order.append("fault")
        raise ToolFailure("FAILED", effect="none")
    monkeypatch.setitem(global_server.HANDLERS, "fault", SpyToolHandler(fail))
    provider = ScriptedProvider(batch(("unknown", "missing", {}), ("blocked", "blocked", {}), ("fault", "fault", {}),
                                     ("read", "read_workspace", {"path": "note.txt"})), ModelError("TRANSPORT"), final("chat", "continued"))
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    original = Path.open
    def observe(self, *args, **kwargs):
        order.append("read")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observe)
    task_id = mgr.Submit(TurnRequest(session.session_id, "batch"))
    CLIController.Execute(f"wait {task_id}", StringIO(), read_answer=lambda prompt: "yes")
    task = mgr.Get(task_id)
    assert task.status == "completed" and task.result.final == "continued"
    assert [(r.call_id, r.status) for r in task.result.tool_results] == [("unknown", "invalid"), ("blocked", "denied"), ("fault", "error"), ("read", "ok")]
    assert order == ["fault", "read"] and len(provider.requests) == 3
    history = SessionMgr.ReadHistory(session.session_id).items
    assert [m.call_id for m in history if m.role == "tool"] == ["unknown", "blocked", "fault", "read"]


def test_shutdown_waits_for_actual_tool_io_before_next_task_or_close(p3_conf, monkeypatch):
    from funagent import global_server
    entered, gate, stopped = Event(), Event(), Event()
    captured = []
    def block(args, context):
        captured.append(context)
        entered.set()
        assert gate.wait(3)
        assert provider.closed == 0
        ToolMgr.GetInstance()
        DbMgr.GetInstance()
        return ToolResult("ignored", "ok", "effect committed", "known")
    monkeypatch.setitem(global_server.HANDLERS, "echo", SpyToolHandler(block))
    provider = ScriptedProvider(batch(("blocked", "echo", {"value": "side effect"}), ("never", "echo", {})), final("chat"))
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    first = mgr.Submit(TurnRequest(session.session_id, "first"))
    assert entered.wait(3)
    second = mgr.Submit(TurnRequest(session.session_id, "second"))
    failures = []
    def stop():
        try:
            Stop()
        except Exception as error:
            failures.append(error)
        finally:
            stopped.set()
    thread = Thread(target=stop)
    thread.start()
    try:
        assert captured[0].cancel.wait(3)
        assert not stopped.is_set() and provider.closed == 0 and len(provider.requests) == 1
        assert mgr.Get(first).status == "running"
        with pytest.raises(DomainError) as error:
            mgr.Submit(TurnRequest(session.session_id, "late"))
        assert error.value.code == "STOPPING"
        gate.set()
        assert stopped.wait(3) and not failures
        assert provider.closed == 1
    finally:
        gate.set()
        thread.join(3)
    from funagent.persistence import SQLiteStore
    store = SQLiteStore(p3_conf / "app.sqlite3")
    try:
        assert store.GetTask(first).status == store.GetTask(second).status == "cancelled"
        assert [(r.status, r.effect) for r in store.GetTask(first).result.tool_results] == [("ok", "known"), ("cancelled", "none")]
    finally:
        store.Close()


def test_session_workspace_is_frozen_before_cwd_changes(p3_conf, tmp_path, monkeypatch):
    first_root, other_root = tmp_path / "first-root", tmp_path / "other-root"
    first_root.mkdir()
    other_root.mkdir()
    (first_root / "note.txt").write_text("original workspace", encoding="utf8")
    (other_root / "note.txt").write_text("wrong workspace", encoding="utf8")
    serv = p3_conf / "serv.conf"
    text = serv.read_text()
    workspace = str(tmp_path / "workspace")
    serv.write_text(text.replace(f'workspace = "{Path(workspace).as_posix()}"', 'workspace = "."'))
    monkeypatch.chdir(first_root)
    Start(p3_conf)
    session = SessionMgr.Create(SessionCreate(workspace="."))
    monkeypatch.chdir(other_root)
    task_id = TaskMgr.GetInstance().Submit(TurnRequest(session.session_id, "read: note.txt"))
    CLIController.Execute(f"wait {task_id}", StringIO(), read_answer=lambda prompt: "yes")
    assert TaskMgr.GetInstance().Get(task_id).result.final == "original workspace"
    assert Path(SessionMgr.Load(session.session_id).session.workspace) == first_root


def test_run_deadline_does_not_release_running_tool_before_return(p3_conf, monkeypatch):
    from funagent import global_server
    now = [100.0]
    monkeypatch.setattr("funagent.interaction.time.monotonic", lambda: now[0])
    entered, gate, next_entered = Event(), Event(), Event()
    seen = []
    def block(args, context):
        seen.append(context)
        entered.set()
        assert gate.wait(3)
        try:
            context.CheckCancelled()
        except DomainError:
            raise ToolFailure("TIMEOUT", effect="none")
        return ToolResult("", "ok", "read")
    monkeypatch.setitem(global_server.HANDLERS, "echo", SpyToolHandler(block))
    def next_request(request, context):
        from funagent.model import ProviderResponse
        next_entered.set()
        return ProviderResponse(final("chat", "next"))
    provider = ScriptedProvider(batch(("one", "echo", {}), ("never", "echo", {})), next_request)
    mgr, session = bind_scripted(p3_conf, provider, monkeypatch)
    first = mgr.Submit(TurnRequest(session.session_id, "first"))
    assert entered.wait(3)
    second = mgr.Submit(TurnRequest(session.session_id, "second"))
    try:
        assert seen[0].deadline == 160.0
        now[0] = 161.0
        assert mgr.Get(first).status == "running" and mgr.Get(second).status == "queued"
        assert not next_entered.is_set() and provider.closed == 0
        gate.set()
        assert wait_task(mgr, first).result.reason == "DEADLINE"
        assert next_entered.wait(3) and wait_task(mgr, second).result.final == "next"
        assert len(seen) == 1
        assert [(r.status, r.effect) for r in mgr.Get(first).result.tool_results] == [("error", "none"), ("cancelled", "none")]
    finally:
        gate.set()


@pytest.mark.parametrize("answer", ["/cancel", None])
def test_cli_cancel_or_input_eof_wakes_human_wait(p3_conf, answer):
    Start(p3_conf)
    session = SessionMgr.Create(SessionCreate(workspace=ConfMgr.GetInstance().Serv().Options()["workspace"]))
    task_id = TaskMgr.GetInstance().Submit(TurnRequest(session.session_id, "ask: question"))
    def respond(prompt):
        if answer is None:
            raise EOFError
        return answer
    CLIController.Execute(f"wait {task_id}", StringIO(), read_answer=respond)
    assert TaskMgr.GetInstance().Get(task_id).status == "cancelled"
    assert InteractionMgr.GetInstance().Pending(task_id) == ()


def test_stop_wakes_pending_without_an_answer_and_cleans_all_resources(p3_conf):
    Start(p3_conf)
    session = SessionMgr.Create(SessionCreate(workspace=ConfMgr.GetInstance().Serv().Options()["workspace"]))
    mgr = TaskMgr.GetInstance()
    task_id = mgr.Submit(TurnRequest(session.session_id, "ask: question"))
    pending(mgr, task_id)
    stopped, failures = Event(), []
    def stop():
        try:
            Stop()
        except Exception as error:
            failures.append(error)
        finally:
            stopped.set()
    thread = Thread(target=stop)
    thread.start()
    try:
        assert stopped.wait(3) and not failures
    finally:
        InteractionMgr.CancelTask(task_id)
        thread.join(3)
    for resource in (ConfMgr, TaskMgr, InteractionMgr, ToolMgr, DbMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()


def test_interaction_startup_failure_closes_new_state_and_provider_once(p3_conf, monkeypatch):
    from funagent import global_server
    from funagent.model import ModelRuntime
    provider = ScriptedProvider()
    monkeypatch.setitem(global_server.PROVIDERS, "demo", lambda settings: provider)
    original = InteractionMgr.Initialize
    with monkeypatch.context() as patch:
        def fail(timeout=120):
            original(timeout)
            raise RuntimeError("SECRET_TOKEN")
        patch.setattr(InteractionMgr, "Initialize", fail)
        with pytest.raises(DomainError) as error:
            Start(p3_conf)
        assert error.value.code == "STARTUP_FAILED" and "SECRET_TOKEN" not in str(error.value)
    assert provider.closed == 1
    for resource in (ConfMgr, ToolMgr, ModelRuntime, TaskMgr, InteractionMgr, DbMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()
    monkeypatch.setitem(global_server.PROVIDERS, "demo", global_server.DemoProvider)
    Start(p3_conf)
    Stop()
