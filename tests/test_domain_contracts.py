from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from agentcore import (
    CanonicalModelRequest,
    Message,
    NormalizedModelResponse,
    ToolCall,
    ToolCatalog,
    ToolOutcome,
    ToolSpec,
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
        Message.assistant_text("answer", finish_reason="stop"),
        Message.assistant_tools([call], finish_reason="tool_calls"),
        Message.tool_result(
            '{"ok":true}', tool_call_id="call-1", tool_name="calculator",
            effect_disposition="none", metadata={"execution_state": "succeeded"},
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
        {
            "role": "assistant", "content": "x",
            "tool_calls": [{"id": "c", "name": "calculator"}],
        },
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
    ("side_effects", "state", "effect", "error_type", "may_run"),
    [
        ("none", "succeeded", "none", None, False),
        ("possible", "succeeded", "confirmed", None, False),
        ("none", "failed", "not_applied", "invalid_arguments", False),
        ("possible", "denied", "not_applied", "policy_denied", False),
        ("possible", "not_executed", "not_applied", "cancelled", False),
        ("none", "unknown", "none", "timeout", True),
        ("possible", "unknown", "unknown", "timeout", True),
    ],
)
def test_tool_outcome_accepts_legal_state_matrix(
    side_effects, state, effect, error_type, may_run,
):
    outcome = ToolOutcome(
        tool_call_id="call", tool_name="tool", side_effects=side_effects,
        state=state, effect_disposition=effect, error_type=error_type,
        may_still_be_running=may_run,
    )
    assert outcome.effect_disposition == effect


@pytest.mark.parametrize(
    ("side_effects", "state", "effect", "error_type", "may_run"),
    [
        ("possible", "succeeded", "unknown", None, False),
        ("none", "succeeded", "none", "unexpected", False),
        ("possible", "denied", "unknown", "denied", False),
        ("possible", "not_executed", "unknown", "cancelled", False),
        ("none", "unknown", "unknown", "timeout", True),
        ("possible", "unknown", "none", "timeout", True),
        ("possible", "failed", "confirmed", "error", False),
        ("possible", "failed", "unknown", "error", True),
    ],
)
def test_tool_outcome_rejects_illegal_state_matrix(
    side_effects, state, effect, error_type, may_run,
):
    with pytest.raises(ValidationError):
        ToolOutcome(
            tool_call_id="call", tool_name="tool", side_effects=side_effects,
            state=state, effect_disposition=effect, error_type=error_type,
            may_still_be_running=may_run,
        )


def test_turn_result_enforces_terminal_error_and_attempt_contracts():
    base = {
        "session_id": "session", "turn_id": "turn", "context_version": 1,
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


def test_tool_catalog_freezes_and_uses_versioned_identity():
    catalog = ToolCatalog()
    catalog.register(ToolSpec(
        tool_id="echo", implementation_version="1", name="echo",
        description="Echo text.", args_model=EchoArgs,
        handler=lambda args, context: args.text,
    ))
    catalog.freeze()
    assert catalog.get_versioned("echo", "1") is catalog.get("echo")
    with pytest.raises(RuntimeError, match="frozen"):
        catalog.register(ToolSpec(
            tool_id="late", implementation_version="1", name="late",
            description="Too late.", args_model=EchoArgs,
            handler=lambda args, context: args.text,
        ))


@pytest.mark.asyncio
async def test_observer_failure_is_isolated_and_warned(
    config_factory, agent_factory, final_response,
):
    class BrokenObserver:
        def on_event(self, event):
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

    with pytest.raises(TypeError, match="on_event"):
        agent_factory(
            config_factory(), transports={"primary": ScriptedTransport(final_response)},
            observers=[LegacyHooks()],
        )