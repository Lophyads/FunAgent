from __future__ import annotations

import sqlite3

import pytest

from agentcore import NormalizedModelResponse, SQLiteStore, StorageError, ToolCall
from agentcore.conversation_loop import ConversationLoop
from agentcore.turn_state import FunctionPhase, LoopState, PhaseAction, PhaseResult
from conftest import ScriptedTransport


def test_turn_setup_phases_follow_their_responsibilities(
    config_factory, agent_factory, final_response,
):
    phases = []

    class PhaseObserver:
        def observe(self, event):
            if event.name == "turn.phase_started":
                phases.append(event.phase)

    with agent_factory(
        config_factory(),
        transports={"primary": ScriptedTransport(final_response)},
        observers=[PhaseObserver()],
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="phase-order")

    assert result.status == "completed"
    assert phases[:5] == [
        "acquire_lease",
        "recover_history",
        "resolve_session_context",
        "establish_turn",
        "load_messages",
    ]
    assert "admission" not in phases


def test_message_load_failure_still_finalizes_persisted_turn(
    config_factory, agent_factory,
):
    class FailingLoadStore(SQLiteStore):
        def load_messages(self, session_id):
            raise StorageError("injected message load failure")

    config = config_factory()
    store = FailingLoadStore(config.database_path)
    transport = ScriptedTransport()
    with agent_factory(
        config, transports={"primary": transport}, store=store,
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="load-fails")
        persisted = store.get_turn(result.turn_id)
        lifecycle = store.get_session_lifecycle("load-fails")

    assert result.status == "failed"
    assert persisted["status"] == "failed"
    assert lifecycle["active_turn_id"] is None
    assert not transport.requests



def test_phase_actions_and_missing_prerequisites_are_explicit():
    assert [action.value for action in PhaseAction] == [
        "fallthrough", "break", "return", "continue",
    ]
    assert PhaseResult().action is PhaseAction.FALLTHROUGH
    for removed in ("PROCEED", "NEXT_ITERATION", "STOP_TURN"):
        assert not hasattr(PhaseAction, removed)

    state = LoopState("session", "turn", "trace")
    for name in ("lease", "context", "request", "response"):
        with pytest.raises(RuntimeError, match=f"sample_phase requires {name}"):
            getattr(state, f"require_{name}")("sample_phase")


def test_missing_tool_lease_fails_before_operation_submission():
    loop = object.__new__(ConversationLoop)
    state = LoopState("session", "turn", "trace")
    state.context = object()
    submitted = []

    def submit(*args, **kwargs):
        submitted.append(True)
        raise AssertionError("operation must not be submitted")

    loop.call_operation = submit
    with pytest.raises(RuntimeError, match="execute_tool requires lease"):
        loop._execute_tool(
            None, state, ToolCall(id="call", name="example", arguments="{}"),
        )
    assert not submitted


def test_missing_request_and_response_fail_in_their_phases():
    loop = object.__new__(ConversationLoop)
    state = LoopState("session", "turn", "trace")
    with pytest.raises(RuntimeError, match="provider_attempts requires request"):
        loop._run_provider_attempts(None, state)
    with pytest.raises(RuntimeError, match="assemble_request requires context"):
        loop._assemble_phase()(None, state)
    with pytest.raises(RuntimeError, match="tool_round requires response"):
        loop._tool_round_phase()(None, state)
    with pytest.raises(RuntimeError, match="finish_text requires response"):
        loop._finish_text_phase()(None, state)
    state.response = NormalizedModelResponse(content="done", finish_reason="stop")
    with pytest.raises(RuntimeError, match="finish_text requires lease"):
        loop._finish_text_phase()(None, state)


def test_tool_continue_and_text_break_persist_and_release(
    config_factory, agent_factory, final_response,
):
    actions = {}

    class PhaseObserver:
        def observe(self, event):
            if event.name == "turn.phase_finished":
                actions[event.phase] = event.payload["action"]

    config = config_factory()
    transport = ScriptedTransport(
        NormalizedModelResponse(
            tool_calls=[ToolCall(
                id="call-1", name="calculator",
                arguments='{"expression":"1+1"}',
            )],
            finish_reason="tool_calls",
        ),
        final_response,
    )
    with agent_factory(
        config, transports={"primary": transport},
        observers=[PhaseObserver()],
    ) as agent:
        result = agent.run_turn_blocking("calculate", session_id="phase-actions")
        persisted = agent._store.get_turn(result.turn_id)
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id, turn_id FROM session_leases WHERE session_id = ?",
            ("phase-actions",),
        ).fetchone()

    assert result.status == persisted["status"] == "completed"
    assert result.iterations == 2
    assert actions["tool_round"] == "continue"
    assert actions["finish_text"] == "break"
    assert lease == (None, None)


def test_return_verdict_still_runs_cleanup(
    config_factory, agent_factory, final_response, monkeypatch,
):
    original = ConversationLoop._finish_text_phase

    def return_after_finish(self):
        phase = original(self)

        def finish(runtime, state):
            phase(runtime, state)
            return PhaseResult(PhaseAction.RETURN)

        return FunctionPhase(
            phase.name, finish, phase.validator, phase.check_active,
        )

    monkeypatch.setattr(ConversationLoop, "_finish_text_phase", return_after_finish)
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="return-verdict")
        persisted = agent._store.get_turn(result.turn_id)
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id, turn_id FROM session_leases WHERE session_id = ?",
            ("return-verdict",),
        ).fetchone()

    assert result.status == persisted["status"] == "completed"
    assert lease == (None, None)


def test_terminal_fallthrough_fails_and_cleanup_finalizes(
    config_factory, agent_factory, final_response, monkeypatch,
):
    monkeypatch.setattr(
        ConversationLoop, "_finish_text_phase",
        lambda self: FunctionPhase(
            "finish_text", lambda runtime, state: PhaseResult(),
        ),
    )
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="bad-verdict")
        persisted = agent._store.get_turn(result.turn_id)
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id, turn_id FROM session_leases WHERE session_id = ?",
            ("bad-verdict",),
        ).fetchone()

    assert result.status == persisted["status"] == "failed"
    assert result.error.code == "turn_failed"
    assert "unexpected action fallthrough" in result.error.message
    assert lease == (None, None)


def test_admission_requires_fallthrough(
    config_factory, agent_factory, final_response, monkeypatch,
):
    monkeypatch.setattr(
        ConversationLoop, "_acquire_lease_phase",
        lambda self: FunctionPhase(
            "acquire_lease",
            lambda runtime, state: PhaseResult(PhaseAction.BREAK),
            check_active=False,
        ),
    )
    transport = ScriptedTransport(final_response)
    with agent_factory(
        config_factory(), transports={"primary": transport},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="bad-admission")

    assert result.status == "failed"
    assert result.error.code == "turn_failed"
    assert "acquire_lease returned unexpected action break" in result.error.message
    assert not transport.requests


def test_cleanup_rejects_non_fallthrough_and_keeps_releasing_lease(
    config_factory, agent_factory, final_response, monkeypatch,
):
    monkeypatch.setattr(
        ConversationLoop, "_repair",
        lambda self, runtime, state: PhaseResult(PhaseAction.BREAK),
    )
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="bad-cleanup")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id, turn_id FROM session_leases WHERE session_id = ?",
            ("bad-cleanup",),
        ).fetchone()

    assert result.status == "completed"
    assert any(
        "repair_unresolved_tools returned unexpected action break" in warning
        for warning in result.warnings
    )
    assert lease == (None, None)
