"""Provider attempts, retry delays, cooldowns and route fallback."""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from openai import APIError

from .interfaces import CancellationToken, TurnCancelled
from .models import AgentConfig, NormalizedModelResponse
from .provider_runtime import ApiModeRegistry, AttemptContext, NormalizedProviderError, ProviderResolver
from .storage import LeaseLostError
from .turn_state import LoopState, RunBudgetExceeded, TurnRuntime

logger = logging.getLogger(__name__)


class ProviderExhausted(RuntimeError):
    def __init__(self, failures: list[dict[str, Any]]) -> None:
        super().__init__("all provider routes were exhausted")
        self.failures = failures


class ProviderRequestAborted(RuntimeError):
    def __init__(self, route_name: str, error: NormalizedProviderError) -> None:
        super().__init__(str(error))
        self.route_name = route_name
        self.error = error


@dataclass(slots=True)
class RouteState:
    cooldown_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> float:
        with self.lock:
            return self.cooldown_until - time.monotonic()

    def set_cooldown(self, seconds: float) -> None:
        with self.lock:
            self.cooldown_until = time.monotonic() + max(0.0, seconds)

    def clear(self) -> None:
        with self.lock:
            self.cooldown_until = 0.0


class ProviderRunner:
    def __init__(
        self, *, config: AgentConfig, provider_resolver: ProviderResolver,
        api_modes: ApiModeRegistry, route_states: tuple[RouteState, ...],
    ) -> None:
        self.config = config
        self.provider_resolver = provider_resolver
        self.api_modes = api_modes
        self.route_states = route_states

    def run(
        self, runtime: TurnRuntime, state: LoopState,
    ) -> tuple[NormalizedModelResponse, int]:
        request = state.require_request('provider_attempts')
        failures: list[dict[str, Any]] = []
        for route_index in range(state.active_route_index, len(self.config.routes)):
            route = self.config.routes[route_index]
            route_state = self.route_states[route_index]
            binding = self.provider_resolver.resolve(route)
            adapter = self.api_modes.get(route.api_mode)
            if route.api_mode not in binding.capabilities:
                raise ProviderRequestAborted(
                    route.name,
                    NormalizedProviderError(
                        f"provider does not support {route.api_mode}",
                        error_type="capability_mismatch",
                    ),
                )
            max_attempts = route.max_attempts or self.config.retry.max_attempts
            for attempt_number in range(1, max_attempts + 1):
                runtime.check_active()
                cooldown = route_state.remaining()
                if cooldown > 0:
                    self._wait(runtime, state, cooldown)
                attempt_id = str(uuid.uuid4())
                state.provider_attempts += 1
                state.route_attempts[route.name] = (
                    state.route_attempts.get(route.name, 0) + 1
                )
                logger.debug(
                    'provider.attempt_started phase=%s route=%s iteration=%s attempt=%s',
                    'provider_attempts',
                    route.name,
                    state.iteration,
                    attempt_number,
                )
                state.warnings.extend(runtime.observability.publish(
                    "provider.attempt_started", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    phase="provider_attempts",
                    payload={
                        "route": route.name, "iteration": state.iteration,
                        "attempt": attempt_number,
                    },
                ))
                attempt_started = time.monotonic()
                wire = adapter.encode(request, binding)
                request_messages = self._provider_request_message_details(
                    wire.body
                )
                request_payload = {
                    "route": route.name,
                    "provider": binding.provider,
                    "model": binding.model,
                    "api_mode": binding.api_mode,
                    "iteration": state.iteration,
                    "attempt": attempt_number,
                    "request_fingerprint": request.fingerprint,
                    "timeout_seconds": wire.timeout_seconds,
                    "message_count": len(request_messages),
                    "messages": [
                        {
                            key: value for key, value in item.items()
                            if key != "content_preview"
                        }
                        for item in request_messages
                    ],
                    "tool_count": len(wire.body.get("tools") or []),
                }
                logger.info(
                    'provider.request phase=%s payload=%s',
                    'provider_attempts',
                    request_payload,
                )
                logger.debug(
                    'provider.request_messages phase=%s payload=%s',
                    'provider_attempts',
                    request_messages,
                )
                try:
                    raw_response = binding.transport.send(
                        wire,
                        AttemptContext(
                            attempt_id=attempt_id, session_id=state.session_id,
                            turn_id=state.turn_id, iteration=state.iteration,
                            route_name=route.name,
                            attempt_number=attempt_number,
                            deadline=runtime.deadline,
                            cancellation=CancellationToken(runtime.cancel_event),
                        ),
                    )
                    runtime.check_active()
                    logger.debug(
                        'provider.response raw_response=%s',
                        raw_response.raw,
                    )
                    normalized = adapter.decode(raw_response, binding)
                except (TurnCancelled, RunBudgetExceeded, LeaseLostError):
                    raise
                except BaseException as exc:
                    if not isinstance(
                        exc, (NormalizedProviderError, APIError, OSError, TimeoutError)
                    ):
                        raise
                    error = adapter.normalize_error(exc)
                    failure_payload = {
                        "route": route.name, "attempt": attempt_number,
                        "error_type": error.error_type,
                        "status_code": error.status_code,
                        "duration_ms": int(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    }
                    logger.warning(
                        'provider.attempt_failed phase=%s payload=%s',
                        'provider_attempts',
                        failure_payload,
                    )
                    state.warnings.extend(runtime.observability.publish(
                        "provider.attempt_failed", trace_id=state.trace_id,
                        session_id=state.session_id, turn_id=state.turn_id,
                        phase="provider_attempts",
                        payload={
                            "route": route.name, "attempt": attempt_number,
                            "error_type": error.error_type,
                            "status_code": error.status_code,
                            "duration_ms": int(
                                (time.monotonic() - attempt_started) * 1000
                            ),
                        },
                    ))
                    failures.append({
                        "route": route.name,
                        "attempt": attempt_number,
                        "error_type": error.error_type,
                        "status_code": error.status_code,
                    })
                    if error.ambiguous:
                        logger.warning(
                            'provider.ambiguous_failure phase=%s route=%s error_type=%s',
                            'provider_attempts',
                            route.name,
                            error.error_type,
                        )
                        state.warnings.extend(runtime.observability.publish(
                            "provider.ambiguous_failure",
                            trace_id=state.trace_id,
                            session_id=state.session_id,
                            turn_id=state.turn_id,
                            phase="provider_attempts",
                            payload={
                                "route": route.name,
                                "error_type": error.error_type,
                            },
                        ))
                    if error.retry_after is not None or error.status_code == 429:
                        route_state.set_cooldown(min(
                            error.retry_after or self.config.retry.base_delay_seconds,
                            self.config.retry.max_retry_after_seconds,
                        ))
                    if error.retryable and attempt_number < max_attempts:
                        delay = self._retry_delay(error, attempt_number - 1)
                        logger.warning(
                            'provider.retry_scheduled phase=%s route=%s attempt=%s delay=%s',
                            'provider_attempts',
                            route.name,
                            attempt_number,
                            delay,
                        )
                        state.warnings.extend(runtime.observability.publish(
                            "provider.retry_scheduled", trace_id=state.trace_id,
                            session_id=state.session_id, turn_id=state.turn_id,
                            phase="provider_attempts",
                            payload={"route": route.name, "attempt": attempt_number, "delay": delay},
                        ))
                        self._wait(runtime, state, delay)
                        continue
                    if error.allow_fallback:
                        break
                    raise ProviderRequestAborted(route.name, error) from exc
                else:
                    route_state.clear()
                    response_content = normalized.content
                    response_content_preview = None
                    response_payload = {
                        "route": route.name,
                        "tool_call_count": len(normalized.tool_calls),
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments_length": len(call.arguments),
                            }
                            for call in normalized.tool_calls
                        ],
                        "content_type": type(response_content).__name__,
                        "content_length": (
                            len(response_content)
                            if response_content is not None else 0
                        ),
                        "content_truncated": bool(
                            response_content is not None
                            and len(response_content) > 1000
                        ),
                        "content_preview": response_content_preview,
                        "finish_reason": (
                            normalized.finish_reason
                            if normalized.finish_reason in {
                                "stop", "length", "tool_calls", "content_filter"
                            } else "other"
                        ),
                        "duration_ms": int(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    }
                    logger.info(
                        'provider.response phase=%s payload=%s',
                        'provider_attempts',
                        response_payload,
                    )
                    state.warnings.extend(runtime.observability.publish(
                        "provider.response", trace_id=state.trace_id,
                        session_id=state.session_id, turn_id=state.turn_id,
                        phase="provider_attempts",
                        payload={
                            "route": route.name,
                            "tool_call_count": len(normalized.tool_calls),
                            "finish_reason": (
                                normalized.finish_reason
                                if normalized.finish_reason in {
                                    "stop", "length", "tool_calls", "content_filter"
                                } else "other"
                            ),
                            "duration_ms": int(
                                (time.monotonic() - attempt_started) * 1000
                            ),
                        },
                    ))
                    return normalized, route_index
            if route_index + 1 < len(self.config.routes):
                logger.warning(
                    'provider.fallback phase=%s from_route=%s to_route=%s',
                    'provider_attempts',
                    route.name,
                    self.config.routes[route_index + 1].name,
                )
                state.warnings.extend(runtime.observability.publish(
                    "provider.fallback", trace_id=state.trace_id,
                    session_id=state.session_id, turn_id=state.turn_id,
                    phase="provider_attempts",
                    payload={
                        "from_route": route.name,
                        "to_route": self.config.routes[route_index + 1].name,
                    },
                ))
        raise ProviderExhausted(failures)

    def _wait(
        self, runtime: TurnRuntime, state: LoopState, delay: float
    ) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            runtime.check_active()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            runtime.cancel_event.wait(min(0.05, remaining))

    def _retry_delay(
        self, error: NormalizedProviderError, attempt_index: int
    ) -> float:
        policy = self.config.retry
        backoff = min(
            policy.max_delay_seconds,
            policy.base_delay_seconds * (2**attempt_index),
        )
        if backoff and policy.jitter_ratio:
            spread = backoff * policy.jitter_ratio
            backoff = max(0.0, backoff + random.uniform(-spread, spread))
        if error.retry_after is not None:
            backoff = max(
                backoff,
                min(error.retry_after, policy.max_retry_after_seconds),
            )
        return min(
            backoff,
            max(policy.max_delay_seconds, policy.max_retry_after_seconds),
        )

    @staticmethod
    def _provider_request_message_details(
        body: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return []
        details: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                details.append({
                    "index": index,
                    "role": "unknown",
                    "content_type": type(message).__name__,
                    "content_length": None,
                    "content_preview": None,
                    "tool_call_count": 0,
                    "tool_names": [],
                })
                continue
            content = message.get("content")
            if isinstance(content, str):
                content_length: int | None = len(content)
                content_preview: str | None = None
            elif content is None:
                content_length = 0
                content_preview = None
            else:
                rendered = repr(content)
                content_length = len(rendered)
                content_preview = None
            raw_tool_calls = message.get("tool_calls")
            tool_calls = (
                raw_tool_calls if isinstance(raw_tool_calls, list) else []
            )
            tool_names = []
            for call in tool_calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                if isinstance(function, Mapping):
                    tool_names.append(function.get("name"))
            details.append({
                "index": index,
                "role": message.get("role"),
                "content_type": type(content).__name__,
                "content_length": content_length,
                "content_preview": content_preview,
                "tool_call_count": len(tool_calls),
                "tool_names": tool_names,
                "tool_call_id": message.get("tool_call_id"),
            })
        return details

