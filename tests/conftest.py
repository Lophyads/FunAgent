from __future__ import annotations

from collections import deque
import json
import threading
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
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
)
from agentcore.provider_runtime import normalize_provider_exception

from agentcore.interfaces import CancellationToken, ToolContext, TurnHostContext, HostCapabilities
from tools.config import ToolSystemConfig, WorkspaceConfig
from tools.context import ToolAvailabilityContext, BackendProbes, ToolUse, ToolInteractions, ToolResources
from tools.model_tools import discover_builtin_tools
from tools import runtime as tool_runtime
from tools.runtime import initialize_tool_system, get_tool_registry, get_tool_services, shutdown_tool_system


def builtin_registry():
    registry = ToolRegistry()
    discover_builtin_tools(registry)
    return registry


def configure_tools(config, *, entries=None, approver=None):
    system_config = replace(ToolSystemConfig.from_agent_config(config), approver=approver)
    if tool_runtime._state is tool_runtime.ToolSystemState.UNINITIALIZED:
        initialize_tool_system(system_config)
    else:
        tool_runtime.validate_tool_config(system_config)
    if entries is not None:
        entries.freeze()
        get_tool_registry().replace_all(entries.entries())


@pytest.fixture(autouse=True)
def isolated_tool_runtime(monkeypatch):
    from tools.browser_backend import AgentBrowserBackend
    from tools.computer_backend import CuaCliBackend
    # Tests never probe optional binaries on the developer's machine.
    monkeypatch.setattr(AgentBrowserBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(CuaCliBackend, "_probe_available", classmethod(lambda cls: False))
    monkeypatch.setattr(CuaCliBackend, "_availability_cache", None)
    yield
    report = shutdown_tool_system(timeout=3)
    assert report.completed, report
    with tool_runtime._lock:
        tool_runtime._state = tool_runtime.ToolSystemState.UNINITIALIZED
        tool_runtime._startup_cwd = None
        tool_runtime._config = tool_runtime._registry = tool_runtime._services = None


@pytest.fixture
def availability(tmp_path):
    from agentcore.runtime_cwd import ResolvedWorkspace
    return ToolAvailabilityContext(
        ToolSystemConfig().tools, ResolvedWorkspace(tmp_path, tmp_path),
        HostCapabilities(), BackendProbes(lambda name: name in {"search", "web_extract"}),
    )


@pytest.fixture
def context_factory(tmp_path):
    @contextmanager
    def factory(session_id="s1", turn_id="t1", call_id="c1", *, host=None, check_active=lambda: None):
        if tool_runtime._state is tool_runtime.ToolSystemState.UNINITIALIZED:
            initialize_tool_system(ToolSystemConfig(workspace=WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path)))
        services = get_tool_services()
        workspace = services.begin_execution(
            namespace="test", profile="test", task_id=session_id,
            owner_id="test-owner", turn_id=turn_id,
        )
        use = services.create_use("test-owner", turn_id, call_id, check_active)
        try:
            with services.tool_use(use):
                yield ToolContext(
                    session_id, turn_id, call_id, workspace,
                    ToolInteractions(use, host or TurnHostContext(), lambda: None, lambda: None),
                    ToolResources(use), CancellationToken(threading.Event()), use.check,
                )
        finally:
            services.end_execution("test-owner", turn_id)
    return factory


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
    configure_tools(config)
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
        configure_tools(config, entries=tool_registry, approver=kwargs.pop("approver", None))
        resolver, api_modes, _ = scripted_provider_stack(config, transports)
        return AgentCore(
            config,
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
