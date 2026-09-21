from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from agentcore import AgentConfig, ModelResponse, ModelRoute, RetryPolicy


class ScriptedTransport:
    def __init__(self, *items: Any) -> None:
        self.items = deque(items)
        self.requests = []
        self.closed = False

    async def complete(self, request):
        self.requests.append(request)
        if not self.items:
            raise AssertionError("scripted transport has no response left")
        item = self.items.popleft()
        if callable(item):
            item = item(request)
            if hasattr(item, "__await__"):
                item = await item
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def config_factory(tmp_path: Path):
    def factory(
        *,
        routes: list[ModelRoute] | None = None,
        system_prompt: str = "test system",
        max_iterations: int = 8,
        tool_result_max_chars: int = 32_768,
        retry: RetryPolicy | None = None,
    ) -> AgentConfig:
        return AgentConfig(
            system_prompt=system_prompt,
            database_path=tmp_path / "agentcore.db",
            max_iterations=max_iterations,
            tool_result_max_chars=tool_result_max_chars,
            retry=retry or RetryPolicy(
                max_attempts=1,
                base_delay_seconds=0,
                max_delay_seconds=0,
                jitter_ratio=0,
                max_retry_after_seconds=0,
            ),
            routes=routes
            or [
                ModelRoute(
                    name="primary",
                    provider="test",
                    model="test-model",
                    api_key_env="TEST_API_KEY",
                )
            ],
        )

    return factory


@pytest.fixture
def final_response() -> ModelResponse:
    return ModelResponse(
        content="done",
        finish_reason="stop",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )

