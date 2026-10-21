"""Shared values and cooperative execution controls for runtime and tools."""
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from threading import Event, Lock
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


def thaw(value):
    """JSON-compatible copy of frozen data for wire and persistence encoders."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    args: Mapping = field(default_factory=dict)
    argument_error: str | None = None

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
class ModelReplay:
    version: int
    format: str
    scope: str
    items: tuple = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "items", freeze(self.items))


@dataclass(frozen=True)
class Message:
    role: str
    content: str = ""
    calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None
    status: str | None = None
    effect: str | None = None
    message_id: int = 0
    task_id: str | None = None
    replay: ModelReplay | None = field(default=None, kw_only=True, repr=False)

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
    replay: ModelReplay | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "calls", tuple(self.calls))
        object.__setattr__(self, "usage", freeze(self.usage))


@dataclass(frozen=True)
class RunInput:
    messages: tuple[Message, ...]
    system_prompt: str = ""

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))


class ExecutionBudget:
    """Shared active-time budget; only task-bound human waits may pause it."""

    def __init__(self, timeout):
        if type(timeout) not in {int, float} or not isfinite(timeout) or timeout <= 0:
            raise DomainError("INVALID_REQUEST", "Execution timeout must be positive")
        self._timeout = timeout
        self._started = time.monotonic()
        self._lock = Lock()
        self._paused_at = None
        self._pause_depth = 0
        self._human_wait = 0.0

    def _times(self, now):
        waiting = self._human_wait
        if self._paused_at is not None:
            waiting += max(0.0, now - self._paused_at)
        elapsed = max(0.0, now - self._started - waiting)
        return elapsed, waiting

    def Remaining(self):
        with self._lock:
            elapsed, _ = self._times(time.monotonic())
            return max(0.0, self._timeout - elapsed)

    @contextmanager
    def Pause(self):
        with self._lock:
            now = time.monotonic()
            elapsed, _ = self._times(now)
            if elapsed >= self._timeout:
                raise ControlError("DEADLINE")
            if self._pause_depth == 0:
                self._paused_at = now
            self._pause_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._pause_depth -= 1
                if self._pause_depth == 0:
                    self._human_wait += max(0.0, time.monotonic() - self._paused_at)
                    self._paused_at = None

    def Snapshot(self):
        with self._lock:
            elapsed, waiting = self._times(time.monotonic())
            return dict(execution_duration_ms=elapsed * 1000,
                        human_wait_duration_ms=waiting * 1000,
                        execution_remaining_ms=max(0.0, self._timeout - elapsed) * 1000)


@dataclass(frozen=True)
class RunContext:
    run_id: str = ""
    purpose: str = "main"
    workspace: str = "."
    access: frozenset[str] = frozenset()
    cancel: Event = field(default_factory=Event, repr=False, compare=False)
    deadline: float | None = None
    record_scope: "RecordScope | str" = "ephemeral"
    task_id: str = ""
    session_id: str = ""
    review_evidence: tuple[str, ...] = ()
    memory_evidence: tuple[str, ...] = ()
    execution_budget: ExecutionBudget | None = field(default=None, repr=False, compare=False, kw_only=True)

    def __post_init__(self):
        # The Event and execution budget are shared across child contexts;
        # permission/evidence collections are copied at this trusted boundary.
        object.__setattr__(self, "access", frozenset(self.access))
        object.__setattr__(self, "review_evidence", tuple(self.review_evidence))
        object.__setattr__(self, "memory_evidence", tuple(self.memory_evidence))

    def CheckCancelled(self):
        if self.cancel.is_set():
            raise ControlError("CANCELLED")
        if self.Remaining() <= 0:
            raise ControlError("DEADLINE")

    def Remaining(self):
        local = float("inf") if self.deadline is None else max(0.0, self.deadline - time.monotonic())
        return min(local, self.execution_budget.Remaining()) if self.execution_budget is not None else local


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
    replay: ModelReplay | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "tool_results", tuple(self.tool_results))
        object.__setattr__(self, "usage", freeze(self.usage))


@dataclass(frozen=True)
class RecordScope:
    kind: str = "ephemeral"
    turn_id: str | None = None


@dataclass(frozen=True)
class ReviewJob:
    session_id: str
    through_message_id: int


@dataclass(frozen=True)
class SessionCreate:
    title: str = "New session"
    workspace: str = "."


@dataclass(frozen=True)
class Session:
    session_id: str
    title: str
    workspace: str
    prompt: str | None = None
    created_at: float = 0


@dataclass(frozen=True)
class SessionSnapshot:
    session: Session
    messages: tuple[Message, ...] = ()
    through_message_id: int = 0


@dataclass(frozen=True)
class TurnRequest:
    session_id: str
    text: str
    request_key: str | None = None


@dataclass(frozen=True)
class Task:
    task_id: str
    request: TurnRequest
    status: str
    result: RunResult | None = None
    created_at: float = 0
    finished_at: float | None = None


@dataclass(frozen=True)
class CancelResult:
    task_id: str
    status: str
    requested: bool


@dataclass(frozen=True)
class PageQuery:
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True)
class HistoryQuery(PageQuery):
    direction: str = "forward"


@dataclass(frozen=True)
class Page:
    items: tuple
    next_cursor: str | None = None


@dataclass(frozen=True)
class ApprovalBinding:
    task_id: str
    run_id: str
    call_id: str
    tool_name: str
    action: str
    arguments_digest: str


@dataclass(frozen=True)
class ClarificationQuestion:
    question_id: str
    prompt: str
    options: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.options, (list, tuple)):
            raise DomainError("INVALID_REQUEST", "Question options must be an array")
        object.__setattr__(self, "options", tuple(self.options))


@dataclass(frozen=True)
class Interaction:
    interaction_id: str
    task_id: str
    run_id: str
    kind: str
    prompt: str
    expires_at: float
    approval: ApprovalBinding | None = None
    questions: tuple[ClarificationQuestion, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "questions", tuple(self.questions))


@contextmanager
def controlled_lock(lock, context):
    """Connection acquisition spends the same cooperative IO budget."""
    context.CheckCancelled()
    while not lock.acquire(timeout=min(0.05, context.Remaining())):
        context.CheckCancelled()
    try:
        context.CheckCancelled()
        yield
    finally:
        lock.release()
