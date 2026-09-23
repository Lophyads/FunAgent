from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetryPolicy(FrozenStrictModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = Field(default=0.5, ge=0, le=60)
    max_delay_seconds: float = Field(default=10.0, ge=0, le=300)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    max_retry_after_seconds: float = Field(default=10.0, ge=0, le=300)


class WorkerPolicy(FrozenStrictModel):
    turn_workers: int = Field(default=4, ge=1, le=64)
    operation_workers: int = Field(default=8, ge=1, le=128)
    queue_capacity: int = Field(default=128, ge=1, le=10_000)


class TimeoutPolicy(FrozenStrictModel):
    run_budget_seconds: float | None = Field(default=300.0, gt=0, le=86_400)
    tool_seconds: float = Field(default=60.0, gt=0, le=3_600)
    memory_seconds: float = Field(default=5.0, gt=0, le=300)
    observer_seconds: float = Field(default=2.0, gt=0, le=300)
    policy_seconds: float = Field(default=30.0, gt=0, le=600)
    review_schedule_seconds: float = Field(default=1.0, gt=0, le=300)
    finalize_seconds: float = Field(default=5.0, gt=0, le=300)


class LeasePolicy(FrozenStrictModel):
    ttl_seconds: float = Field(default=30.0, gt=0, le=3_600)
    renew_interval_seconds: float = Field(default=10.0, gt=0, le=1_200)
    wait_timeout_seconds: float = Field(default=300.0, ge=0, le=86_400)

    @model_validator(mode="after")
    def _renew_before_expiry(self) -> "LeasePolicy":
        if self.renew_interval_seconds >= self.ttl_seconds:
            raise ValueError("renew_interval_seconds must be less than ttl_seconds")
        return self


class LoggingConfig(FrozenStrictModel):
    enabled: bool = True
    directory: Path = Path("logs")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    queue_capacity: int = Field(default=2048, ge=16, le=100_000)
    agent_max_size_mb: int = Field(default=5, ge=1, le=1024)
    agent_backup_count: int = Field(default=3, ge=0, le=100)
    errors_max_size_mb: int = Field(default=2, ge=1, le=1024)
    errors_backup_count: int = Field(default=2, ge=0, le=100)
    gateway_max_size_mb: int = Field(default=5, ge=1, le=1024)
    gateway_backup_count: int = Field(default=3, ge=0, le=100)


class ModelRoute(FrozenStrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    api_mode: str = Field(default="chat_completions", min_length=1, max_length=80)
    base_url: str | None = None
    credential_ref: str = Field(min_length=5, max_length=240)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_attempts: int | None = Field(default=None, ge=1, le=10)

    @field_validator("provider", "model", "api_mode", "credential_ref")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SessionDefaults(FrozenStrictModel):
    profile: str = "default"
    surface: str = "python"
    platform: str = "generic"
    config_version: str = "1"
    enabled_skills: tuple[str, ...] = ()
    enabled_toolsets: tuple[str, ...] = ("default",)
    permissions: frozenset[str] = frozenset()

    @field_validator("profile", "surface", "platform", "config_version")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("session default values must not be blank")
        return value


class AgentConfig(FrozenStrictModel):
    system_prompt: str = "You are a helpful AI assistant. Use tools when they are useful."
    database_path: Path = Path("agentcore.db")
    max_iterations: int = Field(default=8, ge=1, le=100)
    tool_result_max_chars: int = Field(default=32_768, ge=256, le=1_000_000)
    memory_max_chars: int = Field(default=4_000, ge=0, le=100_000)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    workers: WorkerPolicy = Field(default_factory=WorkerPolicy)
    timeouts: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    lease: LeasePolicy = Field(default_factory=LeasePolicy)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    session_defaults: SessionDefaults = Field(default_factory=SessionDefaults)
    routes: tuple[ModelRoute, ...] = Field(min_length=1)

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


@dataclass(frozen=True, slots=True)
class TurnTrigger:
    kind: Literal["user", "internal_recovery"]
    session_id: str
    user_input: UserInput | None = None
    recovery_reason: str | None = None
    source_turn_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"user", "internal_recovery"}:
            raise ValueError("invalid turn trigger kind")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("trigger session_id must not be blank")
        if self.kind == "user":
            if not isinstance(self.user_input, UserInput):
                raise ValueError("user trigger requires a UserInput")
            if self.recovery_reason is not None or self.source_turn_id is not None:
                raise ValueError("user trigger cannot carry recovery metadata")
        elif self.user_input is not None:
            raise ValueError("internal recovery must not repeat user_input")


class LeaseFence(FrozenStrictModel):
    session_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=1)


class LeaseHandle(LeaseFence):
    turn_id: str = Field(min_length=1)
    expires_at: float

    @property
    def fence(self) -> LeaseFence:
        return LeaseFence(
            session_id=self.session_id,
            owner_id=self.owner_id,
            fencing_token=self.fencing_token,
        )


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str = "{}"

    @field_validator("id")
    @classmethod
    def _nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool call id must not be blank")
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _TOOL_NAME_RE.fullmatch(value):
            raise ValueError(f"invalid tool name: {value!r}")
        return value

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

    @model_validator(mode="after")
    def _valid_role_shape(self) -> "Message":
        if self.role == "user":
            if self.content is None and self.api_content is None:
                raise ValueError("user message requires content or api_content")
            if any((
                self.tool_calls is not None,
                self.tool_call_id is not None,
                self.tool_name is not None,
                self.finish_reason is not None,
                self.effect_disposition is not None,
            )):
                raise ValueError("user message contains fields reserved for assistant or tool")
            return self

        if self.role == "assistant":
            if any((
                self.api_content is not None,
                self.tool_call_id is not None,
                self.tool_name is not None,
                self.effect_disposition is not None,
            )):
                raise ValueError("assistant message contains fields reserved for user or tool")
            if self.tool_calls is not None:
                if not self.tool_calls:
                    raise ValueError("assistant tool message requires non-empty tool_calls")
                # if self.content is not None:
                #     raise ValueError("assistant tool message must not include text content")
                ids = [call.id for call in self.tool_calls]
                if len(ids) != len(set(ids)):
                    raise ValueError("assistant tool call ids must be unique")
            elif self.content is None or not self.content.strip():
                raise ValueError("assistant text message requires non-blank content")
            return self

        if any((
            self.content is None,
            self.tool_call_id is None,
            self.tool_name is None,
            self.effect_disposition is None,
        )):
            raise ValueError(
                "tool result requires content, tool_call_id, tool_name, and effect_disposition"
            )
        if any((
            self.api_content is not None,
            self.tool_calls is not None,
            self.finish_reason is not None,
        )):
            raise ValueError("tool result contains fields reserved for user or assistant")
        return self

    @classmethod
    def user(
        cls, content: str, *, api_content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Message":
        return cls(
            role="user", content=content, api_content=api_content,
            metadata=metadata or {},
        )

    @classmethod
    def assistant_text(
        cls, content: str, *, finish_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Message":
        return cls(
            role="assistant", content=content, finish_reason=finish_reason,
            metadata=metadata or {},
        )

    @classmethod
    def assistant_tools(
        cls, tool_calls: list[ToolCall], *, content: str | None = None,
        finish_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Message":
        return cls(
            role="assistant", content=content, tool_calls=tool_calls,
            finish_reason=finish_reason, metadata=metadata or {},
        )

    @classmethod
    def tool_result(
        cls, content: str, *, tool_call_id: str, tool_name: str,
        effect_disposition: Literal["none", "not_applied", "confirmed", "unknown"],
        metadata: dict[str, Any] | None = None,
    ) -> "Message":
        return cls(
            role="tool", content=content, tool_call_id=tool_call_id,
            tool_name=tool_name, effect_disposition=effect_disposition,
            metadata=metadata or {},
        )

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


class CanonicalModelRequest(FrozenStrictModel):
    system_prompt: str
    messages: tuple[Message, ...]
    tool_schema_bytes: bytes
    parameters: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str

    @classmethod
    def build(
        cls, *, system_prompt: str, messages: tuple[Message, ...],
        tool_schema_bytes: bytes, parameters: dict[str, Any] | None = None,
    ) -> "CanonicalModelRequest":
        parameters = dict(parameters or {})
        payload = {
            "system_prompt": system_prompt,
            "messages": [message.model_dump(mode="json") for message in messages],
            "tool_schema": json.loads(tool_schema_bytes.decode("utf-8")),
            "parameters": parameters,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return cls(
            system_prompt=system_prompt,
            messages=messages,
            tool_schema_bytes=tool_schema_bytes,
            parameters=parameters,
            fingerprint=hashlib.sha256(encoded).hexdigest(),
        )


class NormalizedModelResponse(StrictModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_semantic_content(self) -> "NormalizedModelResponse":
        if not self.tool_calls and (self.content is None or not self.content.strip()):
            raise ValueError("model response requires text or tool calls")
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("model response tool call ids must be unique")
        return self


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
    side_effects: Literal["none", "possible"]
    state: Literal["succeeded", "failed", "denied", "not_executed", "unknown"]
    effect_disposition: Literal["none", "not_applied", "confirmed", "unknown"]
    error_type: str | None = None
    may_still_be_running: bool = False

    @model_validator(mode="after")
    def _valid_execution_semantics(self) -> "ToolOutcome":
        if self.may_still_be_running and self.state != "unknown":
            raise ValueError("may_still_be_running is only valid for unknown outcomes")
        if self.state in {"denied", "not_executed"}:
            if self.effect_disposition != "not_applied":
                raise ValueError(f"{self.state} outcome must be not_applied")
            return self
        if self.state == "unknown":
            expected = "none" if self.side_effects == "none" else "unknown"
            if self.effect_disposition != expected:
                raise ValueError(
                    f"unknown {self.side_effects} outcome must use {expected}"
                )
            return self
        if self.state == "succeeded":
            expected = "none" if self.side_effects == "none" else "confirmed"
            if self.effect_disposition != expected:
                raise ValueError(
                    f"succeeded {self.side_effects} outcome must use {expected}"
                )
            if self.error_type is not None:
                raise ValueError("succeeded outcome must not include error_type")
            return self
        allowed = (
            {"not_applied", "none"} if self.side_effects == "none"
            else {"not_applied", "unknown"}
        )
        if self.effect_disposition not in allowed:
            raise ValueError(
                f"failed {self.side_effects} outcome has invalid effect_disposition"
            )
        return self


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
    context_version: int | None = Field(default=None, ge=1)
    completion_reason: str = "unknown"
    tool_outcomes: list[ToolOutcome] = Field(default_factory=list)
    provider_attempts: int = Field(default=0, ge=0)
    route_attempts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_terminal_contract(self) -> "TurnResult":
        if self.status == "completed" and self.error is not None:
            raise ValueError("completed turn must not include an error")
        if self.status in {"failed", "interrupted"} and self.error is None:
            raise ValueError(f"{self.status} turn requires an error")
        if any(value < 0 for value in self.route_attempts.values()):
            raise ValueError("route attempts must be non-negative")
        if self.provider_attempts != sum(self.route_attempts.values()):
            raise ValueError("provider_attempts must equal the sum of route_attempts")
        return self
