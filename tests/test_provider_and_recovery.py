from __future__ import annotations

import json

import pytest

from agentcore import (
    AgentCore,
    Message,
    ModelResponse,
    ModelRoute,
    ProviderError,
    RetryPolicy,
    SQLiteStore,
    ToolCall,
    default_tool_registry,
)

from conftest import ScriptedTransport


def _routes(*, primary_attempts: int = 1) -> list[ModelRoute]:
    return [
        ModelRoute(
            name="primary",
            provider="test",
            model="primary-model",
            api_key_env="PRIMARY_KEY",
            max_attempts=primary_attempts,
        ),
        ModelRoute(
            name="fallback",
            provider="test",
            model="fallback-model",
            api_key_env="FALLBACK_KEY",
            max_attempts=1,
        ),
    ]


@pytest.mark.asyncio
async def test_retry_then_fallback(config_factory):
    retryable = ProviderError(
        "temporary", status_code=503, retryable=True, allow_fallback=True
    )
    primary = ScriptedTransport(retryable, retryable)
    fallback = ScriptedTransport(ModelResponse(content="fallback answer", finish_reason="stop"))
    config = config_factory(
        routes=_routes(primary_attempts=2),
        retry=RetryPolicy(
            max_attempts=2,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
            max_retry_after_seconds=0,
        ),
    )
    async with AgentCore(
        config,
        default_tool_registry(),
        transports={"primary": primary, "fallback": fallback},
    ) as agent:
        result = await agent.run_turn("hello", session_id="fallback")

    assert result.status == "completed"
    assert result.route_used == "fallback"
    assert len(primary.requests) == 2
    assert len(fallback.requests) == 1
    assert result.provider_attempts == 3
    assert result.route_attempts == {"primary": 2, "fallback": 1}
    assert any("fallback activated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_fallback_is_sticky_within_turn_but_resets_next_turn(config_factory):
    primary = ScriptedTransport(
        ProviderError("down", status_code=503, retryable=True, allow_fallback=True),
        ModelResponse(content="primary recovered", finish_reason="stop"),
    )
    fallback = ScriptedTransport(
        ModelResponse(
            tool_calls=[
                ToolCall(id="clock", name="current_time", arguments='{"timezone":"UTC"}')
            ],
            finish_reason="tool_calls",
        ),
        ModelResponse(content="fallback finished", finish_reason="stop"),
    )
    config = config_factory(routes=_routes())
    async with AgentCore(
        config,
        default_tool_registry(),
        transports={"primary": primary, "fallback": fallback},
    ) as agent:
        first = await agent.run_turn("first", session_id="sticky")
        second = await agent.run_turn("second", session_id="sticky")

    assert first.route_used == "fallback"
    assert second.route_used == "primary"
    assert len(primary.requests) == 2
    assert len(fallback.requests) == 2


@pytest.mark.asyncio
async def test_rate_limit_cooldown_skips_primary_on_next_turn(config_factory):
    primary = ScriptedTransport(
        ProviderError(
            "rate limited",
            status_code=429,
            retry_after=1.0,
            retryable=True,
            allow_fallback=True,
        )
    )
    fallback = ScriptedTransport(
        ModelResponse(content="one", finish_reason="stop"),
        ModelResponse(content="two", finish_reason="stop"),
    )
    config = config_factory(
        routes=_routes(),
        retry=RetryPolicy(
            max_attempts=1,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
            max_retry_after_seconds=1,
        ),
    )
    async with AgentCore(
        config,
        default_tool_registry(),
        transports={"primary": primary, "fallback": fallback},
    ) as agent:
        first = await agent.run_turn("first", session_id="cooldown")
        second = await agent.run_turn("second", session_id="cooldown")

    assert first.status == second.status == "completed"
    assert len(primary.requests) == 1
    assert len(fallback.requests) == 2
    assert any("cooling down" in warning for warning in second.warnings)


@pytest.mark.asyncio
async def test_nonretryable_bad_request_aborts_without_fallback(config_factory):
    primary = ScriptedTransport(
        ProviderError(
            "bad request",
            status_code=400,
            retryable=False,
            allow_fallback=False,
        )
    )
    fallback = ScriptedTransport(ModelResponse(content="must not run", finish_reason="stop"))
    async with AgentCore(
        config_factory(routes=_routes()),
        default_tool_registry(),
        transports={"primary": primary, "fallback": fallback},
    ) as agent:
        result = await agent.run_turn("bad", session_id="bad-request")

    assert result.status == "failed"
    assert result.error.details["status_code"] == 400
    assert len(fallback.requests) == 0


@pytest.mark.asyncio
async def test_context_change_is_versioned_without_rewriting_history(config_factory):
    session_id = "context-version"
    config_a = config_factory(system_prompt="prompt A")
    transport_a = ScriptedTransport(ModelResponse(content="answer A", finish_reason="stop"))
    async with AgentCore(
        config_a, default_tool_registry(), transports={"primary": transport_a}
    ) as agent:
        first = await agent.run_turn("one", session_id=session_id)

    config_b = config_factory(system_prompt="prompt B")
    transport_b = ScriptedTransport(ModelResponse(content="answer B", finish_reason="stop"))
    async with AgentCore(
        config_b, default_tool_registry(), transports={"primary": transport_b}
    ) as agent:
        second = await agent.run_turn("two", session_id=session_id)
        contexts = agent.store.get_contexts(session_id)
        messages = agent.store.load_messages(session_id)

    assert first.context_version == 1
    assert second.context_version == 2
    assert [context["system_prompt"] for context in contexts] == ["prompt A", "prompt B"]
    assert [message.content for message in messages if message.role == "user"] == ["one", "two"]
    assert any("context changed" in warning for warning in second.warnings)


@pytest.mark.asyncio
async def test_dangling_tool_call_is_repaired_before_resume(config_factory):
    config = config_factory()
    registry = default_tool_registry()
    registry.freeze()
    store = SQLiteStore(config.database_path)
    store.open()
    state = store.ensure_session_context(
        "recover", config.system_prompt, registry.definitions()
    )
    store.begin_turn(
        turn_id="old-turn",
        session_id="recover",
        context_version=state.version,
        user_message=Message(role="user", content="old question"),
    )
    store.append_message(
        "recover",
        "old-turn",
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="lost", name="current_time", arguments="{}")],
            finish_reason="tool_calls",
        ),
    )
    store.close()

    def inspect_repaired_history(request):
        roles = [message["role"] for message in request.messages]
        assert roles == ["system", "user", "assistant", "tool", "user"]
        repaired = json.loads(request.messages[3]["content"])
        assert repaired["error"]["type"] == "outcome_unknown"
        return ModelResponse(content="resumed", finish_reason="stop")

    transport = ScriptedTransport(inspect_repaired_history)
    async with AgentCore(
        config, registry, transports={"primary": transport}
    ) as agent:
        result = await agent.run_turn("new question", session_id="recover")
        old_turn = agent.store.get_turn("old-turn")

    assert result.status == "completed"
    assert old_turn["status"] == "failed"
    assert any("recovered unfinished turn" in warning for warning in result.warnings)


def test_openai_transport_disables_sdk_retries(monkeypatch):
    import agentcore.provider as provider_module
    from agentcore import OpenAIChatTransport

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            return None

    monkeypatch.setenv("NO_RETRY_KEY", "secret")
    monkeypatch.setattr(provider_module, "OpenAI", FakeClient)
    transport = OpenAIChatTransport(ModelRoute(
        name="no-sdk-retry", provider="openai", model="model",
        api_key_env="NO_RETRY_KEY",
    ))
    transport.close()

    assert captured["max_retries"] == 0