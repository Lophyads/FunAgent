import pytest
from funagent.contracts import (DomainError, SessionCreate, TurnRequest)
from funagent.persistence import SQLiteStore


@pytest.fixture
def store(tmp_path):
    instance = SQLiteStore(tmp_path / "app.sqlite3")
    try:
        yield instance
    finally:
        instance.Close()


def test_freeze_prompt_idempotent_conflict_and_no_cache(store):
    session = store.CreateSession(SessionCreate("first", "/workspace"))
    assert session.prompt is None
    assert store.FreezePrompt(session.session_id, "fixed") == "fixed"
    assert store.FreezePrompt(session.session_id, "fixed") == "fixed"
    with pytest.raises(DomainError) as error:
        store.FreezePrompt(session.session_id, "changed")
    assert error.value.code == "CONFLICT"
    assert store.LoadSession(session.session_id).session.prompt == "fixed"


def test_begin_once_snapshot_contains_current_user(store):
    session = store.CreateSession(SessionCreate())
    task = store.RegisterTurn(TurnRequest(session.session_id, "hello"))
    assert store.GetTask(task.task_id).status == "queued"
    snapshot = store.BeginTurn(task.task_id)
    assert store.GetTask(task.task_id).status == "running"
    assert [(m.role, m.content) for m in snapshot.messages] == [("user", "hello")]
    with pytest.raises(DomainError) as error:
        store.BeginTurn(task.task_id)
    assert error.value.code == "CONFLICT"
    assert len(store.LoadSession(session.session_id).messages) == 1


import sqlite3
from funagent.contracts import (RecordScope, Message, ToolCall, ToolResult,
                               RunResult, PageQuery, HistoryQuery)


def started(store, text="hello", session=None):
    session = session or store.CreateSession(SessionCreate())
    task = store.RegisterTurn(TurnRequest(session.session_id, text))
    store.BeginTurn(task.task_id)
    return session, task, RecordScope("main_history", task.task_id)


def sql(tmp_path, statement, parameters=()):
    with sqlite3.connect(tmp_path / "app.sqlite3") as connection:
        return connection.execute(statement, parameters).fetchall()


def batch():
    return Message("assistant", "working", (ToolCall("c1", "echo"), ToolCall("c2", "echo")))


def test_full_batch_durable_and_unique_pairing(store, tmp_path):
    session, task, scope = started(store)
    store.AppendToolCalls(scope, batch())
    persisted = sql(tmp_path, "SELECT payload FROM messages ORDER BY id")
    assert 'c1' in persisted[-1][0] and 'c2' in persisted[-1][0]
    with pytest.raises(DomainError) as error:
        store.AppendToolCalls(scope, batch())
    assert error.value.code == "CONFLICT"
    for key in ("c1", "c2"):
        store.AppendToolResult(scope, ToolResult(key, "ok", key, "known"))
    with pytest.raises(DomainError):
        store.AppendToolResult(scope, ToolResult("c1", "ok", "duplicate"))
    with pytest.raises(DomainError):
        store.AppendToolResult(scope, ToolResult("missing", "ok", "unpaired"))
    store.FinishTurn(task.task_id, RunResult("completed", final="complete"))
    history = store.ReadHistory(session.session_id, HistoryQuery()).items
    assert [m.role for m in history] == ["user", "assistant", "tool", "tool", "assistant"]
    assert [m.call_id for m in history if m.role == "tool"] == ["c1", "c2"]


def test_finish_final_and_completed_rollback_together(store, tmp_path):
    session, task, _ = started(store)
    sql(tmp_path, "CREATE TRIGGER reject_completed BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'fault'); END")
    with pytest.raises(DomainError) as error:
        store.FinishTurn(task.task_id, RunResult("completed", final="must rollback"))
    assert error.value.code == "STORAGE"
    assert store.GetTask(task.task_id).status == "running"
    assert [m.role for m in store.LoadSession(session.session_id).messages] == ["user"]
    sql(tmp_path, "DROP TRIGGER reject_completed")
    done = store.FinishTurn(task.task_id, RunResult("completed", final="complete"))
    assert done.status == "completed" and done.result.final == "complete"
    with pytest.raises(DomainError):
        store.FinishTurn(task.task_id, RunResult("completed", final="duplicate"))


def test_result_insert_failure_is_rolled_back(store, tmp_path):
    _, task, scope = started(store)
    store.AppendToolCalls(scope, batch())
    sql(tmp_path, "CREATE TRIGGER reject_tool BEFORE INSERT ON messages "
        "WHEN json_extract(NEW.payload,'$.role')='tool' BEGIN SELECT RAISE(ABORT, 'fault'); END")
    with pytest.raises(DomainError):
        store.AppendToolResult(scope, ToolResult("c1", "ok", "executed", "known"))
    assert store.GetTask(task.task_id).status == "running"
    assert not sql(tmp_path, "SELECT result_message_id FROM tool_calls WHERE result_message_id IS NOT NULL")


@pytest.mark.parametrize("status", ["failed", "limit", "cancelled"])
def test_terminal_failure_pairs_remaining_tools_without_final(store, status):
    session, task, scope = started(store)
    store.AppendToolCalls(scope, batch())
    result = RunResult(status, reason="test", tool_results=(
        ToolResult("c1", "ok", "ran", "known"), ToolResult("c2", "cancelled", "Tool not executed")))
    task = store.FinishTurn(task.task_id, result)
    assert task.status == ("failed" if status == "limit" else status)
    history = store.ReadHistory(session.session_id, HistoryQuery()).items
    assert [m.status for m in history if m.role == "tool"] == ["ok", "cancelled"]
    assert not any(m.role == "assistant" and not m.calls for m in history)


def test_repair_unknown_effects_no_replay_and_queued_no_user(store, tmp_path):
    session, task, scope = started(store)
    store.AppendToolCalls(scope, batch())
    store.AppendToolResult(scope, ToolResult("c1", "ok", "known", "known"))
    queued = store.RegisterTurn(TurnRequest(session.session_id, "never began"))
    store.Close()
    reopened = SQLiteStore(tmp_path / "app.sqlite3")
    try:
        assert reopened.RepairInterrupted() == 2
        assert reopened.RepairInterrupted() == 0
        assert reopened.GetTask(task.task_id).status == "interrupted"
        assert reopened.GetTask(queued.task_id).status == "interrupted"
        history = reopened.ReadHistory(session.session_id, HistoryQuery()).items
        assert [m.content for m in history if m.role == "user"] == ["hello"]
        assert [(m.status,m.effect) for m in history if m.role == "tool"] == [("ok","known"),("unknown","unknown")]
    finally:
        reopened.Close()


def test_history_cursor_keeps_whole_turns_and_stable_snapshot(store):
    session, task, scope = started(store, "one")
    store.AppendToolCalls(scope, batch())
    store.FinishTurn(task.task_id, RunResult("cancelled", tool_results=(
        ToolResult("c1", "cancelled", "not executed"), ToolResult("c2", "cancelled", "not executed"))))
    _, second, _ = started(store, "two", session)
    store.FinishTurn(second.task_id, RunResult("completed", final="two final"))
    page = store.ReadHistory(session.session_id, HistoryQuery(limit=1))
    assert len(page.items) == 4 and page.next_cursor
    _, third, _ = started(store, "three", session)
    store.FinishTurn(third.task_id, RunResult("completed", final="three final"))
    tail = store.ReadHistory(session.session_id, HistoryQuery(limit=1, cursor=page.next_cursor))
    assert [m.content for m in tail.items] == ["two", "two final"]
    assert tail.next_cursor is None
    with pytest.raises(DomainError):
        store.ReadHistory("missing", HistoryQuery(cursor=page.next_cursor))


def test_sessions_cursor_missing_objects_and_invalid_queries(store):
    first = store.CreateSession(SessionCreate("first"))
    second = store.CreateSession(SessionCreate("second"))
    page = store.ListSessions(PageQuery(limit=1))
    assert page.items == (first,) and page.next_cursor
    store.CreateSession(SessionCreate("third"))
    assert store.ListSessions(PageQuery(limit=1, cursor=page.next_cursor)).items == (second,)
    for operation in (lambda: store.LoadSession("missing"), lambda: store.GetTask("missing"),
                      lambda: store.RegisterTurn(TurnRequest("missing", "text"))):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "NOT_FOUND"
    for query in (PageQuery(limit=0), PageQuery(limit=True), PageQuery(cursor="broken")):
        with pytest.raises(DomainError) as error:
            store.ListSessions(query)
        assert error.value.code == "INVALID_REQUEST"



def test_begin_transaction_fault_rolls_back_running_and_user(store, tmp_path):
    session=store.CreateSession(SessionCreate())
    task=store.RegisterTurn(TurnRequest(session.session_id,"hello"))
    sql(tmp_path, "CREATE TRIGGER reject_user BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(DomainError) as error:
        store.BeginTurn(task.task_id)
    assert error.value.code=="STORAGE"
    assert store.GetTask(task.task_id).status=="queued"
    assert not store.LoadSession(session.session_id).messages


def test_frozen_prompt_is_not_rewritten_and_running_history_never_exposes_unpaired_calls(store,tmp_path):
    session,task,scope=started(store)
    store.FreezePrompt(session.session_id,"fixed")
    sql(tmp_path,"CREATE TRIGGER reject_prompt BEFORE UPDATE OF prompt ON sessions BEGIN SELECT RAISE(ABORT,'rewrite'); END")
    assert store.FreezePrompt(session.session_id,"fixed")=="fixed"
    store.AppendToolCalls(scope,batch())
    assert store.ReadHistory(session.session_id,HistoryQuery()).items==()
    store.FinishTurn(task.task_id,RunResult("cancelled"))
    assert len(store.ReadHistory(session.session_id,HistoryQuery()).items)==4
