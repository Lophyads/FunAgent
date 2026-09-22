from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agentcore import (
    ChatCompletionsAdapter, ModelRoute, NormalizedModelResponse,
    NormalizedProviderError, OpenAIHttpTransport, ProviderRuntime, RetryPolicy,
    ToolCall, WireResponse,
)
from conftest import ScriptedTransport


def _routes(primary_attempts: int = 1) -> list[ModelRoute]:
    return [
        ModelRoute(name="primary", provider="test", model="primary-model", api_mode="scripted", credential_ref="env:PRIMARY_KEY", max_attempts=primary_attempts),
        ModelRoute(name="fallback", provider="test", model="fallback-model", api_mode="scripted", credential_ref="env:FALLBACK_KEY", max_attempts=1),
    ]


@pytest.mark.asyncio
async def test_retry_then_fallback_uses_one_ledger_entry_per_send(config_factory, agent_factory):
    error = NormalizedProviderError("temporary", status_code=503, retryable=True, allow_fallback=True)
    primary = ScriptedTransport(error, error)
    fallback = ScriptedTransport(NormalizedModelResponse(content="fallback answer", finish_reason="stop"))
    config = config_factory(
        routes=_routes(2),
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0, jitter_ratio=0, max_retry_after_seconds=0),
    )
    async with agent_factory(config, transports={"primary": primary, "fallback": fallback}) as agent:
        result = await agent.run_turn("hello", session_id="fallback")

    assert result.status == "completed"
    assert result.route_used == "fallback"
    assert result.provider_attempts == 3
    assert result.route_attempts == {"primary": 2, "fallback": 1}
    assert len(primary.requests) == 2 and len(fallback.requests) == 1
    with sqlite3.connect(config.database_path) as connection:
        states = [row[0] for row in connection.execute("SELECT state FROM model_attempts ORDER BY started_at")]
    assert states == ["failed", "failed", "succeeded"]


@pytest.mark.asyncio
async def test_retry_reuses_request_and_fallback_resets_next_turn(config_factory, agent_factory):
    primary = ScriptedTransport(
        NormalizedProviderError("down", status_code=503, retryable=True, allow_fallback=True),
        NormalizedModelResponse(content="primary recovered", finish_reason="stop"),
    )
    fallback = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(id="clock", name="current_time", arguments='{"timezone":"UTC"}')], finish_reason="tool_calls"),
        NormalizedModelResponse(content="fallback finished", finish_reason="stop"),
    )
    config = config_factory(routes=_routes())
    async with agent_factory(config, transports={"primary": primary, "fallback": fallback}) as agent:
        first = await agent.run_turn("first", session_id="sticky")
        second = await agent.run_turn("second", session_id="sticky")

    assert first.route_used == "fallback" and second.route_used == "primary"
    assert len(primary.requests) == 2 and len(fallback.requests) == 2


@pytest.mark.asyncio
async def test_nonretryable_bad_request_aborts_without_fallback(config_factory, agent_factory):
    primary = ScriptedTransport(NormalizedProviderError("bad request", status_code=400))
    fallback = ScriptedTransport(NormalizedModelResponse(content="must not run"))
    async with agent_factory(config_factory(routes=_routes()), transports={"primary": primary, "fallback": fallback}) as agent:
        result = await agent.run_turn("hello", session_id="bad-request")

    assert result.status == "failed"
    assert result.error.code == "provider_error"
    assert len(primary.requests) == 1 and not fallback.requests


def test_openai_transport_disables_sdk_retries(monkeypatch):
    import agentcore.provider_runtime as provider_module
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def close(self):
            return None

    monkeypatch.setattr(provider_module, "OpenAI", FakeClient)
    OpenAIHttpTransport(api_key="secret", base_url=None, timeout_seconds=12).close()
    assert captured["max_retries"] == 0


def test_chat_completions_adapter_classifies_empty_message_as_retryable():
    raw = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=None), finish_reason="stop")], usage=None)
    runtime = ProviderRuntime(provider="test", model="model", endpoint="offline", api_mode="chat_completions", capabilities=frozenset({"chat_completions"}), transport=ScriptedTransport())
    with pytest.raises(NormalizedProviderError) as captured:
        ChatCompletionsAdapter().decode(WireResponse(raw=raw), runtime)
    assert captured.value.error_type == "empty_response"
    assert captured.value.retryable and captured.value.allow_fallback