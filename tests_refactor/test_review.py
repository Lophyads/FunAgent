"""P5 contracts: real app/memory databases and controlled background work."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import json
import time

import pytest

from funagent.contracts import (DomainError, SessionCreate, TurnRequest, RunResult,
                               Message, ToolCall, ToolResult, RecordScope, RunContext)
from funagent.persistence import SQLiteStore


def turn(store, session, text="hello", status="completed", tools=False):
    task = store.RegisterTurn(TurnRequest(session.session_id, text))
    store.BeginTurn(task.task_id)
    if tools:
        scope = RecordScope("main_history", task.task_id)
        store.AppendToolCalls(scope, Message("assistant", calls=(ToolCall("call", "echo"),)))
        store.AppendToolResult(scope, ToolResult("call", "ok", "tool evidence"))
    store.FinishTurn(task.task_id, RunResult(status, final="reply" if status == "completed" else None))
    return store.ReviewThrough(task.task_id)


@pytest.fixture
def review_db(tmp_path):
    store = SQLiteStore(tmp_path / "app.sqlite3")
    try:
        yield store, store.CreateSession(SessionCreate())
    finally:
        store.Close()


def test_review_range_is_a_fixed_complete_turn_snapshot(review_db):
    store, session = review_db
    first = turn(store, session, "first", tools=True)
    second = turn(store, session, "second")
    task = store.RegisterTurn(TurnRequest(session.session_id, "still running"))
    running = store.BeginTurn(task.task_id).through_message_id
    messages = store.ReviewRange(session.session_id, 0, first)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert [m.message_id for m in messages] == list(range(1, first + 1))
    assert [m.content for m in store.ReviewRange(session.session_id, first, second)] == ["second", "reply"]
    for after, through in [(0, first - 1), (1, second), (0, running), (second, first), (0, second + 100)]:
        with pytest.raises(DomainError) as error:
            store.ReviewRange(session.session_id, after, through)
        assert error.value.code == "INVALID_REQUEST"
    other = store.CreateSession(SessionCreate())
    with pytest.raises(DomainError):
        store.ReviewRange(other.session_id, 0, first)
    assert store.ReviewRange(session.session_id, second, second) == ()


def test_review_watermark_cas_is_monotonic_and_persistent(review_db, tmp_path):
    store, session = review_db
    first = turn(store, session)
    second = turn(store, session, status="failed", tools=True)
    assert store.ReviewWatermark(session.session_id) == 0
    store.AdvanceReview(session.session_id, 0, first)
    with pytest.raises(DomainError) as error:
        store.AdvanceReview(session.session_id, 0, second)
    assert error.value.code == "CONFLICT"
    with pytest.raises(DomainError):
        store.AdvanceReview(session.session_id, first, first - 1)
    with pytest.raises(DomainError):
        store.AdvanceReview(session.session_id, first, second - 1)
    assert store.ReviewWatermark(session.session_id) == first
    store.AdvanceReview(session.session_id, first, second)
    observer = SQLiteStore(tmp_path / "app.sqlite3")
    try:
        assert observer.ReviewWatermark(session.session_id) == second
    finally:
        observer.Close()
    with pytest.raises(DomainError) as error:
        store.ReviewWatermark("missing")
    assert error.value.code == "NOT_FOUND"


@pytest.mark.parametrize("after,through", [(True, 2), (0, False), (-1, 2), (0, "2")])
def test_review_rejects_invalid_boundary_types(review_db, after, through):
    store, session = review_db
    turn(store, session)
    with pytest.raises(DomainError) as error:
        store.ReviewRange(session.session_id, after, through)
    assert error.value.code == "INVALID_REQUEST"


from funagent.conf import ConfMgr
from funagent.memory import MemoryMgr, SQLiteMemoryStore, MemoryDraft, MemoryQuery
from funagent.memory.review import (ReviewProcess, MemoryReviewStrategy, REVIEW_DEFINITIONS,
                                   MemoryWriteHandler, MemoryUpdateHandler, MemoryDeleteHandler)
from funagent.memory.retrieval import MemorySearchHandler
from funagent.model import ModelRuntime
from funagent.persistence import DbMgr
from funagent.review import ReviewJob
from funagent.tool import ToolMgr
from fakes import ScriptedProvider, ChatAdapter, BlocksAdapter, final


@pytest.fixture
def review_app(conf_dir, tmp_path):
    def bind(provider, facts=None):
        ConfMgr.Initialize(conf_dir)
        conf = ConfMgr.GetInstance()
        ToolMgr.Initialize(REVIEW_DEFINITIONS, {"memory_search": MemorySearchHandler,
                           "memory_write": MemoryWriteHandler, "memory_update": MemoryUpdateHandler,
                           "memory_delete": MemoryDeleteHandler})
        ModelRuntime.Initialize({name: conf.Model().Profile(name) for name in ("main", "fallback")},
                                {"fake": provider}, {"chat": ChatAdapter, "blocks": BlocksAdapter})
        store = SQLiteStore(tmp_path / "app.sqlite3")
        DbMgr.Initialize(store)
        facts = facts or SQLiteMemoryStore(tmp_path / "memory.sqlite3")
        MemoryMgr.Initialize({"facts": facts})
        session = store.CreateSession(SessionCreate())
        through = turn(store, session, "User prefers Chinese", tools=True)
        return store, session, facts, ReviewJob(session.session_id, through)
    yield bind


def mutation(id="new-call", name="memory_write", **args):
    return {"id": id, "name": name, "args": {"source": "facts", "message_ids": [1], **args}}


def batch(*calls):
    return {"calls": list(calls), "finish": "tool_calls"}


def reader():
    return RunContext(access=frozenset({"memory.facts.read", "memory.facts.write"}))


def test_review_uses_same_run_trusted_writes_and_does_not_touch_main_history(review_app, monkeypatch):
    provider = ScriptedProvider(batch(mutation(content="Prefers Chinese", type="profile")), final("chat", "review text"))
    store, session, facts, job = review_app(provider)
    monkeypatch.setattr(MemoryMgr.GetInstance(), "Prepare", lambda *a: pytest.fail("Review called Prepare"))
    from funagent.task import TaskProcess
    monkeypatch.setattr(TaskProcess, "Run", lambda *a: pytest.fail("Review called TaskProcess"))
    before = store.LoadSession(session.session_id)
    result = ReviewProcess.Run(job, RunContext())
    assert result.status == "completed"
    assert store.ReviewWatermark(session.session_id) == job.through_message_id
    assert store.LoadSession(session.session_id) == before
    record = facts.Search(MemoryQuery(), RunContext()).records[0]
    assert record.content == "Prefers Chinese" and record.type == "profile"
    assert record.evidence == (f"session:{session.session_id}/message:1",)
    assert all(c.purpose == "review" and c.record_scope == "ephemeral" and not c.task_id for c in provider.contexts)
    assert {d["name"] for d in provider.requests[0].payload["tools"]} == set(REVIEW_DEFINITIONS)


def test_plain_final_is_not_a_confirmed_memory_write(review_app):
    store, session, facts, job = review_app(ScriptedProvider(final("chat", "I saved facts")))
    result = ReviewProcess.Run(job, RunContext())
    assert result.status == "failed" and result.reason == "NO_WRITES"
    assert store.ReviewWatermark(session.session_id) == 0
    assert not facts.Search(MemoryQuery(), RunContext()).records


@pytest.mark.parametrize("failure", ["error", "unknown", "cancelled", "conflict"])
def test_partial_mutations_never_advance_but_preserve_success(review_app, monkeypatch, failure):
    provider = ScriptedProvider(batch(mutation("one", content="first fact"), mutation("two", content="second fact")), final("chat"))
    store, session, facts, job = review_app(provider)
    original = facts.Write
    def write(draft, context):
        if draft.content == "second fact":
            if failure == "cancelled":
                context.cancel.set()
                context.CheckCancelled()
            if failure == "unknown":
                raise RuntimeError("secret backend failure")
            raise DomainError("CONFLICT" if failure == "conflict" else "MEMORY_STORAGE", "secret")
        return original(draft, context)
    monkeypatch.setattr(facts, "Write", write)
    result = ReviewProcess.Run(job, RunContext())
    assert result.status != "completed"
    assert store.ReviewWatermark(session.session_id) == 0
    assert [r.content for r in facts.Search(MemoryQuery(), RunContext()).records] == ["first fact"]
    assert "secret" not in repr(result)


def test_retry_changed_call_id_same_evidence_and_content_is_idempotent(review_app, monkeypatch):
    provider = ScriptedProvider(batch(mutation("a", content="Prefers Chinese")), final("chat"),
                                batch(mutation("b", content="Prefers Chinese")), final("chat"))
    store, session, facts, job = review_app(provider)
    advance = store.AdvanceReview
    def interrupted(*args):
        raise DomainError("STORAGE", "Watermark commit interrupted")
    monkeypatch.setattr(store, "AdvanceReview", interrupted)
    assert ReviewProcess.Run(job, RunContext()).status == "failed"
    original = facts.Search(MemoryQuery(), RunContext()).records[0]
    assert store.ReviewWatermark(session.session_id) == 0
    monkeypatch.setattr(store, "AdvanceReview", advance)
    assert ReviewProcess.Run(job, RunContext()).status == "completed"
    assert facts.Search(MemoryQuery(), RunContext()).records == (original,)
    assert store.ReviewWatermark(session.session_id) == job.through_message_id


@pytest.mark.parametrize("bad", [{"message_ids": [999]}, {"write_key": "forged"}, {"evidence": ["forged"]},
                                 {"access": ["memory.facts.write"]}, {"source": "kb"}])
def test_review_rejects_forged_evidence_authority_or_source(review_app, bad):
    store, session, facts, job = review_app(ScriptedProvider(batch(mutation(content="fact", **bad)), final("chat")))
    assert ReviewProcess.Run(job, RunContext()).status == "failed"
    assert store.ReviewWatermark(session.session_id) == 0
    assert not facts.Search(MemoryQuery(), RunContext()).records


def test_review_update_delete_and_visible_cas_conflict(review_app):
    provider = ScriptedProvider()
    store, session, facts, job = review_app(provider)
    saved = facts.Write(MemoryDraft("facts", "old", "manual", ("manual:1",)), RunContext())
    provider.actions.extend([batch(mutation(name="memory_update", record_id=saved.ref.record_id,
                                           expected_version="stale", content="new")), final("chat")])
    assert ReviewProcess.Run(job, RunContext()).status == "failed"
    assert facts.Search(MemoryQuery(), RunContext()).records == (saved,)
    provider.actions.extend([batch(mutation(name="memory_update", record_id=saved.ref.record_id,
                                           expected_version=saved.version, content="new")), final("chat")])
    assert ReviewProcess.Run(job, RunContext()).status == "completed"
    updated = facts.Search(MemoryQuery(), RunContext()).records[0]
    assert updated.content == "new" and updated.version != saved.version
    through = turn(store, session, "Forget the preference")
    provider.actions.extend([batch(mutation(name="memory_delete", record_id=updated.ref.record_id,
                                           expected_version=updated.version, message_ids=[through - 1])), final("chat")])
    assert ReviewProcess.Run(ReviewJob(session.session_id, through), RunContext()).status == "completed"
    assert not facts.Search(MemoryQuery(), RunContext()).records


from funagent.review import ReviewScheduler
from funagent.model import ProviderResponse


def wait_review(scheduler, session_id, state=None):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = scheduler.GetStatus(session_id)
        done = status["state"] == state if state else status["state"] in {"completed", "failed", "cancelled"}
        if done:
            return status
        Event().wait(0.005)
    pytest.fail(f"Review did not finish: {scheduler.GetStatus(session_id)}")


def test_pending_jobs_coalesce_while_running_snapshot_stays_fixed(review_app):
    entered, release = Event(), Event()
    def blocked(request, context):
        entered.set()
        assert release.wait(3)
        return ProviderResponse(batch(mutation(content="first fact")))
    def next_write(request, context):
        history = json.loads(request.payload["messages"][-1]["content"])
        return ProviderResponse(batch(mutation(content="later fact", message_ids=[history[0]["message_id"]])))
    provider = ScriptedProvider(blocked, final("chat"), next_write, final("chat"))
    store, session, facts, job = review_app(provider)
    ReviewScheduler.Initialize(1)
    scheduler = ReviewScheduler.GetInstance()
    try:
        scheduler.Schedule(session.session_id, job.through_message_id)
        assert entered.wait(1)
        second = turn(store, session, "second chat")
        third = turn(store, session, "third chat")
        scheduler.Schedule(session.session_id, second)
        scheduler.Schedule(session.session_id, third)
        status = scheduler.GetStatus(session.session_id)
        assert status["running_through"] == job.through_message_id and status["pending_through"] == third
        other = store.CreateSession(SessionCreate())
        through = turn(store, other)
        with pytest.raises(DomainError) as error:
            scheduler.Schedule(other.session_id, through)
        assert error.value.code == "CAPACITY"
        first_history = provider.requests[0].payload["messages"][-1]["content"]
        assert "second chat" not in first_history and "third chat" not in first_history
        release.set()
        status = wait_review(scheduler, session.session_id, "completed")
        assert status["through_message_id"] == third
        assert store.ReviewWatermark(session.session_id) == third
        assert "second chat" in provider.requests[2].payload["messages"][-1]["content"]
        assert len(facts.Search(MemoryQuery(), RunContext()).records) == 2
    finally:
        release.set()
        ReviewScheduler.Close()


def test_failed_review_does_not_kill_worker_and_next_job_runs(review_app):
    provider = ScriptedProvider(final("chat", "No write"))
    store, session, facts, job = review_app(provider)
    ReviewScheduler.Initialize()
    scheduler = ReviewScheduler.GetInstance()
    try:
        scheduler.Schedule(session.session_id, job.through_message_id)
        assert wait_review(scheduler, session.session_id)["error_code"] == "NO_WRITES"
        second = store.CreateSession(SessionCreate())
        through = turn(store, second)
        provider.actions.extend([batch(mutation(content="other fact", message_ids=[through - 1])), final("chat")])
        scheduler.Schedule(second.session_id, through)
        assert wait_review(scheduler, second.session_id)["state"] == "completed"
        assert store.ReviewWatermark(session.session_id) == 0
        assert store.ReviewWatermark(second.session_id) == through
    finally:
        ReviewScheduler.Close()


def test_stop_cancels_pending_and_running_but_waits_for_real_io(review_app):
    entered, release, stopped = Event(), Event(), Event()
    def blocked(request, context):
        entered.set()
        assert release.wait(3)
        return ProviderResponse(final("chat"))
    store, session, facts, job = review_app(ScriptedProvider(blocked))
    ReviewScheduler.Initialize()
    scheduler = ReviewScheduler.GetInstance()
    pool = ThreadPoolExecutor(1)
    try:
        scheduler.Schedule(session.session_id, job.through_message_id)
        assert entered.wait(1)
        pending_through = turn(store, session, "pending same session")
        scheduler.Schedule(session.session_id, pending_through)
        other = store.CreateSession(SessionCreate())
        scheduler.Schedule(other.session_id, turn(store, other))
        def stop():
            stopped.set()
            scheduler.Stop()
        future = pool.submit(stop)
        assert stopped.wait(1)
        assert wait_review(scheduler, other.session_id)["state"] == "cancelled"
        assert not future.done()
        assert scheduler.GetStatus(session.session_id)["cancelled_through"] == pending_through
        assert facts.Search(MemoryQuery(), RunContext()).records == ()
        release.set()
        future.result(2)
        assert scheduler.GetStatus(session.session_id)["state"] == "cancelled"
        assert store.ReviewWatermark(session.session_id) == 0
        with pytest.raises(DomainError) as error:
            scheduler.Schedule(session.session_id, job.through_message_id)
        assert error.value.code == "STOPPING"
    finally:
        release.set()
        pool.shutdown()
        ReviewScheduler.Close()


from funagent.global_server import Start, Stop
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from funagent.model.demo import DemoAdapter
from funagent.contracts import ModelReply, ControlError
from test_task_flow import wait_task


@pytest.fixture
def review_cli_conf(cli_conf):
    file = cli_conf / "memory.conf"
    file.write_text(file.read_text().replace("[policies.review]", "[policies.review]\nenabled = true\ntimeout = 2.0\ncapacity = 2"), encoding="utf8")
    return cli_conf


def test_main_commits_before_schedule_review_is_independent_and_latest_fact_is_visible(review_cli_conf, monkeypatch):
    from funagent import global_server
    entered, release = Event(), Event()
    contexts, main_inputs, scheduled = [], [], []
    class Provider:
        def __init__(self, settings): pass
        def Send(self, request, context):
            contexts.append(context)
            last = request.payload.messages[-1]
            if context.purpose == "review":
                if last.role == "tool":
                    return ProviderResponse(ModelReply("Review complete"))
                entered.set()
                assert release.wait(3)
                history = json.loads(last.content)
                return ProviderResponse(ModelReply(calls=(ToolCall("review-call", "memory_write", {
                    "source": "facts", "message_ids": [history[0]["message_id"]], "content": "Prefers Chinese", "type": "profile"}),), finish_reason="tool_calls"))
            main_inputs.append(request.payload)
            return ProviderResponse(ModelReply("main reply"))
        def Close(self): pass
    monkeypatch.setitem(global_server.PROVIDERS, "demo", Provider)
    original = ReviewScheduler.Schedule
    def schedule(self, session_id, through):
        history = DbMgr.GetInstance().ReviewRange(session_id, 0, through)
        assert history[-1].role == "assistant" and history[-1].content == "main reply"
        scheduled.append(through)
        return original(self, session_id, through)
    monkeypatch.setattr(ReviewScheduler, "Schedule", schedule)
    Start(review_cli_conf)
    try:
        session = SessionMgr.Create(SessionCreate())
        tasks, scheduler = TaskMgr.GetInstance(), ReviewScheduler.GetInstance()
        first = wait_task(tasks, tasks.Submit(TurnRequest(session.session_id, "Prefers Chinese")))
        assert first.status == "completed" and entered.wait(1)
        review_context = next(c for c in contexts if c.purpose == "review")
        first_main = next(c for c in contexts if c.purpose == "main")
        first_main.cancel.set()
        assert not review_context.cancel.is_set()
        second = wait_task(tasks, tasks.Submit(TurnRequest(session.session_id, "next chat")))
        assert second.status == "completed" and not review_context.cancel.is_set()
        assert scheduler.GetStatus(session.session_id)["running_through"] == scheduled[0]
        release.set()
        assert wait_review(scheduler, session.session_id, "completed")["through_message_id"] == scheduled[-1]
        third = wait_task(tasks, tasks.Submit(TurnRequest(session.session_id, "use preference")))
        assert third.status == "completed"
        assert "Prefers Chinese" in main_inputs[-1].messages[0].content
        history = SessionMgr.ReadHistory(session.session_id).items
        assert [m.role for m in history] == ["user", "assistant"] * 3
        assert not any("Review" in m.content for m in history)
    finally:
        release.set()
        Stop()


def test_full_background_queue_cannot_fail_an_already_committed_main_task(review_cli_conf, monkeypatch):
    from funagent import global_server
    monkeypatch.setattr(ReviewScheduler, "Schedule", lambda *a: (_ for _ in ()).throw(DomainError("CAPACITY", "queue full")))
    Start(review_cli_conf)
    session = SessionMgr.Create(SessionCreate())
    mgr = TaskMgr.GetInstance()
    result = wait_task(mgr, mgr.Submit(TurnRequest(session.session_id, "hello")))
    assert result.status == "completed" and result.result.final == "Demo: hello"
    assert SessionMgr.ReadHistory(session.session_id).items[-1].content == "Demo: hello"


@pytest.mark.parametrize("field,value", [("capacity", "0"), ("capacity", "true"), ("capacity", "1.5"),
                                         ("timeout", "0"), ("enabled", '"yes"')])
def test_invalid_review_policy_fails_before_publication(cli_conf, field, value):
    file = cli_conf / "memory.conf"
    file.write_text(file.read_text().replace("[policies.review]", "[policies.review]\n" + field + " = " + value), encoding="utf8")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(cli_conf)
    assert error.value.code == "CONFIG_INVALID"
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()


def test_review_watermark_conflict_preserves_higher_progress(review_app, monkeypatch):
    provider = ScriptedProvider(batch(mutation(content="fact")), final("chat"))
    store, session, facts, job = review_app(provider)
    later = turn(store, session, "new chat")
    advance = store.AdvanceReview
    def race(session_id, expected, through):
        advance(session_id, expected, later)
        advance(session_id, expected, through)
    monkeypatch.setattr(store, "AdvanceReview", race)
    result = ReviewProcess.Run(job, RunContext())
    assert result.reason == "CONFLICT"
    assert store.ReviewWatermark(session.session_id) == later
    assert len(facts.Search(MemoryQuery(), RunContext()).records) == 1


def test_review_scheduler_startup_fault_closes_resources_and_restarts(review_cli_conf, monkeypatch):
    original = ReviewScheduler.Initialize
    monkeypatch.setattr(ReviewScheduler, "Initialize", lambda *a: (_ for _ in ()).throw(DomainError("STARTUP_FAILED", "fault")))
    with pytest.raises(DomainError):
        Start(review_cli_conf)
    for manager in (TaskMgr, ReviewScheduler, MemoryMgr, DbMgr, ModelRuntime, ConfMgr):
        with pytest.raises(DomainError):
            manager.GetInstance()
    monkeypatch.setattr(ReviewScheduler, "Initialize", original)
    Start(review_cli_conf)
    assert ReviewScheduler.GetInstance() is not None


def test_shared_real_provider_lock_wait_obeys_deadline_and_never_sends_expired_io(monkeypatch):
    import httpx
    from funagent.model.openai_chat import OpenAIProvider
    from funagent.model import ProviderRequest
    from test_provider_contract import completion
    tick = [10.0]
    monkeypatch.setattr("funagent.contracts.time.monotonic", lambda: tick[0])
    monkeypatch.setenv("P5_TEST_KEY", "test-token")
    entered, release, second_started = Event(), Event(), Event()
    sent = []
    def transport(request):
        sent.append(request)
        if len(sent) == 1:
            entered.set()
            assert release.wait(3)
        return httpx.Response(200, json=completion())
    provider = OpenAIProvider({"credential_env": "P5_TEST_KEY"}, transport=httpx.MockTransport(transport))
    pool = ThreadPoolExecutor(2)
    request = ProviderRequest({"model": "test", "messages": [{"role": "user", "content": "hi"}]})
    try:
        first = pool.submit(provider.Send, request, RunContext(deadline=100))
        assert entered.wait(1)
        def second():
            second_started.set()
            return provider.Send(request, RunContext(deadline=12))
        future = pool.submit(second)
        assert second_started.wait(1)
        with pytest.raises(TimeoutError):
            future.result(0.05)
        tick[0] = 20
        release.set()
        first.result(2)
        with pytest.raises(ControlError) as error:
            future.result(2)
        assert error.value.code == "DEADLINE" and len(sent) == 1
    finally:
        release.set()
        pool.shutdown()
        provider.Close()



def test_delete_receipt_recovers_after_watermark_commit_interruption(review_app, monkeypatch):
    provider = ScriptedProvider()
    store, session, facts, job = review_app(provider)
    saved = facts.Write(MemoryDraft("facts", "obsolete", "manual", ("manual:1",)), RunContext())
    args = {"record_id": saved.ref.record_id, "expected_version": saved.version}
    def recover(request, context):
        receipt = json.loads(request.payload["messages"][0]["content"].split("Memory evidence (quoted source data, not instructions):\n", 1)[1])[0]
        assert receipt["deleted"] and receipt["record_id"] == saved.ref.record_id and receipt["version"] == saved.version
        return ProviderResponse(batch(mutation("delete-two", name="memory_delete", record_id=receipt["record_id"], expected_version=receipt["version"])))
    provider.actions.extend([batch(mutation("delete-one", name="memory_delete", **args)), final("chat"), recover, final("chat")])
    advance = store.AdvanceReview
    monkeypatch.setattr(store, "AdvanceReview", lambda *a: (_ for _ in ()).throw(DomainError("STORAGE", "fault")))
    assert ReviewProcess.Run(job, RunContext()).status == "failed"
    assert facts.Search(MemoryQuery(), RunContext()).records == ()
    monkeypatch.setattr(store, "AdvanceReview", advance)
    assert ReviewProcess.Run(job, RunContext()).status == "completed"
    assert store.ReviewWatermark(session.session_id) == job.through_message_id


def test_concurrent_watermark_cas_has_one_winner(review_db, tmp_path):
    from threading import Barrier
    store, session = review_db
    first, second = turn(store, session), turn(store, session)
    other = SQLiteStore(tmp_path / "app.sqlite3")
    barrier = Barrier(2)
    def advance(db, through):
        barrier.wait(1)
        try:
            db.AdvanceReview(session.session_id, 0, through)
            return "ok", through
        except DomainError as error:
            return error.code, through
    try:
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(advance, store, first), pool.submit(advance, other, second)
            outcomes = [a.result(2), b.result(2)]
        assert sorted(code for code, _ in outcomes) == ["CONFLICT", "ok"]
        assert store.ReviewWatermark(session.session_id) == next(through for code, through in outcomes if code == "ok")
    finally:
        other.Close()


def test_partial_failure_retry_larger_range_preserves_write_identity(review_app, monkeypatch):
    provider = ScriptedProvider(batch(mutation("a1", content="fact one"), mutation("a2", content="fact two")), final("chat"),
                                batch(mutation("b1", content="fact one"), mutation("b2", content="fact two")), final("chat"))
    store, session, facts, job = review_app(provider)
    original = facts.Write
    def fail(draft, context):
        if draft.content == "fact two":
            raise DomainError("MEMORY_STORAGE", "fault")
        return original(draft, context)
    monkeypatch.setattr(facts, "Write", fail)
    assert ReviewProcess.Run(job, RunContext()).status == "failed"
    saved = facts.Search(MemoryQuery(), RunContext()).records[0]
    through = turn(store, session, "later chat")
    monkeypatch.setattr(facts, "Write", original)
    assert ReviewProcess.Run(ReviewJob(session.session_id, through), RunContext()).status == "completed"
    records = facts.Search(MemoryQuery(), RunContext()).records
    assert len(records) == 2 and records[0] == saved


def test_update_retry_reads_new_version_after_watermark_interruption(review_app, monkeypatch):
    provider = ScriptedProvider()
    store, session, facts, job = review_app(provider)
    saved = facts.Write(MemoryDraft("facts", "old", "manual", ("manual:1",)), RunContext())
    provider.actions.extend([batch(mutation(name="memory_update", record_id=saved.ref.record_id,
                                           expected_version=saved.version, content="new")), final("chat")])
    advance = store.AdvanceReview
    monkeypatch.setattr(store, "AdvanceReview", lambda *a: (_ for _ in ()).throw(DomainError("STORAGE", "fault")))
    assert ReviewProcess.Run(job, RunContext()).status == "failed"
    updated = facts.Search(MemoryQuery(), RunContext()).records[0]
    assert updated.version != saved.version
    def reread(request, context):
        current = json.loads(request.payload["messages"][0]["content"].split("Memory evidence (quoted source data, not instructions):\n", 1)[1])[0]
        assert current["version"] == updated.version
        return ProviderResponse(batch(mutation("retry", name="memory_update", record_id=current["record_id"],
                                               expected_version=current["version"], content="new")))
    provider.actions.extend([reread, final("chat")])
    monkeypatch.setattr(store, "AdvanceReview", advance)
    assert ReviewProcess.Run(job, RunContext()).status == "completed"


def test_review_forbidden_tool_never_invokes_handler(review_app):
    provider = ScriptedProvider(batch({"id": "human", "name": "ask_user", "args": {"prompt": "Wait for approval"}}), final("chat"))
    store, session, facts, job = review_app(provider)
    result = ReviewProcess.Run(job, RunContext())
    assert result.status == "failed" and result.tool_results[0].status in {"invalid", "denied"}
    assert store.ReviewWatermark(session.session_id) == 0


def test_unexpected_review_exception_is_reported_and_worker_continues(review_app, monkeypatch):
    store, session, facts, job = review_app(ScriptedProvider(batch(mutation(content="fact")), final("chat")))
    original = ReviewProcess.Run
    monkeypatch.setattr(ReviewProcess, "Run", lambda *a: (_ for _ in ()).throw(RuntimeError("SECRET_TOKEN")))
    ReviewScheduler.Initialize()
    scheduler = ReviewScheduler.GetInstance()
    try:
        scheduler.Schedule(session.session_id, job.through_message_id)
        status = wait_review(scheduler, session.session_id)
        assert status["error_code"] == "REVIEW_FAILED" and "SECRET_TOKEN" not in repr(status)
        monkeypatch.setattr(ReviewProcess, "Run", original)
        scheduler.Schedule(session.session_id, job.through_message_id)
        assert wait_review(scheduler, session.session_id)["state"] == "completed"
    finally:
        ReviewScheduler.Close()



def test_tombstone_receipts_are_review_only_and_different_evidence_cannot_acknowledge(review_app):
    provider = ScriptedProvider()
    store, session, facts, job = review_app(provider)
    saved = facts.Write(MemoryDraft("facts", "obsolete", "manual", ("manual:1",)), RunContext())
    args = {"record_id": saved.ref.record_id, "expected_version": saved.version}
    provider.actions.extend([batch(mutation(name="memory_delete", **args)), final("chat")])
    assert ReviewProcess.Run(job, RunContext()).status == "completed"
    with pytest.raises(DomainError) as error:
        MemoryMgr.GetInstance().Search(MemoryQuery(include_deleted=True), reader())
    assert error.value.code == "DENIED"
    through = turn(store, session, "another turn")
    provider.actions.extend([batch(mutation(name="memory_delete", message_ids=[through - 1], **args)), final("chat")])
    assert ReviewProcess.Run(ReviewJob(session.session_id, through), RunContext()).status == "failed"
    assert store.ReviewWatermark(session.session_id) == job.through_message_id



def test_offline_cli_demonstrates_main_recall_review_and_background_status(review_cli_conf, monkeypatch):
    from io import StringIO
    from funagent.api.cli import CLIController
    file = review_cli_conf / "memory.conf"
    file.write_text(file.read_text().replace("enabled = false", "enabled = true", 1), encoding="utf8")
    purposes = []
    original = ModelRuntime.Run
    def run(self, input, strategy, context):
        purposes.append(context.purpose)
        return original(self, input, strategy, context)
    monkeypatch.setattr(ModelRuntime, "Run", run)
    Start(review_cli_conf)
    session = SessionMgr.Create(SessionCreate())
    tasks, scheduler = TaskMgr.GetInstance(), ReviewScheduler.GetInstance()
    task = wait_task(tasks, tasks.Submit(TurnRequest(session.session_id, "remember: 用户喜欢中文")))
    assert task.status == "completed"
    assert wait_review(scheduler, session.session_id)["state"] == "completed"
    output = StringIO()
    CLIController.Execute(f"review {session.session_id}", output)
    status = json.loads(output.getvalue())
    assert status["state"] == "completed" and status["through_message_id"] == 2
    output = StringIO()
    CLIController.Execute("memory list facts profile", output)
    assert json.loads(output.getvalue())["records"][0]["content"] == "用户喜欢中文"
    second = wait_task(tasks, tasks.Submit(TurnRequest(session.session_id, "memory: 中文")))
    assert "用户喜欢中文" in second.result.final
    assert set(purposes) == {"main", "recall", "review"}
    assert [m.role for m in SessionMgr.ReadHistory(session.session_id).items] == ["user", "assistant"] * 2
