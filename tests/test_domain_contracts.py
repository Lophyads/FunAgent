from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from agentcore import (
    CanonicalModelRequest,
    Message,
    NormalizedModelResponse,
    ToolCall,
    ToolRegistry,
    ToolOutcome,
    ToolEntry,
    TurnError,
    TurnResult,
)

from conftest import ScriptedTransport


class EchoArgs(BaseModel):
    text: str


def test_message_named_constructors_round_trip():
    call = ToolCall(id="call-1", name="calculator", arguments='{"expression":"1+1"}')
    messages = [
        Message.user("question", api_content="question with context", metadata={"a": 1}),
        Message.assistant_text("answer"),
        Message.assistant_tools([call], tool_call_effects={"call-1": "none"}),
        Message.tool_result(
            '{"ok":true}', tool_call_id="call-1", tool_name="calculator",
            effect_disposition="none", reply_to_message_id=3,
            tool_outcome_state="succeeded",
        ),
    ]

    assert [Message.model_validate(message.model_dump()) for message in messages] == messages
    assert messages[0].to_wire()["content"] == "question with context"
    assert messages[2].to_wire()["tool_calls"][0]["id"] == "call-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "user"},
        {"role": "user", "content": "x", "tool_call_id": "c"},
        {"role": "assistant"},
        {"role": "assistant", "content": "x", "api_content": "forbidden"},
        {"role": "assistant", "tool_calls": []},
        {"role": "assistant", "content": "x", "tool_call_effects": {"c": "none"}},
        {
            "role": "assistant", "tool_calls": [
                {"id": "same", "name": "calculator"},
                {"id": "same", "name": "calculator"},
            ],
        },
        {"role": "tool", "content": "{}"},
        {
            "role": "tool", "content": "{}", "tool_call_id": "c",
            "tool_name": "calculator", "effect_disposition": "none",
            "finish_reason": "stop",
        },
    ],
)
def test_message_rejects_illegal_role_shapes(payload):
    with pytest.raises(ValidationError):
        Message.model_validate(payload)


@pytest.mark.parametrize(
    ("call_id", "name"),
    [(" ", "calculator"), ("id", ""), ("id", "bad name"), ("id", "1starts")],
)
def test_tool_call_rejects_invalid_identity(call_id, name):
    with pytest.raises(ValidationError):
        ToolCall(id=call_id, name=name)


def test_normalized_response_requires_semantic_content_and_unique_tool_ids():
    with pytest.raises(ValidationError, match="text or tool calls"):
        NormalizedModelResponse()
    with pytest.raises(ValidationError, match="must be unique"):
        NormalizedModelResponse(tool_calls=[
            ToolCall(id="same", name="calculator"),
            ToolCall(id="same", name="current_time"),
        ])
    assert NormalizedModelResponse(content="answer").content == "answer"


@pytest.mark.parametrize(
    ("side_effects", "state", "effect", "error_type"),
    [
        ("none", "succeeded", "none", None),
        ("possible", "succeeded", "confirmed", None),
        ("none", "failed", "not_applied", "invalid_arguments"),
        ("possible", "denied", "not_applied", "policy_denied"),
        ("possible", "not_executed", "not_applied", "cancelled"),
        ("none", "unknown", "none", "timeout"),
        ("possible", "unknown", "unknown", "timeout"),
    ],
)
def test_tool_outcome_accepts_legal_state_matrix(
    side_effects, state, effect, error_type,
):
    outcome = ToolOutcome(
        tool_call_id="call", tool_name="tool", side_effects=side_effects,
        state=state, effect_disposition=effect, error_type=error_type,
    )
    assert outcome.effect_disposition == effect


@pytest.mark.parametrize(
    ("side_effects", "state", "effect", "error_type"),
    [
        ("possible", "succeeded", "unknown", None),
        ("none", "succeeded", "none", "unexpected"),
        ("possible", "denied", "unknown", "denied"),
        ("possible", "not_executed", "unknown", "cancelled"),
        ("none", "unknown", "unknown", "timeout"),
        ("possible", "unknown", "none", "timeout"),
        ("possible", "failed", "confirmed", "error"),
    ],
)
def test_tool_outcome_rejects_illegal_state_matrix(
    side_effects, state, effect, error_type,
):
    with pytest.raises(ValidationError):
        ToolOutcome(
            tool_call_id="call", tool_name="tool", side_effects=side_effects,
            state=state, effect_disposition=effect, error_type=error_type,
        )


def test_turn_result_enforces_terminal_error_and_attempt_contracts():
    base = {
        "session_id": "session", "turn_id": "turn",
        "provider_attempts": 1, "route_attempts": {"primary": 1},
    }
    assert TurnResult(status="completed", **base).error is None
    with pytest.raises(ValidationError, match="must not include an error"):
        TurnResult(status="completed", error=TurnError(code="bad", message="bad"), **base)
    with pytest.raises(ValidationError, match="requires an error"):
        TurnResult(status="failed", **base)
    with pytest.raises(ValidationError, match="must equal"):
        TurnResult(
            status="failed", error=TurnError(code="bad", message="bad"),
            **{**base, "provider_attempts": 2},
        )
    assert "new_messages" not in TurnResult.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TurnResult(status="completed", new_messages=[], **base)


def test_canonical_request_fingerprint_is_stable():
    message = Message.user("hello")
    first = CanonicalModelRequest.build(
        system_prompt="system", messages=(message,), tool_schema_bytes=b"[]",
        parameters={"temperature": 0},
    )
    second = CanonicalModelRequest.build(
        system_prompt="system", messages=(message,), tool_schema_bytes=b"[]",
        parameters={"temperature": 0},
    )
    assert first.fingerprint == second.fingerprint


def test_tool_registry_freezes_and_uses_versioned_identity():
    registry = ToolRegistry()
    registry.register(ToolEntry(
        tool_id="echo", implementation_version="1", name="echo",
        description="Echo text.", args_model=EchoArgs,
        handler=lambda args, context: args.text,
    ))
    registry.freeze()
    entry = registry.get_entry("echo")
    assert (entry.tool_id, entry.implementation_version) == ("echo", "1")
    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(ToolEntry(
            tool_id="late", implementation_version="1", name="late",
            description="Too late.", args_model=EchoArgs,
            handler=lambda args, context: args.text,
        ))


@pytest.mark.asyncio
async def test_observer_failure_is_isolated_and_warned(
    config_factory, agent_factory, final_response,
):
    class BrokenObserver:
        def observe(self, event):
            raise RuntimeError("observer unavailable")

    async with agent_factory(
        config_factory(), transports={"primary": ScriptedTransport(final_response)},
        observers=[BrokenObserver()],
    ) as agent:
        result = await agent.run_turn("hello", session_id="observer-failure")

    assert result.status == "completed"
    assert any("observer 0 failed" in warning for warning in result.warnings)


def test_legacy_dynamic_lifecycle_hooks_are_rejected(
    config_factory, agent_factory, final_response,
):
    class LegacyHooks:
        def before_model(self, event):
            return None

    with pytest.raises(TypeError, match="observe"):
        agent_factory(
            config_factory(), transports={"primary": ScriptedTransport(final_response)},
            observers=[LegacyHooks()],
        )
