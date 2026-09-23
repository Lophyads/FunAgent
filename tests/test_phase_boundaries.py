from __future__ import annotations

from agentcore import SQLiteStore, StorageError
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

