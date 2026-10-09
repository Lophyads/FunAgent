from threading import Event, Thread
import sqlite3
import time
import pytest

from funagent.contracts import DomainError, TurnRequest, ToolResult, HistoryQuery
from funagent.model import ModelError
from funagent.task import TaskMgr
from funagent.session import SessionMgr
from fakes import ScriptedProvider, SpyToolHandler, calls, final


def wait_task(mgr, task_id):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        task = mgr.Get(task_id)
        if task.status not in {"queued", "running"}:
            return task
        Event().wait(0.005)
    pytest.fail("Task did not terminate")


def test_fifo_frozen_prompt_fresh_history_single_user(task_app):
    entered, gate = Event(), Event()
    def block(request, context):
        entered.set()
        assert gate.wait(3)
        from funagent.model import ProviderResponse
        return ProviderResponse(calls("chat", "c1"))
    provider = ScriptedProvider(block, final("chat", "first reply"), final("chat", "second reply"))
    mgr, store, session, spy = task_app(provider)
    try:
        a = mgr.Submit(TurnRequest(session.session_id, "first"))
        assert entered.wait(3)
        b = mgr.Submit(TurnRequest(session.session_id, "second"))
        assert mgr.Get(b).status == "queued"
        frozen = SessionMgr.Load(session.session_id).session.prompt
        assert frozen
        gate.set()
        assert wait_task(mgr, a).result.final == "first reply"
        assert wait_task(mgr, b).result.final == "second reply"
        assert SessionMgr.Load(session.session_id).session.prompt == frozen
        assert [m.content for m in SessionMgr.ReadHistory(session.session_id).items if m.role == "user"] == ["first", "second"]
        messages = provider.requests[-1].payload["messages"]
        assert [m["content"] for m in messages if m["role"] == "user"] == ["first", "second"]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant", "user"]
        assert len(spy.calls) == 1
    finally:
        gate.set()


def test_capacity_includes_running_cancel_waits_for_real_io(task_app):
    entered, gate = Event(), Event()
    def block(args, context):
        entered.set()
        assert gate.wait(3)
        return ToolResult("forged", "ok", "side effect done", "known")
    mgr, store, session, spy = task_app(ScriptedProvider(calls("chat", "c1", "c2"), final("chat")),
                                         handler=SpyToolHandler(block), capacity=1)
    try:
        task_id = mgr.Submit(TurnRequest(session.session_id, "run"))
        assert entered.wait(3)
        assert mgr.Cancel(task_id).requested
        assert mgr.Get(task_id).status == "running"
        with pytest.raises(DomainError) as error:
            mgr.Submit(TurnRequest(session.session_id, "blocked"))
        assert error.value.code == "CAPACITY"
        gate.set()
        assert wait_task(mgr, task_id).status == "cancelled"
        history = store.ReadHistory(session.session_id, HistoryQuery()).items
        assert [(m.status,m.effect) for m in history if m.role == "tool"] == [("ok","known"),("cancelled","none")]
        assert len(spy.calls) == 1
    finally:
        gate.set()


def test_queued_cancel_no_user_and_frees_admission(task_app):
    entered, gate = Event(), Event()
    def block(request, context):
        entered.set()
        assert gate.wait(3)
        from funagent.model import ProviderResponse
        return ProviderResponse(final("chat"))
    mgr, store, session, _ = task_app(ScriptedProvider(block, final("chat")), capacity=2)
    try:
        a = mgr.Submit(TurnRequest(session.session_id, "first"))
        assert entered.wait(3)
        b = mgr.Submit(TurnRequest(session.session_id, "cancel before begin"))
        assert mgr.Cancel(b).status == "cancelled"
        c = mgr.Submit(TurnRequest(session.session_id, "third"))
        assert not mgr.Cancel(b).requested
        gate.set()
        assert wait_task(mgr, a).status == "completed"
        assert wait_task(mgr, c).status == "completed"
        assert [m.content for m in store.ReadHistory(session.session_id, HistoryQuery()).items if m.role == "user"] == ["first", "third"]
    finally:
        gate.set()


def sql(tmp_path, statement):
    with sqlite3.connect(tmp_path / "app.sqlite3") as db:
        return db.execute(statement).fetchall()


def test_tool_batch_is_committed_before_handler(task_app, tmp_path):
    observations = []
    def inspect(args, context):
        observations.append(sql(tmp_path, "SELECT call_id FROM tool_calls ORDER BY rowid"))
        return ToolResult("ignored", "ok", "verified")
    mgr, _, session, spy = task_app(ScriptedProvider(calls("chat", "c1", "c2"), final("chat")), handler=SpyToolHandler(inspect))
    task_id = mgr.Submit(TurnRequest(session.session_id, "tools"))
    assert wait_task(mgr, task_id).status == "completed"
    assert len(spy.calls) == 2
    assert observations == [[("c1",), ("c2",)], [("c1",), ("c2",)]]


def test_failed_tool_batch_commit_never_dispatches(task_app, tmp_path):
    mgr, _, session, spy = task_app(ScriptedProvider(calls("chat", "c1")))
    sql(tmp_path, "CREATE TRIGGER reject_batch BEFORE INSERT ON tool_calls BEGIN SELECT RAISE(ABORT,'fault'); END")
    task_id = mgr.Submit(TurnRequest(session.session_id, "tools"))
    task = wait_task(mgr, task_id)
    assert task.status == "failed" and task.result.error_code == "STORAGE"
    assert not spy.calls and not sql(tmp_path, "SELECT * FROM tool_calls")
    assert len(SessionMgr.ReadHistory(session.session_id).items) == 1


def test_result_write_failure_cannot_complete(task_app, tmp_path):
    mgr, store, session, spy = task_app(ScriptedProvider(calls("chat", "c1"), final("chat")))
    original = store.AppendToolResult
    def fail_once(scope, result):
        store.AppendToolResult = original
        raise DomainError("STORAGE", "injected")
    store.AppendToolResult = fail_once
    task = wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "tools")))
    assert task.status == "failed" and task.result.error_code == "STORAGE"
    assert len(spy.calls) == 1
    history = SessionMgr.ReadHistory(session.session_id).items
    assert [(m.call_id,m.status) for m in history if m.role == "tool"] == [("c1","ok")]
    assert not any(m.role == "assistant" and not m.calls for m in history)


def test_publish_and_register_failure_leave_no_queued_or_capacity(task_app, monkeypatch, tmp_path):
    mgr, store, session, _ = task_app(ScriptedProvider(final("chat")), capacity=1)
    original = mgr._publish
    def fail(task_id):
        original(task_id)
        raise RuntimeError("publish fault after enqueue")
    with monkeypatch.context() as patch:
        patch.setattr(mgr, "_publish", fail)
        with pytest.raises(DomainError):
            mgr.Submit(TurnRequest(session.session_id, "unpublished"))
    assert not sql(tmp_path, "SELECT task_id FROM tasks WHERE status IN ('queued','running')")
    assert not sql(tmp_path, "SELECT * FROM messages")
    with pytest.raises(DomainError) as error:
        mgr.Submit(TurnRequest("missing", "invalid"))
    assert error.value.code == "NOT_FOUND"
    assert wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "works"))).status == "completed"


def test_stop_stops_admission_and_waits_before_resource_close(task_app):
    entered, gate, stopped = Event(), Event(), Event()
    def block(request, context):
        entered.set()
        assert gate.wait(3)
        assert not provider.closed
        from funagent.model import ProviderResponse
        return ProviderResponse(final("chat"))
    provider = ScriptedProvider(block)
    mgr, _, session, _ = task_app(provider)
    task_id = mgr.Submit(TurnRequest(session.session_id, "running"))
    assert entered.wait(3)
    def stop():
        mgr.Stop()
        stopped.set()
    thread = Thread(target=stop)
    thread.start()
    try:
        # Cancel's event is the deterministic acknowledgement of Stop beginning.
        assert provider.contexts[0].cancel.wait(3)
        assert not stopped.is_set()
        with pytest.raises(DomainError) as error:
            mgr.Submit(TurnRequest(session.session_id, "late"))
        assert error.value.code == "STOPPING"
        gate.set()
        assert stopped.wait(3)
        assert mgr.Get(task_id).status == "cancelled"
    finally:
        gate.set()
        thread.join(3)


def test_failure_survives_worker_and_startup_repairs(task_app):
    mgr, store, session, _ = task_app(ScriptedProvider(ModelError("AUTH"), final("chat")))
    first = mgr.Submit(TurnRequest(session.session_id, "fails"))
    assert wait_task(mgr, first).status == "failed"
    assert wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "next"))).status == "completed"
    TaskMgr.Close()
    orphan = store.RegisterTurn(TurnRequest(session.session_id, "orphan"))
    TaskMgr.Initialize(2)
    assert TaskMgr.GetInstance().Get(orphan.task_id).status == "interrupted"



def test_final_commit_failure_is_failed_without_reply_then_worker_continues(task_app, tmp_path):
    mgr, _, session, _ = task_app(ScriptedProvider(final("chat", "rollback"), final("chat", "next")))
    sql(tmp_path, "CREATE TRIGGER reject_completed BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'fault'); END")
    task = wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "first")))
    assert task.status == "failed" and task.result.error_code == "STORAGE"
    assert [m.role for m in SessionMgr.ReadHistory(session.session_id).items] == ["user"]
    sql(tmp_path, "DROP TRIGGER reject_completed")
    assert wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "second"))).result.final == "next"


def test_observed_commit_wins_over_late_cancel_and_driver_error(task_app):
    mgr, store, session, _ = task_app(ScriptedProvider(final("chat", "committed"), final("chat", "next")), capacity=1)
    original = store.FinishTurn
    def commit_then_error(task_id, result):
        store.FinishTurn = original
        actual = original(task_id, result)
        mgr.Cancel(task_id)
        raise DomainError("STORAGE", "late acknowledgement failure")
    store.FinishTurn = commit_then_error
    first = wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "first")))
    assert first.status == "completed" and first.result.final == "committed"
    # Wait until worker returns from acknowledgement handling, using its public
    # admission result; no new IO can start before that worker returns.
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        try:
            second = mgr.Submit(TurnRequest(session.session_id, "second"))
            break
        except DomainError as error:
            assert error.code == "CAPACITY"
            Event().wait(0.005)
    else:
        pytest.fail("Completed slot was not released")
    assert wait_task(mgr, second).status == "completed"


def test_ephemeral_does_not_write_main_history_with_database_ready(task_app, tmp_path):
    from funagent.model import ModelRuntime
    from funagent.contracts import RunContext, RunInput, Message
    from fakes import MainStrategy
    mgr, store, session, _ = task_app(ScriptedProvider(final("chat"), calls("chat", "temporary"), final("chat")))
    assert wait_task(mgr, mgr.Submit(TurnRequest(session.session_id,"persistent"))).status == "completed"
    before = sql(tmp_path, "SELECT payload FROM messages ORDER BY id")
    result = ModelRuntime.GetInstance().Run(RunInput((Message("user","ephemeral"),)), MainStrategy, RunContext())
    assert result.status == "completed" and len(result.tool_results)==1
    assert sql(tmp_path,"SELECT payload FROM messages ORDER BY id") == before


def test_persistent_iteration_limit_has_pairs_and_no_synthetic_reply(task_app):
    provider=ScriptedProvider(*(calls("chat",f"c{i}") for i in range(6)))
    mgr, _, session, spy = task_app(provider)
    task=wait_task(mgr,mgr.Submit(TurnRequest(session.session_id,"loop")))
    assert task.status=="failed" and task.result.status=="limit" and task.result.reason=="MAX_ITERATIONS"
    assert len(spy.calls)==6 and len(provider.requests)==6
    history=SessionMgr.ReadHistory(session.session_id).items
    assert len(history)==13 and sum(m.role=="tool" for m in history)==6
    assert all(m.calls for m in history if m.role=="assistant")
