from __future__ import annotations

import pytest
import time
import sqlite3
import threading

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


def _expire_lease(database_path, session_id):
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE session_leases SET expires_at=? WHERE session_id=?",
            (time.time() - 1, session_id),
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
        gateway.delivery.submit_drain().result(timeout=3)
        assert gateway.agent._store.list_pending_deliveries() == []
        assert gateway.agent._store.list_pending_recoveries(3) == []

    assert len(replay_transport.requests) == 0
    assert len(adapter.sent) == len(replay_adapter.sent) == 1
    assert adapter.sent[0] == replay_adapter.sent[0]


def test_attach_converts_crashed_turn_to_internal_recovery(
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
    store.append_user_message("recover", Message.user("second"), fence=lease.fence)
    store.close()
    _expire_lease(config.database_path, "recover")

    adapter = RecordingAdapter(True)
    transport = ScriptedTransport(
        NormalizedModelResponse(content="continued", finish_reason="stop")
    )
    with _gateway(config, adapter, transport) as gateway:
        recovered = gateway.attach_session("recover").future.result(timeout=3)
        gateway.delivery.drain()
        lifecycle = gateway.agent._store.get_session_recovery("recover")
        messages = gateway.agent._store.load_messages("recover")

    assert recovered.status == "completed"
    assert recovered.completion_reason == "model_final"
    assert len(transport.requests) == 1
    assert lifecycle["resume_pending"] == 0
    assert sum(message.content == "second" for message in messages) == 1
    assert sum(message.kind == "internal_recovery" for message in messages) == 0
    assert any(text == "continued" for _, text in adapter.sent)


def test_attach_repairs_running_tool_without_replay(
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
    store.append_user_message(
        "tool-recover", Message.user("run a tool"), fence=lease.fence,
    )
    call = ToolCall(id="uncertain", name="current_time", arguments="{}")
    store.append_tool_calls(
        "tool-recover",
        Message.assistant_tools([call], tool_call_effects={"uncertain": "possible"}),
        fence=lease.fence,
    )
    store.close()
    _expire_lease(config.database_path, "tool-recover")

    transport = ScriptedTransport(
        NormalizedModelResponse(content="continued", finish_reason="stop")
    )
    with _gateway(config, RecordingAdapter(True), transport) as gateway:
        assert gateway.attach_session("tool-recover").future.result(timeout=3).status == "completed"
        messages = gateway.agent._store.load_messages("tool-recover")

    assert len(transport.requests) == 1
    outcomes = [message for message in messages if message.role == "tool"]
    assert len(outcomes) == 1
    assert outcomes[0].tool_outcome_state == "unknown"
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
    store.append_user_message("suspend", Message.user("continue"), fence=lease.fence)
    store.close()
    _expire_lease(config.database_path, "suspend")

    with _gateway(
        config, RecordingAdapter(), ScriptedTransport(
            NormalizedProviderError("bad request", status_code=400)
        ), max_recovery_attempts=1,
    ) as gateway:
        result = gateway.attach_session("suspend").future.result(timeout=3)
        lifecycle = gateway.agent._store.get_session_recovery("suspend")

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
        assert gateway.attach_session("suspend") is None
        assert not user_transport.requests
        user_result = gateway.run_turn_blocking(
            "please continue", session_id="suspend"
        )
        lifecycle = gateway.agent._store.get_session_recovery("suspend")
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
    store.append_user_message(
        "quick-restart", Message.user("second"), fence=lease.fence,
    )
    store.close()

    entered, release = threading.Event(), threading.Event()

    def resume(request):
        entered.set()
        assert release.wait(3)
        return NormalizedModelResponse(content="resumed", finish_reason="stop")

    transport = ScriptedTransport(resume)
    try:
        with _gateway(
            config, RecordingAdapter(True), transport,
            recovery_policy="owned", owns_database=True,
        ) as gateway:
            assert entered.wait(3)
            recovery = gateway.attach_session("quick-restart")
            release.set()
            assert recovery.future.result(timeout=3).status == "completed"
            lifecycle = gateway.agent._store.get_session_recovery("quick-restart")
    finally:
        release.set()

    assert lifecycle["resume_pending"] == 0
    assert len(transport.requests) == 1


def test_normal_shutdown_leaves_no_pending_recovery(config_factory, final_response):
    config = config_factory()
    with _gateway(
        config, RecordingAdapter(), ScriptedTransport(final_response)
    ) as gateway:
        assert gateway.run_turn_blocking("hello", session_id="clean").status == "completed"

    store = SQLiteStore(config.database_path)
    store.open()
    assert store.mark_stale_leases() == []
    assert store.list_pending_recoveries(3) == []
    store.close()
