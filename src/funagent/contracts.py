"""Small immutable values shared by configuration, runtime, and tools."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Event
from types import MappingProxyType
import time


class DomainError(Exception):
    """Stable public failure; never include credentials or wire data."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ControlError(DomainError):
    def __init__(self, code):
        super().__init__(code, "Run cancelled" if code == "CANCELLED" else "Run deadline expired")


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    args: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "args", freeze(self.args))


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: str
    content: str
    effect: str = "none"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping

    def __post_init__(self):
        object.__setattr__(self, "parameters", freeze(self.parameters))


@dataclass(frozen=True)
class Message:
    role: str
    content: str = ""
    calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None
    status: str | None = None
    effect: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "calls", tuple(self.calls))


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()


@dataclass(frozen=True)
class ModelReply:
    content: str | None = None
    calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "calls", tuple(self.calls))
        object.__setattr__(self, "usage", freeze(self.usage))


@dataclass(frozen=True)
class RunInput:
    messages: tuple[Message, ...]
    system_prompt: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class RunContext:
    run_id: str = ""
    purpose: str = "main"
    workspace: str = "."
    access: frozenset[str] = frozenset()
    cancel: Event = field(default_factory=Event, repr=False, compare=False)
    deadline: float | None = None
    record_scope: str = "ephemeral"

    def CheckCancelled(self):
        if self.cancel.is_set():
            raise ControlError("CANCELLED")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ControlError("DEADLINE")

    def Remaining(self):
        return float("inf") if self.deadline is None else max(0.0, self.deadline - time.monotonic())


@dataclass(frozen=True)
class RunResult:
    status: str
    final: str | None = None
    reason: str | None = None
    error_code: str | None = None
    tool_results: tuple[ToolResult, ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    iterations: int = 0
    attempts: int = 0
    profile_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "tool_results", tuple(self.tool_results))
        object.__setattr__(self, "usage", freeze(self.usage))
