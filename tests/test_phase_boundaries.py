from __future__ import annotations

from agentcore.provider_runner import ProviderRunner

import sqlite3
from types import SimpleNamespace
import inspect

import pytest

from agentcore import (
    LeaseAcquireTimeout, LeaseLostError, Message, NormalizedModelResponse,
    NormalizedProviderError, RunBudgetExceeded, SQLiteStore,
    StorageError, ToolCall, TurnCancelled, TurnTrigger,
    ToolExecutor,
)
from agentcore.conversation_loop import ConversationLoop
from agentcore.lease import LeaseScope
from agentcore.session_context import RecoverySessionMissing, SessionContextFactory
from agentcore.turn_phase import (
    FunctionPhase, PhaseAction, PhaseResult, ProviderExhausted,
    ProviderRequestAborted, TurnPhases,
)
from agentcore.turn_state import LoopState
from conftest import ScriptedApiModeAdapter, ScriptedTransport


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
    assert phases[:2] == ["acquire_execution", "prepare_conversation"]
    assert "admission" not in phases


def test_conversation_loop_only_orchestrates_phase_interfaces():
    assert {
        name for name, value in vars(ConversationLoop).items()
        if inspect.isfunction(value)
    } == {"__init__", "run", "_run_iterations"}
    source = inspect.getsource(ConversationLoop)
    assert ".store." not in source
    assert ".transport." not in source
    assert ".handler(" not in source


def test_message_load_failure_still_finalizes_persisted_turn(
    config_factory, agent_factory, monkeypatch,
):
    outer_errors = []
    original = TurnPhases.handle_outer_loop_error

    def outer(self, runtime, state, exc):
        outer_errors.append(exc)
        return original(self, runtime, state, exc)

    monkeypatch.setattr(TurnPhases, "handle_outer_loop_error", outer)

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
        tail = store.latest_message("load-fails")

    assert result.status == "failed"
    assert result.completion_reason == "turn_failed"
    assert len(outer_errors) == 1
    assert isinstance(outer_errors[0], StorageError)
    assert tail is not None and tail[1].role == "user"
    assert not transport.requests



def test_phase_actions_and_missing_prerequisites_are_explicit():
    assert [action.value for action in PhaseAction] == [
        "fallthrough", "break", "return", "continue",
    ]
    assert PhaseResult().action is PhaseAction.FALLTHROUGH
    for removed in ("PROCEED", "NEXT_ITERATION", "STOP_TURN"):
        assert not hasattr(PhaseAction, removed)

    state = LoopState("session", "turn", "trace")
    for name in ("context", "request", "response"):
        with pytest.raises(RuntimeError, match=f"sample_phase requires {name}"):
            getattr(state, f"require_{name}")("sample_phase")


def test_tool_round_validation_ignores_matching_ids_in_older_history():
    call = ToolCall(id="reused-id", name="current_time", arguments="{}")
    old_assistant = Message.assistant_tools([call])
    old_result = Message.tool_result(
        "old result", tool_call_id=call.id, tool_name=call.name,
        effect_disposition="none", reply_to_message_id=1,
        tool_outcome_state="succeeded",
    )
    current_assistant = Message.assistant_tools([call])
    state = LoopState("session", "turn", "trace")
    state.response = NormalizedModelResponse(
        tool_calls=[call], finish_reason="tool_calls",
    )
    state.assistant_message = current_assistant
    state.messages = [old_assistant, old_result, current_assistant]
    phase = object.__new__(TurnPhases)._tool_round_phase()
    verdict = PhaseResult(PhaseAction.CONTINUE)

    with pytest.raises(RuntimeError, match="unresolved tool calls"):
        phase.validate_postconditions(state, verdict)

    state.messages.append(Message.tool_result(
        "new result", tool_call_id=call.id, tool_name=call.name,
        effect_disposition="none", reply_to_message_id=3,
        tool_outcome_state="succeeded",
    ))
    phase.validate_postconditions(state, verdict)


def test_lost_execution_ownership_fails_before_operation_submission():
    executor = object.__new__(ToolExecutor)
    state = LoopState("session", "turn", "trace")
    state.context = object()
    submitted = []

    def submit(*args, **kwargs):
        submitted.append(True)
        raise AssertionError("operation must not be submitted")

    def check_active():
        raise LeaseLostError("execution ownership was lost")

    runtime = SimpleNamespace(call_operation=submit, check_active=check_active)
    with pytest.raises(LeaseLostError, match="execution ownership was lost"):
        executor.execute(
            runtime, state,
            ToolCall(id="call", name="example", arguments="{}"),
        )
    assert not submitted


def test_missing_request_and_response_fail_in_their_phases():
    loop = object.__new__(TurnPhases)
    state = LoopState("session", "turn", "trace")
    runtime = SimpleNamespace(check_active=lambda: None)
    with pytest.raises(RuntimeError, match="provider_attempts requires request"):
        ProviderRunner.run(None, None, state)
    with pytest.raises(RuntimeError, match="assemble_request requires context"):
        loop._assemble_phase()(runtime, state)
    with pytest.raises(RuntimeError, match="tool_round requires response"):
        loop._tool_round_phase()(runtime, state)
    with pytest.raises(RuntimeError, match="finish_text requires response"):
        loop._finish_text_phase()(runtime, state)


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
        tail = agent._store.latest_message("phase-actions")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("phase-actions",),
        ).fetchone()

    assert result.status == "completed"
    assert tail is not None and tail[1].role == "assistant"
    assert result.iterations == 2
    assert actions["tool_round"] == "continue"
    assert actions["finish_text"] == "break"
    assert lease == (None,)


def test_return_verdict_still_runs_cleanup(
    config_factory, agent_factory, final_response, monkeypatch,
):
    original = TurnPhases._finish_text_phase

    def return_after_finish(self):
        phase = original(self)

        def finish(runtime, state):
            phase(runtime, state)
            return PhaseResult(PhaseAction.RETURN)

        return FunctionPhase(
            phase.name, finish, phase.validator, phase.check_active,
        )

    monkeypatch.setattr(TurnPhases, "_finish_text_phase", return_after_finish)
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="return-verdict")
        tail = agent._store.latest_message("return-verdict")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("return-verdict",),
        ).fetchone()

    assert result.status == "completed"
    assert tail is not None and tail[1].role == "assistant"
    assert lease == (None,)


def test_terminal_fallthrough_fails_and_cleanup_finalizes(
    config_factory, agent_factory, final_response, monkeypatch,
):
    monkeypatch.setattr(
        TurnPhases, "_finish_text_phase",
        lambda self: FunctionPhase(
            "finish_text", lambda runtime, state: PhaseResult(),
        ),
    )
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="bad-verdict")
        tail = agent._store.latest_message("bad-verdict")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("bad-verdict",),
        ).fetchone()

    assert result.status == "failed"
    assert result.completion_reason == "turn_failed"
    assert tail is not None and tail[1].role == "user"
    assert result.error.code == "turn_failed"
    assert "unexpected action fallthrough" in result.error.message
    assert lease == (None,)


def test_admission_break_requires_structured_terminal_state(
    config_factory, agent_factory, final_response, monkeypatch,
):
    monkeypatch.setattr(
        TurnPhases, "_acquire_execution_phase",
        lambda self: FunctionPhase(
            "acquire_execution",
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
    assert "BREAK requires a structured terminal outcome" in result.error.message
    assert not transport.requests


def test_cleanup_break_keeps_releasing_lease(
    config_factory, agent_factory, final_response, monkeypatch,
):
    monkeypatch.setattr(
        TurnPhases, "_repair",
        lambda self, runtime, state: PhaseResult(PhaseAction.BREAK),
    )
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="bad-cleanup")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("bad-cleanup",),
        ).fetchone()

    assert result.status == "completed"
    assert any(
        "cleanup phase repair_unresolved_tools ended early" in warning
        for warning in result.warnings
    )
    assert lease == (None,)


def test_cleanup_failure_preserves_original_error_and_releases_lease(
    config_factory, agent_factory, monkeypatch,
):
    def fail_provider(self, runtime, state):
        raise ProviderExhausted([{"route": "primary", "attempt": 1}])

    def fail_repair(self, runtime, state):
        raise RuntimeError("repair unavailable")

    monkeypatch.setattr(ProviderRunner, "run", fail_provider)
    monkeypatch.setattr(TurnPhases, "_repair", fail_repair)
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport()},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="cleanup-fault")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("cleanup-fault",),
        ).fetchone()

    assert result.status == "failed"
    assert result.completion_reason == "providers_exhausted"
    assert result.error is not None
    assert result.error.details["failures"] == [
        {"route": "primary", "attempt": 1}
    ]
    assert any("repair unavailable" in warning for warning in result.warnings)
    assert lease == (None,)


@pytest.mark.parametrize(
    ("failure", "status", "reason", "error_code"),
    [
        (TurnCancelled("cancelled"), "interrupted", "cancelled", "cancelled"),
        (
            RunBudgetExceeded("budget"), "interrupted",
            "run_budget_exhausted", "run_budget_exhausted",
        ),
        (LeaseLostError("lost"), "interrupted", "lease_lost", "lease_lost"),
        (
            ProviderExhausted([{"route": "primary", "attempt": 1}]),
            "failed", "providers_exhausted", "providers_exhausted",
        ),
        (
            ProviderRequestAborted(
                "primary", NormalizedProviderError(
                    "bad request", status_code=400, error_type="provider_error",
                ),
            ),
            "failed", "provider_request_aborted", "provider_error",
        ),
    ],
)
def test_expected_provider_phase_errors_break_without_outer_handler(
    config_factory, agent_factory, monkeypatch,
    failure, status, reason, error_code,
):
    outer_errors = []
    finished = []
    original = TurnPhases.handle_outer_loop_error

    def outer(self, runtime, state, exc):
        outer_errors.append(exc)
        return original(self, runtime, state, exc)

    class PhaseObserver:
        def observe(self, event):
            if event.name == "turn.phase_finished":
                finished.append((event.phase, event.payload["action"]))

    def fail_provider(self, runtime, state):
        raise failure

    monkeypatch.setattr(ProviderRunner, "run", fail_provider)
    monkeypatch.setattr(TurnPhases, "handle_outer_loop_error", outer)
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport()},
        observers=[PhaseObserver()],
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="expected-error")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("expected-error",),
        ).fetchone()

    assert result.status == status
    assert result.completion_reason == reason
    assert result.error is not None and result.error.code == error_code
    assert not outer_errors
    assert ("provider_attempts", "break") in finished
    assert ("release_execution", "fallthrough") in finished
    assert lease == (None,)
    if isinstance(failure, ProviderExhausted):
        assert result.error.details["failures"] == failure.failures
    if isinstance(failure, ProviderRequestAborted):
        assert result.route_used == "primary"
        assert result.error.details["status_code"] == 400


@pytest.mark.parametrize(
    ("failure", "method_name", "reason"),
    [
        (
            LeaseAcquireTimeout("busy"), "acquire",
            "lease_timeout",
        ),
    ],
)
def test_expected_setup_error_breaks_and_cleans_up(
    config_factory, agent_factory, monkeypatch, failure, method_name, reason,
):
    outer_errors = []
    finished = []
    original = TurnPhases.handle_outer_loop_error

    def outer(self, runtime, state, exc):
        outer_errors.append(exc)
        return original(self, runtime, state, exc)

    def raise_failure(*args, **kwargs):
        raise failure

    class PhaseObserver:
        def observe(self, event):
            if event.name == "turn.phase_finished":
                finished.append((event.phase, event.payload["action"]))

    target = (
        LeaseScope if method_name == "acquire"
        else SessionContextFactory
    )
    monkeypatch.setattr(target, method_name, raise_failure)
    monkeypatch.setattr(TurnPhases, "handle_outer_loop_error", outer)
    transport = ScriptedTransport()
    with agent_factory(
        config_factory(), transports={"primary": transport},
        observers=[PhaseObserver()],
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="setup-break")

    assert result.status == "failed"
    assert result.completion_reason == reason
    assert result.error is not None and result.error.code == reason
    assert not outer_errors and not transport.requests
    assert any(action == "break" for _, action in finished)
    assert ("release_execution", "fallthrough") in finished


def test_recovery_not_eligible_is_a_phase_break(
    config_factory, agent_factory, monkeypatch,
):
    outer_errors = []
    original = TurnPhases.handle_outer_loop_error

    def outer(self, runtime, state, exc):
        outer_errors.append(exc)
        return original(self, runtime, state, exc)

    monkeypatch.setattr(TurnPhases, "handle_outer_loop_error", outer)
    with agent_factory(
        config_factory(), transports={"primary": ScriptedTransport()},
    ) as agent:
        result = agent.submit_recovery(
            TurnTrigger(kind="internal_recovery", session_id="missing-recovery")
        ).future.result(timeout=5)

    assert result.status == "failed"
    assert result.completion_reason == "recovery_not_eligible"
    assert not outer_errors


def test_recovery_session_missing_is_a_phase_break(
    config_factory, agent_factory, monkeypatch,
):
    outer_errors = []
    original = TurnPhases.handle_outer_loop_error

    def outer(self, runtime, state, exc):
        outer_errors.append(exc)
        return original(self, runtime, state, exc)

    def missing_session(*args, **kwargs):
        raise RecoverySessionMissing("recovery session is absent")

    monkeypatch.setattr(TurnPhases, "handle_outer_loop_error", outer)
    monkeypatch.setattr(
        SQLiteStore, "claim_recovery", lambda self, *args, **kwargs: True
    )
    monkeypatch.setattr(SessionContextFactory, "build", missing_session)
    with agent_factory(
        config_factory(), transports={"primary": ScriptedTransport()},
    ) as agent:
        result = agent.submit_recovery(
            TurnTrigger(kind="internal_recovery", session_id="absent-session")
        ).future.result(timeout=5)

    assert result.status == "failed"
    assert result.completion_reason == "recovery_session_missing"
    assert not outer_errors


def test_unexpected_adapter_fault_uses_outer_handler_once(
    config_factory, agent_factory, final_response, monkeypatch,
):
    outer_errors = []
    original = TurnPhases.handle_outer_loop_error

    def outer(self, runtime, state, exc):
        outer_errors.append(exc)
        return original(self, runtime, state, exc)

    def invalid_decode(self, response, runtime):
        raise ValueError("invalid adapter output")

    monkeypatch.setattr(ScriptedApiModeAdapter, "decode", invalid_decode)
    monkeypatch.setattr(TurnPhases, "handle_outer_loop_error", outer)
    config = config_factory()
    transport = ScriptedTransport(final_response)
    with agent_factory(
        config, transports={"primary": transport},
    ) as agent:
        result = agent.run_turn_blocking("hello", session_id="bad-adapter")
    with sqlite3.connect(config.database_path) as connection:
        lease = connection.execute(
            "SELECT owner_id FROM session_leases WHERE session_id = ?",
            ("bad-adapter",),
        ).fetchone()

    assert result.status == "failed"
    assert result.completion_reason == "turn_failed"
    assert len(outer_errors) == 1
    assert isinstance(outer_errors[0], ValueError)
    assert result.provider_attempts == 1
    assert len(transport.requests) == 1
    assert lease == (None,)
