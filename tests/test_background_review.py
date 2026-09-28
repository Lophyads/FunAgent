from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace

import pytest

from agentcore import (
    BackgroundReviewScheduler, Message, NormalizedModelResponse,
    SessionRuntimeSnapshot, SystemPromptSnapshot, TurnResult, TurnSnapshot,
)
from agentcore.workers import TurnQueueFullError, WorkerPoolClosedError
from tools.runtime import get_tool_services
from conftest import ScriptedTransport
from test_loop_persistence import memory_loop


def snapshot(session="chat", turn="source"):
    prompt = b"original prompt with memory"
    return TurnSnapshot(
        result=TurnResult(status="completed", session_id=session, turn_id=turn),
        messages=(Message.user("original", metadata={"nested": [1]}).model_dump(mode="json"),),
        context=SessionRuntimeSnapshot(
            SystemPromptSnapshot(prompt.decode(), prompt, hashlib.sha256(prompt).hexdigest()), {}, b"[]",
        ),
        trace_id=f"trace-{turn}",
    )


def test_review_queue_is_bounded_serializes_each_chat_and_detaches_inputs():
    first_started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    other_started, other_release = threading.Event(), threading.Event()
    seen = []

    def review(task):
        if task.snapshot.result.turn_id == "one":
            first_started.set()
            assert release.wait(3)
        elif task.snapshot.result.turn_id == "two":
            second_started.set()
        elif task.snapshot.result.turn_id == "three":
            other_started.set()
            assert other_release.wait(3)
        seen.append(task)

    reviews = BackgroundReviewScheduler(review, workers=2, capacity=4)
    try:
        first = reviews.submit(snapshot(turn="one"))
        assert first_started.wait(2)
        source = snapshot(turn="two")
        second = reviews.submit(source)
        source.messages[0]["metadata"]["nested"].append(2)
        third = reviews.submit(snapshot(session="another-chat", turn="three"))
        assert other_started.wait(2)  # Another chat runs while the first is blocked.
        fourth = reviews.submit(snapshot(turn="four"))
        with pytest.raises(TurnQueueFullError):
            reviews.submit(snapshot(turn="overflow"))
        assert not second_started.is_set()
        other_release.set()
        third.future.result(timeout=3)
        release.set()
        first.future.result(timeout=3)
        second.future.result(timeout=3)
        fourth.future.result(timeout=3)
        same_chat = [task for task in seen if task.snapshot.result.session_id == "chat"]
        assert [task.snapshot.result.turn_id for task in same_chat] == ["one", "two", "four"]
        assert same_chat[1].snapshot.messages[0]["metadata"]["nested"] == [1]
        assert same_chat[1].parent_trace_id == "trace-two"
        assert same_chat[1].turn_id != source.result.turn_id
    finally:
        release.set()
        other_release.set()
        assert reviews.close(timeout=3)


def test_review_failures_have_linked_trace_and_do_not_stop_following_jobs():
    events = []

    def review(task):
        if task.snapshot.result.turn_id == "bad":
            raise ValueError("review failed")

    reviews = BackgroundReviewScheduler(
        review, observability=SimpleNamespace(publish=lambda name, **fields: events.append((name, fields))),
    )
    try:
        bad = reviews.submit(snapshot(turn="bad"))
        good = reviews.submit(snapshot(turn="good"))
        with pytest.raises(ValueError, match="review failed"):
            bad.future.result(timeout=3)
        good.future.result(timeout=3)
        failed = next(fields for name, fields in events if name == "review.failed")
        completed = next(fields for name, fields in events if name == "review.completed")
        assert failed["payload"] == {"parent_trace_id": "trace-bad", "error_type": "ValueError"}
        assert completed["payload"]["parent_trace_id"] == "trace-good"
        assert failed["trace_id"] != completed["trace_id"]
    finally:
        assert reviews.close(timeout=3)


def test_close_cancels_queued_work_and_waits_only_within_budget():
    started, release = threading.Event(), threading.Event()
    running = []

    def review(task):
        running.append(task)
        started.set()
        assert release.wait(3)

    reviews = BackgroundReviewScheduler(review)
    try:
        first = reviews.submit(snapshot(turn="one"))
        assert started.wait(2)
        queued = reviews.submit(snapshot(turn="two"))
        assert not reviews.close(timeout=0)
        assert queued.future.cancelled()
        assert running[0].cancel_event.is_set()
        with pytest.raises(WorkerPoolClosedError):
            reviews.submit(snapshot())
        release.set()
        first.future.result(timeout=3)
        assert len(running) == 1
    finally:
        release.set()
        assert reviews.close(timeout=3)


def test_foreground_returns_while_review_reuses_exact_context_and_independent_history(
    config_factory, agent_factory,
):
    config = config_factory()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    captured, review_requests, results = [], [], []

    def review(task):
        captured.append(task)
        started.set()
        try:
            assert release.wait(3)
            loop, _, adapter, _ = memory_loop(
                config, ScriptedTransport(NormalizedModelResponse(content="reviewed")),
                model_context=task.snapshot.context,
                history=tuple(Message.model_validate(item) for item in task.snapshot.messages),
                cancel=task.cancel_event, workspace=task.snapshot.workspace.cwd,
                turn_id=task.turn_id,
                services=get_tool_services(),
            )
            results.append(loop.run())
            review_requests.extend(adapter.canonical_requests)
        finally:
            finished.set()

    class MutatingUpdater:
        def update(self, snapshot):
            snapshot.messages[0]["content"] = "changed by updater"
            snapshot.result.warnings.append("updater-only warning")

    reviews = BackgroundReviewScheduler(review)
    transport = ScriptedTransport(NormalizedModelResponse(content="parent answer"))
    with agent_factory(
        config, transports={"primary": transport},
        memory_updater=MutatingUpdater(), review_scheduler=reviews,
    ) as agent:
        try:
            result = agent.run_turn_blocking("parent question", session_id="chat")
            assert result.status == "completed"
            assert started.wait(2) and not finished.is_set()
            task, = captured
            assert task.parent_trace_id == result.turn_id
            assert task.snapshot.messages[0]["content"] == "parent question"
            assert "updater-only warning" not in task.snapshot.result.warnings
            main = agent._api_modes.get("scripted").canonical_requests[0]
            release.set()
            assert finished.wait(3)
            assert results[0].status == "completed"
            request, = review_requests
            assert request.system_prompt == main.system_prompt
            assert request.tool_schema_bytes == main.tool_schema_bytes
            assert [m.content for m in request.messages] == ["parent question", "parent answer", "review"]
            assert [m.content for m in agent._store.load_messages("chat")] == ["parent question", "parent answer"]
        finally:
            release.set()
            assert reviews.close(timeout=3)


def test_missing_context_and_async_reviewers_are_rejected():
    async def async_review(task):
        return None

    with pytest.raises(TypeError, match="synchronous"):
        BackgroundReviewScheduler(async_review)
    reviews = BackgroundReviewScheduler(lambda task: None)
    try:
        source = snapshot()
        with pytest.raises(ValueError, match="original model context"):
            reviews.schedule(TurnSnapshot(source.result, source.messages))
    finally:
        assert reviews.close(timeout=3)
