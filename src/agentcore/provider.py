from __future__ import annotations

import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from openai import AsyncOpenAI

from .models import ModelRequest, ModelResponse, ModelRoute, ToolCall


class ProviderConfigurationError(ValueError):
    pass


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
        allow_fallback: bool = False,
        error_type: str = "provider_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable
        self.allow_fallback = allow_fallback
        self.error_type = error_type


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
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def normalize_provider_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = _retry_after_seconds(headers)
    name = type(exc).__name__.lower()
    network_error = status is None and (
        isinstance(exc, (OSError, TimeoutError)) or "timeout" in name or "connection" in name
    )
    retryable = network_error or status in {408, 409, 429} or bool(status and status >= 500)
    allow_fallback = retryable or status in {401, 403, 404}
    return ProviderError(
        f"{type(exc).__name__}: {exc}",
        status_code=status,
        retry_after=retry_after,
        retryable=retryable,
        allow_fallback=allow_fallback,
        error_type="network_error" if network_error else "provider_error",
    )


class OpenAIChatTransport:
    def __init__(self, route: ModelRoute, *, client: Any | None = None) -> None:
        self.route = route
        if client is not None:
            self._client = client
            return
        api_key = os.getenv(route.api_key_env)
        if not api_key:
            raise ProviderConfigurationError(
                f"route {route.name!r} requires environment variable {route.api_key_env!r}"
            )
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": route.timeout_seconds,
        }
        if route.base_url:
            kwargs["base_url"] = route.base_url
        self._client = AsyncOpenAI(**kwargs)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.route.model,
            "messages": request.messages,
        }
        if request.tools:
            kwargs["tools"] = request.tools
        try:
            response = await self._client.chat.completions.create(**kwargs)
            if not response.choices:
                raise ProviderError(
                    "provider returned no choices",
                    retryable=True,
                    allow_fallback=True,
                    error_type="empty_response",
                )
            choice = response.choices[0]
            message = choice.message
            tool_calls: list[ToolCall] = []
            for raw_call in message.tool_calls or []:
                tool_calls.append(
                    ToolCall(
                        id=raw_call.id,
                        name=raw_call.function.name,
                        arguments=raw_call.function.arguments or "{}",
                    )
                )
            usage: dict[str, int] = {}
            if response.usage is not None:
                dumped = response.usage.model_dump(exclude_none=True)
                usage = {key: value for key, value in dumped.items() if isinstance(value, int)}
            return ModelResponse(
                content=message.content,
                tool_calls=tool_calls,
                finish_reason=choice.finish_reason,
                usage=usage,
            )
        except Exception as exc:
            raise normalize_provider_exception(exc) from exc

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
