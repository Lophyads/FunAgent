from __future__ import annotations

import pytest
import time

from agentcore import (
    GatewayRuntime, LeasePolicy, Message, NormalizedModelResponse,
    NormalizedProviderError, NullEventSink, ObservabilityHub,
    SQLiteStore, ToolCall,
)
from conftest import ScriptedTransport, scripted_provider_stack
from agentcore.workers import TurnDispatcher


class RecordingAdapter:
    destination = "test:chat"

    def __init__(self, *acks: bool) -> None:
        self.acks = list(acks)
        self.sent: list[tuple[str, str]] = []

    def send(
        self, *, obligation_id: str, destination: str, text: str
    ) -> bool:
        assert destination == self.destination
        self.sent.append((obligation_id, text))
        return self.acks.pop(0) if self.acks else True


def _gateway(config, adapter, transport, **options):
    resolver, api_modes, _ = scripted_provider_stack(
        config, {"primary": transport}
    )
    return GatewayRuntime(
        config, adapter, provider_resolver=resolver, api_modes=api_modes,
        **options,
    )


def test_failed_delivery_replays_without_calling_model(
    config_factory, final_response,
):
    config = config_factory()
    transport = ScriptedTransport(final_response)
    adapter = RecordingAdapter(False)
    with _gateway(config, adapter, transport) as gateway:
        result = gateway.run_turn_blocking("hello", session_id="delivery")
        assert result.status == "completed"
        assert len(transport.requests) == 1
        assert gateway.agent._store.list_pending_deliveries()[0]["state"] == "failed"

    replay_transport = ScriptedTransport()
    replay_adapter = RecordingAdapter(True)
    with _gateway(config, replay_adapter, replay_transport) as gateway:
        assert gateway.agent._store.list_pending_deliveries() == []
        assert gateway.agent._store.list_pending_recoveries(3) == []

    assert len(replay_transport.requests) == 0
    assert len(adapter.sent) == len(replay_adapter.sent) == 1
    assert adapter.sent[0] == replay_adapter.sent[0]


def test_startup_converts_crashed_turn_to_internal_recovery(
    config_factory, agent_factory, final_response,
):
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)}
    ) as agent:
        first = agent.run_turn_blocking("first", session_id="recover")
    assert first.status == "completed"

    store = SQLiteStore(config.database_path)
    store.open()
    lease = store.acquire_lease(
        session_id="recover", owner_id="crashed-process",
        turn_id="crashed-turn", ttl_seconds=30,
    )
    assert lease is not None
    store.begin_turn(
        turn_id="crashed-turn", session_id="recover", context_version=1,
        user_message=Message.user("second"), fence=lease.fence,
    )
    store.release_lease(lease)
    store.close()

    adapter = RecordingAdapter(True)
    transport = ScriptedTransport(
        NormalizedModelResponse(content="continued", finish_reason="stop")
    )
    with _gateway(config, adapter, transport) as gateway:
        recovered = gateway.wait_for_recovery()
        gateway.delivery.drain()
        lifecycle = gateway.agent._store.get_session_lifecycle("recover")
        messages = gateway.agent._store.load_messages("recover")
        crashed = gateway.agent._store.get_turn("crashed-turn")

    assert len(recovered) == 1
    assert recovered[0].status == "completed"
    assert recovered[0].completion_reason == "model_final"
    assert len(transport.requests) == 1
    assert crashed["status"] == "failed"
    assert lifecycle["resume_pending"] == 0
    assert lifecycle["active_turn_id"] is None
    assert sum(message.content == "second" for message in messages) == 1
    assert sum(
        message.metadata.get("message_kind") == "recovery"
        for message in messages
    ) == 1
    assert any(text == "continued" for _, text in adapter.sent)




def test_startup_repair_marks_running_tool_unknown_without_replay(
    config_factory, agent_factory, final_response,
):
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)}
    ) as agent:
        agent.run_turn_blocking("first", session_id="tool-recover")

    store = SQLiteStore(config.database_path)
    store.open()
    lease = store.acquire_lease(
        session_id="tool-recover", owner_id="crashed-process",
        turn_id="tool-crash", ttl_seconds=30,
    )
    assert lease is not None
    store.begin_turn(
        turn_id="tool-crash", session_id="tool-recover", context_version=1,
        user_message=Message.user("run a tool"), fence=lease.fence,
    )
    call = ToolCall(id="uncertain", name="current_time", arguments="{}")
    store.stage_tool_calls(
        "tool-recover", "tool-crash",
        Message.assistant_tools([call], finish_reason="tool_calls"),
        [(call, "possible")], fence=lease.fence,
    )
    assert store.mark_tool_running(
        "tool-crash", "uncertain", fence=lease.fence
    )
    store.release_lease(lease)
    store.close()

    transport = ScriptedTransport(
        NormalizedModelResponse(content="continued", finish_reason="stop")
    )
    with _gateway(config, RecordingAdapter(True), transport) as gateway:
        assert gateway.wait_for_recovery()[0].status == "completed"
        outcomes = gateway.agent._store.get_tool_outcomes("tool-crash")
        messages = gateway.agent._store.load_messages("tool-recover")

    assert len(transport.requests) == 1
    assert len(outcomes) == 1
    assert outcomes[0].state == "unknown"
    assert outcomes[0].effect_disposition == "unknown"
    assert sum(
        message.role == "tool" and message.tool_call_id == "uncertain"
        for message in messages
    ) == 1



def test_failed_recovery_suspends_after_attempt_limit(
    config_factory, agent_factory, final_response,
):
    config = config_factory()
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)}
    ) as agent:
        agent.run_turn_blocking("first", session_id="suspend")

    store = SQLiteStore(config.database_path)
    store.open()
    lease = store.acquire_lease(
        session_id="suspend", owner_id="crashed-process",
        turn_id="suspended-crash", ttl_seconds=30,
    )
    assert lease is not None
    store.begin_turn(
        turn_id="suspended-crash", session_id="suspend",
        context_version=1, user_message=Message.user("continue"),
        fence=lease.fence,
    )
    store.release_lease(lease)
    store.close()

    with _gateway(
        config, RecordingAdapter(), ScriptedTransport(
            NormalizedProviderError("bad request", status_code=400)
        ), max_recovery_attempts=1,
    ) as gateway:
        result = gateway.wait_for_recovery()[0]
        lifecycle = gateway.agent._store.get_session_lifecycle("suspend")

    assert result.status == "failed"
    assert lifecycle["resume_pending"] == 1
    assert lifecycle["resume_suspended"] == 1
    assert lifecycle["resume_attempts"] == 1

    user_transport = ScriptedTransport(
        NormalizedModelResponse(content="user guided", finish_reason="stop")
    )
    with _gateway(
        config, RecordingAdapter(), user_transport,
        max_recovery_attempts=1,
    ) as gateway:
        assert gateway.wait_for_recovery() == []
        assert not user_transport.requests
        user_result = gateway.run_turn_blocking(
            "please continue", session_id="suspend"
        )
        lifecycle = gateway.agent._store.get_session_lifecycle("suspend")
    assert user_result.status == "completed"
    assert lifecycle["resume_pending"] == 0
    assert lifecycle["resume_suspended"] == 0
    assert len(user_transport.requests) == 1



def test_nested_observability_payload_redacts_secrets():
    events = []

    class Observer:
        def observe(self, event):
            events.append(event)

    hub = ObservabilityHub(
        observers=(Observer(),), sinks=(NullEventSink(),)
    )
    try:
        hub.publish(
            "test.event", trace_id="trace",
            payload={"nested": {"api_key": "secret", "safe": "ok"}},
        )
    finally:
        hub.close(1)

    assert events[0].payload["nested"]["api_key"] == "[redacted]"
    assert events[0].payload["nested"]["safe"] == "ok"
    with pytest.raises(TypeError):
        events[0].payload["nested"]["safe"] = "modified"



def test_reserved_recovery_lane_precedes_user_without_using_worker():
    dispatcher = TurnDispatcher(max_workers=1, capacity=3)
    order = []
    try:
        reservation = dispatcher.reserve("session")
        user = dispatcher.submit(
            "session", lambda _cancel: order.append("user")
        )
        other = dispatcher.submit(
            "other", lambda _cancel: order.append("other")
        )
        other.future.result(timeout=2)
        assert not user.future.done()
        recovery = reservation.commit(
            lambda _cancel: order.append("recovery")
        )
        recovery.future.result(timeout=2)
        user.future.result(timeout=2)
        assert order == ["other", "recovery", "user"]
    finally:
        dispatcher.close(timeout=2)



def test_quick_restart_recovers_after_stale_lease_expires(
    config_factory, agent_factory, final_response,
):
    config = config_factory().model_copy(update={
        "lease": LeasePolicy(
            ttl_seconds=0.3, renew_interval_seconds=0.1,
            wait_timeout_seconds=1,
        ),
    })
    with agent_factory(
        config, transports={"primary": ScriptedTransport(final_response)}
    ) as agent:
        agent.run_turn_blocking("first", session_id="quick-restart")

    store = SQLiteStore(config.database_path)
    store.open()
    lease = store.acquire_lease(
        session_id="quick-restart", owner_id="dead-process",
        turn_id="quick-crash", ttl_seconds=0.25,
    )
    assert lease is not None
    store.begin_turn(
        turn_id="quick-crash", session_id="quick-restart",
        context_version=1, user_message=Message.user("second"),
        fence=lease.fence,
    )
    store.close()

    transport = ScriptedTransport(
        NormalizedModelResponse(content="resumed", finish_reason="stop")
    )
    with _gateway(config, RecordingAdapter(True), transport) as gateway:
        assert gateway.wait_for_recovery() == []
        deadline = time.monotonic() + 3
        while not gateway._recovery_submissions and time.monotonic() < deadline:
            time.sleep(0.05)
        results = gateway.wait_for_recovery()
        lifecycle = gateway.agent._store.get_session_lifecycle(
            "quick-restart"
        )

    assert len(results) == 1
    assert results[0].status == "completed"
    assert lifecycle["resume_pending"] == 0
    assert len(transport.requests) == 1



def test_gateway_reports_prior_clean_shutdown(config_factory):
    config = config_factory()
    with _gateway(
        config, RecordingAdapter(), ScriptedTransport()
    ) as gateway:
        assert gateway.last_shutdown_clean is True

    store = SQLiteStore(config.database_path)
    store.open()
    assert store.record_runtime_start("simulated-dead-runtime") is True
    store.close()

    with _gateway(
        config, RecordingAdapter(), ScriptedTransport()
    ) as gateway:
        assert gateway.last_shutdown_clean is False
