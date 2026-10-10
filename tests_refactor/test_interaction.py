"""P3 human waiting, scope, expiry, and approval bound to one dispatch."""
from contextlib import contextmanager
from threading import Event, Thread
import time
import pytest

from funagent.contracts import ControlError, DomainError, RunContext, ToolCall, ToolDefinition
from funagent.interaction import InteractionMgr
from funagent.tool import ToolMgr
from funagent.tool.human import AskUserHandler
from fakes import SpyToolHandler


@pytest.fixture
def interactions():
    InteractionMgr.Initialize(timeout=20)
    try:
        yield InteractionMgr.GetInstance()
    finally:
        InteractionMgr.Close()


@contextmanager
def waiting(mgr, context, kind="ask_user", action=None):
    outcome, done = {}, Event()
    def run():
        try:
            outcome["answer"] = action() if action else mgr.Wait(kind, "Which file?", context)
        except Exception as error:
            outcome["error"] = error
        finally:
            done.set()
    thread = Thread(target=run)
    thread.start()
    try:
        end = time.perf_counter() + 3
        while time.perf_counter() < end:
            requests = mgr.Pending(context.task_id)
            if requests:
                yield requests[0], outcome, done
                break
            if done.is_set():
                pytest.fail(f"Wait returned without a pending request: {outcome}")
            done.wait(0.005)
        else:
            pytest.fail("No human request was published")
    finally:
        context.cancel.set()
        mgr.CancelTask(context.task_id)
        thread.join(3)
        assert not thread.is_alive()


def context(task="task", run="run", **kwargs):
    return RunContext(task_id=task, run_id=run, **kwargs)


def test_resolve_wakes_exact_original_wait(interactions):
    ctx = context()
    with waiting(interactions, ctx) as (request, outcome, done):
        assert (request.task_id, request.run_id, request.kind) == ("task", "run", "ask_user")
        assert interactions.Pending("another-task") == ()
        interactions.Resolve(request.interaction_id, "note.txt", task_id="task", run_id="run")
        assert done.wait(3) and outcome == {"answer": "note.txt"}
        assert interactions.Pending("task") == ()


@pytest.mark.parametrize("task, run, identifier", [("other", "run", None), ("task", "old-run", None), ("task", "run", "missing")])
def test_noncurrent_answers_do_not_wake_waiter(interactions, task, run, identifier):
    with waiting(interactions, context()) as (request, outcome, done):
        with pytest.raises(DomainError) as error:
            interactions.Resolve(identifier or request.interaction_id, "wrong", task_id=task, run_id=run)
        assert error.value.code == "INTERACTION_STALE" and not done.is_set()
        interactions.Resolve(request.interaction_id, "right", task_id="task", run_id="run")
        assert done.wait(3) and outcome == {"answer": "right"}
        with pytest.raises(DomainError) as error:
            interactions.Resolve(request.interaction_id, "repeat", task_id="task", run_id="run")
        assert error.value.code == "INTERACTION_STALE"


@pytest.mark.parametrize("answer", ["", "   ", True, "x" * 4001], ids=["empty", "blank", "boolean", "too-long"])
def test_invalid_answer_leaves_pending_request(interactions, answer):
    with waiting(interactions, context()) as (request, outcome, done):
        with pytest.raises(DomainError) as error:
            interactions.Resolve(request.interaction_id, answer, task_id="task", run_id="run")
        assert error.value.code == "INVALID_REQUEST" and not done.is_set()
        assert interactions.Pending("task") == (request,)


def test_expired_request_rejects_late_answer_and_wakes(interactions, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("funagent.interaction.time.monotonic", lambda: now[0])
    with waiting(interactions, context()) as (request, outcome, done):
        now[0] = request.expires_at + 1
        with pytest.raises(DomainError) as error:
            interactions.Resolve(request.interaction_id, "late", task_id="task", run_id="run")
        assert error.value.code == "INTERACTION_STALE"
        assert done.wait(3) and outcome["error"].code == "INTERACTION_EXPIRED"
        assert interactions.Pending("task") == ()


def test_parent_deadline_caps_human_wait(interactions, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("funagent.interaction.time.monotonic", lambda: now[0])
    with waiting(interactions, context(deadline=102.0)) as (request, outcome, done):
        assert request.expires_at == 102.0
        now[0] = 103.0
        assert interactions.Pending("task") == ()
        assert done.wait(3) and isinstance(outcome["error"], ControlError)
        assert outcome["error"].code == "DEADLINE"


def test_task_cancel_wakes_and_cleans_pending(interactions):
    ctx = context()
    with waiting(interactions, ctx) as (request, outcome, done):
        ctx.cancel.set()
        InteractionMgr.CancelTask("task")
        assert done.wait(3) and outcome["error"].code == "CANCELLED"
        assert interactions.Pending("task") == ()
        with pytest.raises(DomainError):
            interactions.Resolve(request.interaction_id, "late", task_id="task", run_id="run")


def test_close_wakes_all_waiters_then_allows_reassembly(interactions):
    with waiting(interactions, context("one", "run1")) as (_, first, done1):
        with waiting(interactions, context("two", "run2")) as (_, second, done2):
            InteractionMgr.Close()
            assert done1.wait(3) and done2.wait(3)
            assert first["error"].code == second["error"].code == "CANCELLED"
    InteractionMgr.Initialize()
    assert InteractionMgr.GetInstance().Pending("one") == ()


@pytest.mark.parametrize("ctx", [RunContext(purpose="review"), RunContext(purpose="recall"), RunContext()])
def test_noninteractive_context_denied_without_pending(interactions, ctx):
    with pytest.raises(DomainError) as error:
        interactions.Wait("ask_user", "question", ctx)
    assert error.value.code == "INTERACTION_DENIED"
    assert interactions.Pending(ctx.task_id) == ()


def test_ask_user_tool_denied_in_review(interactions):
    ToolMgr.Initialize({"ask_user": ToolDefinition("ask_user", "Ask", {"type": "object"})}, {"ask_user": AskUserHandler})
    result = ToolMgr.GetInstance().Execute(ToolCall("ask", "ask_user", {"prompt": "question"}), ("ask_user",), RunContext(purpose="review"))
    assert result.status == "denied" and result.effect == "none"
    assert interactions.Pending("") == ()


def test_approval_is_bound_to_exact_call_and_not_reused(interactions):
    spy = SpyToolHandler()
    ToolMgr.Initialize({"write": ToolDefinition("write", "Write", {"type": "object"})}, {"write": spy}, approval_names=("write",))
    tools = ToolMgr.GetInstance()
    first = context()
    with waiting(interactions, first, action=lambda: tools.Execute(ToolCall("c1", "write", {"value": "first"}), ("write",), first)) as (request, outcome, done):
        assert request.kind == "approval" and "first" in request.prompt
        assert not spy.calls
        interactions.Resolve(request.interaction_id, True, task_id="task", run_id="run")
        assert done.wait(3) and outcome["answer"].status == "ok"
        assert len(spy.calls) == 1
        with pytest.raises(DomainError):
            interactions.Resolve(request.interaction_id, True, task_id="task", run_id="run")
    previous_id = request.interaction_id
    second = context()
    with waiting(interactions, second, action=lambda: tools.Execute(ToolCall("c2", "write", {"value": "second"}), ("write",), second)) as (request, outcome, done):
        with pytest.raises(DomainError) as error:
            interactions.Resolve(previous_id, True, task_id="task", run_id="run")
        assert error.value.code == "INTERACTION_STALE" and not done.is_set()
        interactions.Resolve(request.interaction_id, False, task_id="task", run_id="run")
        assert done.wait(3) and outcome["answer"].status == "denied"
        assert len(spy.calls) == 1


def test_approval_rejects_bad_args_and_review_before_dispatch(interactions):
    spy = SpyToolHandler()
    schema = {"type": "object", "additionalProperties": False}
    ToolMgr.Initialize({"write": ToolDefinition("write", "Write", schema)}, {"write": spy}, approval_names=("write",))
    tools = ToolMgr.GetInstance()
    assert tools.Execute(ToolCall("bad", "write", {"access": "admin"}), ("write",), context()).status == "invalid"
    assert tools.Execute(ToolCall("review", "write"), ("write",), RunContext(purpose="review")).status == "denied"
    assert not spy.calls and interactions.Pending("task") == ()


@pytest.mark.parametrize("control", ["expiry", "cancel"])
def test_expired_or_cancelled_approval_cannot_dispatch(interactions, monkeypatch, control):
    now = [100.0]
    monkeypatch.setattr("funagent.interaction.time.monotonic", lambda: now[0])
    spy = SpyToolHandler()
    ToolMgr.Initialize({"write": ToolDefinition("write", "Write", {"type": "object"})}, {"write": spy}, approval_names=("write",))
    ctx = context()
    action = lambda: ToolMgr.GetInstance().Execute(ToolCall("c", "write"), ("write",), ctx)
    with waiting(interactions, ctx, action=action) as (request, outcome, done):
        if control == "expiry":
            now[0] = request.expires_at + 1
        else:
            ctx.cancel.set()
        with pytest.raises(DomainError) as error:
            interactions.Resolve(request.interaction_id, True, task_id="task", run_id="run")
        assert error.value.code == "INTERACTION_STALE"
        assert done.wait(3) and outcome["answer"].status == ("error" if control == "expiry" else "cancelled")
        assert outcome["answer"].effect == "none" and not spy.calls
