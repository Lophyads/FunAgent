from __future__ import annotations

from collections import deque
import json
from types import MappingProxyType
from typing import Any, Mapping

import pytest

from agentcore import (
    AgentConfig,
    AgentCore,
    ApiModeRegistry,
    CanonicalModelRequest,
    ModelRoute,
    NormalizedModelResponse,
    ProviderRuntime,
    RetryPolicy,
    StaticProviderResolver,
    TimeoutPolicy,
    ToolRegistry,
    WireRequest,
    WireResponse,
    WorkerPolicy,
    default_tool_registry,
)
from agentcore.provider_runtime import normalize_provider_exception


class ScriptedTransport:
    """Synchronous, offline Transport replacement for provider tests."""

    def __init__(self, *items: Any) -> None:
        self.items = deque(items)
        self.requests: list[WireRequest] = []
        self.contexts: list[Any] = []
        self.closed = False

    def send(self, request: WireRequest, context: Any) -> WireResponse:
        self.requests.append(request)
        self.contexts.append(context)
        if not self.items:
            raise AssertionError("scripted transport has no response left")
        item = self.items.popleft()
        if callable(item):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, WireResponse) else WireResponse(raw=item)

    def close(self) -> None:
        self.closed = True


class ScriptedApiModeAdapter:
    """Adapter used only in tests; it keeps model values fully offline."""

    name = "scripted"

    def __init__(self) -> None:
        self.canonical_requests: list[CanonicalModelRequest] = []

    def encode(
        self, request: CanonicalModelRequest, runtime: ProviderRuntime,
    ) -> WireRequest:
        self.canonical_requests.append(request)
        body = {
            "model": runtime.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                *(message.to_wire() for message in request.messages),
            ],
            "tools": json.loads(request.tool_schema_bytes.decode("utf-8")),
        }
        return WireRequest(
            body=MappingProxyType(body), timeout_seconds=runtime.timeout_seconds
        )

    def decode(
        self, response: WireResponse, runtime: ProviderRuntime,
    ) -> NormalizedModelResponse:
        if not isinstance(response.raw, NormalizedModelResponse):
            raise TypeError("scripted transport must return NormalizedModelResponse")
        return response.raw

    def normalize_error(self, exc: BaseException):
        return normalize_provider_exception(exc)


def scripted_provider_stack(
    config: AgentConfig, transports: Mapping[str, ScriptedTransport],
) -> tuple[StaticProviderResolver, ApiModeRegistry, ScriptedApiModeAdapter]:
    adapter = ScriptedApiModeAdapter()
    api_modes = ApiModeRegistry()
    api_modes.register(adapter)
    api_modes.freeze()
    runtimes = {
        route.name: ProviderRuntime(
            provider=route.provider,
            model=route.model,
            endpoint="https://offline.invalid/v1",
            api_mode="scripted",
            capabilities=frozenset({"scripted", "tools"}),
            transport=transports[route.name],
            timeout_seconds=route.timeout_seconds,
        )
        for route in config.routes
    }
    return StaticProviderResolver(runtimes), api_modes, adapter


@pytest.fixture
def agent_factory():
    def factory(
        config: AgentConfig, *, transports: Mapping[str, ScriptedTransport],
        tool_registry: ToolRegistry | None = None, **kwargs: Any,
    ) -> AgentCore:
        resolver, api_modes, _ = scripted_provider_stack(config, transports)
        return AgentCore(
            config,
            tool_registry=tool_registry or default_tool_registry(),
            provider_resolver=resolver,
            api_modes=api_modes,
            **kwargs,
        )
    return factory


@pytest.fixture
def config_factory(tmp_path):
    def factory(
        *, routes: list[ModelRoute] | None = None,
        system_prompt: str = "test system", max_iterations: int = 8,
        tool_result_max_chars: int = 32_768,
        retry: RetryPolicy | None = None,
        workers: WorkerPolicy | None = None,
        timeouts: TimeoutPolicy | None = None,
    ) -> AgentConfig:
        return AgentConfig(
            system_prompt=system_prompt, database_path=tmp_path / "agentcore.db",
            max_iterations=max_iterations, tool_result_max_chars=tool_result_max_chars,
            retry=retry or RetryPolicy(
                max_attempts=1, base_delay_seconds=0, max_delay_seconds=0,
                jitter_ratio=0, max_retry_after_seconds=0,
            ),
            workers=workers or WorkerPolicy(), timeouts=timeouts or TimeoutPolicy(),
            routes=routes or [
                ModelRoute(
                    name="primary", provider="test", model="test-model",
                    api_mode="scripted", credential_ref="env:TEST_API_KEY",
                )
            ],
        )
    return factory


@pytest.fixture
def final_response() -> NormalizedModelResponse:
    return NormalizedModelResponse(
        content="done", finish_reason="stop",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )
