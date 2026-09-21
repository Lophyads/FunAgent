from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = Field(default=0.5, ge=0, le=60)
    max_delay_seconds: float = Field(default=10.0, ge=0, le=300)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    max_retry_after_seconds: float = Field(default=10.0, ge=0, le=300)


class WorkerPolicy(StrictModel):
    turn_workers: int = Field(default=4, ge=1, le=64)
    operation_workers: int = Field(default=8, ge=1, le=128)
    queue_capacity: int = Field(default=128, ge=1, le=10_000)


class TimeoutPolicy(StrictModel):
    run_budget_seconds: float | None = Field(default=300.0, gt=0, le=86_400)
    tool_seconds: float = Field(default=60.0, gt=0, le=3_600)
    memory_seconds: float = Field(default=5.0, gt=0, le=300)
    middleware_seconds: float = Field(default=2.0, gt=0, le=300)
    policy_seconds: float = Field(default=30.0, gt=0, le=600)
    review_schedule_seconds: float = Field(default=1.0, gt=0, le=300)
    finalize_seconds: float = Field(default=5.0, gt=0, le=300)


class ModelRoute(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    api_mode: Literal["chat_completions"] = "chat_completions"
    base_url: str | None = None
    api_key_env: str = Field(min_length=1, max_length=200)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_attempts: int | None = Field(default=None, ge=1, le=10)

    @field_validator("provider", "model", "api_key_env")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class AgentConfig(StrictModel):
    system_prompt: str = "You are a helpful AI assistant. Use tools when they are useful."
    database_path: Path = Path("agentcore.db")
    max_iterations: int = Field(default=8, ge=1, le=100)
    tool_result_max_chars: int = Field(default=32_768, ge=256, le=1_000_000)
    memory_max_chars: int = Field(default=4_000, ge=0, le=100_000)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    workers: WorkerPolicy = Field(default_factory=WorkerPolicy)
    timeouts: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    routes: list[ModelRoute] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_route_names(self) -> "AgentConfig":
        names = [route.name for route in self.routes]
        if len(names) != len(set(names)):
            raise ValueError("route names must be unique")
        return self


class UserInput(StrictModel):
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("user input must not be empty")
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def normalize(cls, value: "UserInput | str") -> "UserInput":
        return value if isinstance(value, cls) else cls(text=value)


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str = "{}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


class Message(StrictModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    api_content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    finish_reason: str | None = None
    effect_disposition: Literal["none", "not_applied", "confirmed", "unknown"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        content = self.api_content if self.role == "user" and self.api_content is not None else self.content
        wire: dict[str, Any] = {"role": self.role, "content": content}
        if self.role == "assistant" and self.tool_calls:
            wire["tool_calls"] = [call.to_wire() for call in self.tool_calls]
        if self.role == "tool":
            wire["tool_call_id"] = self.tool_call_id
            if self.tool_name:
                wire["name"] = self.tool_name
        return wire


class ModelRequest(StrictModel):
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ModelResponse(StrictModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class MemoryItem(StrictModel):
    id: str = ""
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnError(StrictModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolOutcome(StrictModel):
    tool_call_id: str
    tool_name: str
    state: Literal["succeeded", "failed", "denied", "not_executed", "unknown"]
    effect_disposition: Literal["none", "not_applied", "confirmed", "unknown"]
    error_type: str | None = None
    may_still_be_running: bool = False


class TurnResult(StrictModel):
    status: Literal["completed", "failed", "interrupted"]
    final_text: str | None = None
    session_id: str
    turn_id: str
    new_messages: list[Message] = Field(default_factory=list)
    iterations: int = 0
    route_used: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: TurnError | None = None
    context_version: int
    completion_reason: str = "unknown"
    tool_outcomes: list[ToolOutcome] = Field(default_factory=list)
    provider_attempts: int = 0
    route_attempts: dict[str, int] = Field(default_factory=dict)

