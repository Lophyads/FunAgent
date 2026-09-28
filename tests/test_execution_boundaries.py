from __future__ import annotations

from types import SimpleNamespace
import sqlite3

import pytest

from agentcore import Message, NormalizedModelResponse, PreparedConversation, TurnTrigger
from tools.config import ToolSystemConfig
from tools.lifecycle import SessionResourceKey
from agentcore.runtime_cwd import resolve_workspace
from tools.runtime import ToolSystemNotInitialized, get_tool_services
from tools.services import ToolServices
from test_loop_persistence import memory_loop, tool_response
from test_tool_execution_resources import StatefulBrowser
from conftest import ScriptedTransport


def test_memory_loop_uses_explicit_services_without_process_initialization(config_factory):
    config = config_factory()
    browser = StatefulBrowser()
    services = ToolServices(
        ToolSystemConfig.from_agent_config(config).normalized(),
        provider_factories={"browser": lambda: browser},
    )
    try:
        with pytest.raises(ToolSystemNotInitialized):
            get_tool_services()
        assert services.available("browser")
        for index in (1, 2):
            turn_id = f"read-only-{index}"
            key = SessionResourceKey("shared", "default", "chat")
            loop, _, _, _ = memory_loop(
                config, ScriptedTransport(tool_response("browser"), NormalizedModelResponse(content="done")),
                services=services, resource_key=key, turn_id=turn_id,
                handler=lambda args, context: context.services.browser().action(context.resource_key, "visit", [], context),
            )
            assert loop.run().status == "completed"
            assert list(browser.visits.values()) == [index]
        assert not config.database_path.exists()
        with pytest.raises(ToolSystemNotInitialized):
            get_tool_services()
    finally:
        assert services.close(timeout=2).completed


def test_preparation_returns_data_and_loop_detaches_it(config_factory):
    config = config_factory()
    _, context, _, _ = memory_loop(config, ScriptedTransport())
    prepared = PreparedConversation(
        context, (Message.user("input", metadata={"nested": [1]}),),
        resolve_workspace(None, None, get_tool_services().config.workspace),
        SessionResourceKey("test", "default", "chat"),
    )

    def prepare(runtime):
        return prepared

    loop, _, _, _ = memory_loop(config, ScriptedTransport(NormalizedModelResponse(content="done")), prepare=prepare)
    assert loop.run().status == "completed"
    prepared.messages[0].metadata["nested"].append(2)
    assert loop.messages[0].metadata == {"nested": [1]}
    assert loop._state.submitted_message == loop.messages[0]
    with pytest.raises(RuntimeError, match="already been admitted"):
        loop._state.accept(prepared)


def test_failed_preparation_creates_no_tool_resources(config_factory):
    config = config_factory()

    def prepare(runtime):
        raise ValueError("context build failed")

    transport = ScriptedTransport()
    loop, _, _, _ = memory_loop(config, transport, prepare=prepare)
    result = loop.run()
    assert result.status == "failed"
    assert not loop.messages and not transport.requests
    assert not get_tool_services()._providers


def test_release_survives_failure_before_cleanup_phase_action(config_factory):
    loop, _, _, _ = memory_loop(config_factory(), ScriptedTransport(NormalizedModelResponse(content="done")))

    def publish(name, **fields):
        if name == "turn.phase_started" and fields.get("phase") == "release_execution":
            raise RuntimeError("observation unavailable")
        return ()

    loop.phases.observability = SimpleNamespace(publish=publish)
    assert loop.run().status == "completed"
    assert loop._state.released


def test_recovery_claim_is_finished_when_context_preparation_fails(config_factory, agent_factory, monkeypatch):
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(NormalizedModelResponse(content="done"))},
    ) as agent:
        assert agent.run_turn_blocking("first", session_id="recover").status == "completed"
        store = agent._store
        lease = store.acquire_lease(
            session_id="recover", owner_id="previous", turn_id="interrupted", ttl_seconds=30,
        )
        store.append_user_message("recover", Message.user("unfinished"), fence=lease.fence)
        with sqlite3.connect(config.database_path) as connection:
            connection.execute("UPDATE session_leases SET expires_at=0 WHERE session_id='recover'")
        assert store.mark_stale_leases(frozenset({"recover"})) == ["recover"]

        def fail(*args, **kwargs):
            raise ValueError("context unavailable")

        monkeypatch.setattr(agent._session_contexts, "build", fail)
        result = agent.submit_recovery(TurnTrigger(kind="internal_recovery", session_id="recover")).future.result(timeout=3)
        assert result.status == "failed"
        lifecycle = store.get_session_recovery("recover")
        assert lifecycle["resume_attempts"] == 1
        assert lifecycle["resume_pending"] == 1
        assert lifecycle["resume_not_before"] is not None
        assert "context unavailable" in result.error.message
        assert [m.content for m in store.load_messages("recover")] == ["first", "done", "unfinished"]
