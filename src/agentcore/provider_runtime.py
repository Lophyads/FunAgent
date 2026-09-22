from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from openai import OpenAI

from .interfaces import CancellationToken
from .models import (
    CanonicalModelRequest,
    ModelRoute,
    NormalizedModelResponse,
    ToolCall,
)


class ProviderConfigurationError(ValueError):
    pass


class NormalizedProviderError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None,
        retry_after: float | None = None, retryable: bool = False,
        allow_fallback: bool = False, error_type: str = "provider_error",
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable
        self.allow_fallback = allow_fallback
        self.error_type = error_type
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class AttemptContext:
    attempt_id: str
    session_id: str
    turn_id: str
    iteration: int
    route_name: str
    attempt_number: int
    deadline: float | None
    cancellation: CancellationToken


@dataclass(frozen=True, slots=True)
class WireRequest:
    body: Mapping[str, Any]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class WireResponse:
    raw: Any


class Transport(Protocol):
    def send(self, request: WireRequest, context: AttemptContext) -> WireResponse: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    provider: str
    model: str
    endpoint: str
    api_mode: str
    capabilities: frozenset[str]
    transport: Transport
    timeout_seconds: float = 60.0


class ProviderResolver(Protocol):
    def resolve(self, route: ModelRoute) -> ProviderRuntime: ...

    def close(self) -> None: ...


class ApiModeAdapter(Protocol):
    name: str

    def encode(
        self, request: CanonicalModelRequest, runtime: ProviderRuntime
    ) -> WireRequest: ...

    def decode(
        self, response: WireResponse, runtime: ProviderRuntime
    ) -> NormalizedModelResponse: ...

    def normalize_error(self, exc: BaseException) -> NormalizedProviderError: ...


class ApiModeRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ApiModeAdapter] = {}
        self._frozen = False

    def register(self, adapter: ApiModeAdapter) -> None:
        if self._frozen:
            raise RuntimeError("API mode registry is frozen")
        if adapter.name in self._adapters:
            raise ValueError(f"API mode already registered: {adapter.name}")
        for method in ("encode", "decode", "normalize_error"):
            callback = getattr(adapter, method, None)
            if callback is None or not callable(callback):
                raise TypeError(f"API mode adapter must define {method}()")
            if inspect.iscoroutinefunction(callback):
                raise TypeError(f"API mode adapter {method}() must be synchronous")
        self._adapters[adapter.name] = adapter

    def freeze(self) -> None:
        self._frozen = True

    def get(self, name: str) -> ApiModeAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise ProviderConfigurationError(
                f"API mode is not registered: {name}"
            ) from exc


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class EnvironmentCredentialResolver:
    def resolve(self, reference: str) -> str:
        prefix = "env:"
        if not reference.startswith(prefix) or len(reference) == len(prefix):
            raise ProviderConfigurationError(
                f"unsupported credential reference: {reference!r}"
            )
        variable = reference[len(prefix):]
        value = os.getenv(variable)
        if not value:
            raise ProviderConfigurationError(
                f"credential environment variable is not set: {variable}"
            )
        return value


class OpenAIHttpTransport:
    def __init__(
        self, *, api_key: str, base_url: str | None,
        timeout_seconds: float, client: Any | None = None,
    ) -> None:
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    def send(self, request: WireRequest, context: AttemptContext) -> WireResponse:
        context.cancellation.raise_if_cancelled()
        response = self._client.chat.completions.create(**dict(request.body))
        return WireResponse(raw=response)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


class OpenAICompatibleProviderResolver:
    def __init__(
        self, *, credentials: CredentialResolver | None = None,
        supported_providers: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self._credentials = credentials or EnvironmentCredentialResolver()
        self._supported = dict(supported_providers or {
            "openai": frozenset({"chat_completions", "tools"}),
            "openai_compatible": frozenset({"chat_completions", "tools"}),
        })
        self._runtimes: dict[str, ProviderRuntime] = {}

    def resolve(self, route: ModelRoute) -> ProviderRuntime:
        cached = self._runtimes.get(route.name)
        if cached is not None:
            return cached
        capabilities = self._supported.get(route.provider)
        if capabilities is None:
            raise ProviderConfigurationError(
                f"provider is not registered: {route.provider}"
            )
        if route.api_mode not in capabilities:
            raise ProviderConfigurationError(
                f"provider {route.provider!r} does not support {route.api_mode!r}"
            )
        api_key = self._credentials.resolve(route.credential_ref)
        runtime = ProviderRuntime(
            provider=route.provider,
            model=route.model,
            endpoint=route.base_url or "https://api.openai.com/v1",
            api_mode=route.api_mode,
            capabilities=capabilities,
            transport=OpenAIHttpTransport(
                api_key=api_key,
                base_url=route.base_url,
                timeout_seconds=route.timeout_seconds,
            ),
            timeout_seconds=route.timeout_seconds,
        )
        self._runtimes[route.name] = runtime
        return runtime

    def close(self) -> None:
        seen: set[int] = set()
        for runtime in self._runtimes.values():
            identity = id(runtime.transport)
            if identity in seen:
                continue
            seen.add(identity)
            runtime.transport.close()


class StaticProviderResolver:
    """Explicit runtime registry for tests and embedded deployments."""

    def __init__(self, runtimes: Mapping[str, ProviderRuntime]) -> None:
        self._runtimes = dict(runtimes)

    def resolve(self, route: ModelRoute) -> ProviderRuntime:
        try:
            runtime = self._runtimes[route.name]
        except KeyError as exc:
            raise ProviderConfigurationError(
                f"no runtime configured for route: {route.name}"
            ) from exc
        if runtime.api_mode != route.api_mode:
            raise ProviderConfigurationError(
                f"runtime API mode mismatch for route: {route.name}"
            )
        return runtime

    def close(self) -> None:
        seen: set[int] = set()
        for runtime in self._runtimes.values():
            identity = id(runtime.transport)
            if identity in seen:
                continue
            seen.add(identity)
            runtime.transport.close()


class ChatCompletionsAdapter:
    name = "chat_completions"

    def encode(
        self, request: CanonicalModelRequest, runtime: ProviderRuntime
    ) -> WireRequest:
        messages = [{"role": "system", "content": request.system_prompt}]
        messages.extend(message.to_wire() for message in request.messages)
        body: dict[str, Any] = {
            "model": runtime.model,
            "messages": messages,
            **request.parameters,
        }
        tools = json.loads(request.tool_schema_bytes.decode("utf-8"))
        if tools:
            body["tools"] = tools
        return WireRequest(
            body=MappingProxyType(body),
            timeout_seconds=runtime.timeout_seconds,
        )

    def decode(
        self, response: WireResponse, runtime: ProviderRuntime
    ) -> NormalizedModelResponse:
        raw = response.raw
        if not getattr(raw, "choices", None):
            raise NormalizedProviderError(
                "provider returned no choices", retryable=True,
                allow_fallback=True, error_type="empty_response",
            )
        choice = raw.choices[0]
        message = choice.message
        tool_calls = [
            ToolCall(
                id=item.id,
                name=item.function.name,
                arguments=item.function.arguments or "{}",
            )
            for item in (message.tool_calls or [])
        ]
        if not tool_calls and (
            message.content is None or not message.content.strip()
        ):
            raise NormalizedProviderError(
                "provider returned an empty message", retryable=True,
                allow_fallback=True, error_type="empty_response",
            )
        usage: dict[str, int] = {}
        if getattr(raw, "usage", None) is not None:
            dumped = raw.usage.model_dump(exclude_none=True)
            usage = {
                key: value for key, value in dumped.items()
                if isinstance(value, int)
            }
        return NormalizedModelResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    def normalize_error(self, exc: BaseException) -> NormalizedProviderError:
        return normalize_provider_exception(exc)


def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            return None


def normalize_provider_exception(exc: BaseException) -> NormalizedProviderError:
    if isinstance(exc, NormalizedProviderError):
        return exc
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    retry_after = _retry_after_seconds(getattr(response, "headers", None))
    name = type(exc).__name__.lower()
    network_error = status is None and (
        isinstance(exc, (OSError, TimeoutError))
        or "timeout" in name
        or "connection" in name
    )
    retryable = network_error or status in {408, 409, 429} or bool(
        status and status >= 500
    )
    return NormalizedProviderError(
        f"{type(exc).__name__}: {exc}",
        status_code=status,
        retry_after=retry_after,
        retryable=retryable,
        allow_fallback=retryable or status in {401, 403, 404},
        error_type="network_error" if network_error else "provider_error",
        ambiguous=network_error,
    )


def default_api_mode_registry() -> ApiModeRegistry:
    registry = ApiModeRegistry()
    registry.register(ChatCompletionsAdapter())
    registry.freeze()
    return registry


__all__ = [
    "ApiModeAdapter",
    "ApiModeRegistry",
    "AttemptContext",
    "ChatCompletionsAdapter",
    "EnvironmentCredentialResolver",
    "NormalizedProviderError",
    "OpenAICompatibleProviderResolver",
    "OpenAIHttpTransport",
    "ProviderConfigurationError",
    "ProviderResolver",
    "ProviderRuntime",
    "StaticProviderResolver",
    "Transport",
    "WireRequest",
    "WireResponse",
    "default_api_mode_registry",
    "normalize_provider_exception",
]
