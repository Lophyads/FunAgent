from __future__ import annotations

import inspect
import json
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from agentcore.interfaces import ToolContext
    from .context import ToolAvailabilityContext


ArgsT = TypeVar("ArgsT", bound=BaseModel)
ToolHandler = Callable[[ArgsT, "ToolContext"], Any]
ToolCheck = Callable[["ToolAvailabilityContext"], bool]
ToolPolicyResolver = Callable[[ArgsT], "ToolExecutionPolicy"]
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    side_effects: Literal["none", "possible"] = "possible"
    approval_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.side_effects not in {"none", "possible"}:
            raise ValueError("invalid side_effects classification")
        if any(not scope.strip() for scope in self.approval_scopes):
            raise ValueError("approval scopes must not be blank")


@dataclass(frozen=True, slots=True)
class ToolEntry(Generic[ArgsT]):
    """One registered tool's model schema and executable implementation."""

    tool_id: str
    implementation_version: str
    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]
    toolset: str = "default"
    check_fn: ToolCheck | None = None
    required_capabilities: frozenset[str] = frozenset()
    required_permissions: frozenset[str] = frozenset()
    requires_approval: bool = False
    side_effects: Literal["none", "possible"] = "possible"
    timeout_seconds: float | None = None
    policy_resolver: ToolPolicyResolver[ArgsT] | None = None
    interactive_wait: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


class ToolRegistry:
    """Thread-safe registry of tool entries available to future turns."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[str, ToolEntry[Any]] = {}
        self._frozen = False
        self._ordered: tuple[ToolEntry[Any], ...] = ()
        self._generation = 0

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def register(self, entry: ToolEntry[Any]) -> None:
        with self._lock:
            if self._frozen:
                raise RuntimeError(
                    "tool registry is frozen; use replace_all() between turns"
                )
            self._validate_entry(entry)
            if entry.name in self._tools:
                raise ValueError(f"tool already registered: {entry.name}")
            identity = (entry.tool_id, entry.implementation_version)
            if any(
                (registered.tool_id, registered.implementation_version) == identity
                for registered in self._tools.values()
            ):
                raise ValueError(
                    "tool implementation already registered: "
                    f"{entry.tool_id}@{entry.implementation_version}"
                )
            self._tools[entry.name] = entry
            self._generation += 1

    @staticmethod
    def _validate_entry(entry: ToolEntry[Any]) -> None:
        if not isinstance(entry, ToolEntry):
            raise TypeError("tool registry accepts ToolEntry instances")
        if not _TOOL_NAME_RE.fullmatch(entry.tool_id):
            raise ValueError(f"invalid tool id: {entry.tool_id!r}")
        if not entry.implementation_version.strip():
            raise ValueError("implementation_version must not be blank")
        if not _TOOL_NAME_RE.fullmatch(entry.name):
            raise ValueError(f"invalid tool name: {entry.name!r}")
        if not _TOOL_NAME_RE.fullmatch(entry.toolset):
            raise ValueError(f"invalid toolset: {entry.toolset!r}")
        if not entry.description.strip():
            raise ValueError("tool description must not be blank")
        if not isinstance(entry.args_model, type) or not issubclass(entry.args_model, BaseModel):
            raise TypeError("args_model must be a pydantic BaseModel subclass")
        if not callable(entry.handler):
            raise TypeError("tool handler must be callable")
        ToolExecutionPolicy(side_effects=entry.side_effects)
        if not entry.required_capabilities.issubset({"clarify", "approval"}):
            raise ValueError("unknown host capability")
        try:
            json.dumps(entry.definition(), allow_nan=False)
        except Exception as exc:
            raise ValueError(f"invalid schema for tool {entry.name}: {exc}") from exc
        if inspect.iscoroutinefunction(entry.handler) or inspect.iscoroutinefunction(
            getattr(entry.handler, "__call__", None)
        ):
            raise TypeError(
                "async tool handlers are not supported; provide a synchronous adapter"
            )
        if entry.check_fn is not None:
            if not callable(entry.check_fn):
                raise TypeError("check_fn must be callable")
            if inspect.iscoroutinefunction(entry.check_fn):
                raise TypeError("async tool checks are not supported")
        if entry.timeout_seconds is not None and entry.timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be greater than zero")
        if entry.policy_resolver is not None:
            if not callable(entry.policy_resolver) or inspect.iscoroutinefunction(
                entry.policy_resolver
            ):
                raise TypeError("policy_resolver must be synchronous")

    def replace_all(self, entries: tuple[ToolEntry[Any], ...]) -> None:
        """Atomically publish entries for future turns."""
        names: dict[str, ToolEntry[Any]] = {}
        identities: set[tuple[str, str]] = set()
        for entry in entries:
            self._validate_entry(entry)
            identity = (entry.tool_id, entry.implementation_version)
            if entry.name in names:
                raise ValueError(f"duplicate tool name: {entry.name}")
            if identity in identities:
                raise ValueError(
                    "duplicate tool implementation identity: "
                    f"{entry.tool_id}@{entry.implementation_version}"
                )
            names[entry.name] = entry
            identities.add(identity)
        ordered = tuple(sorted(entries, key=lambda item: (item.tool_id, item.name)))
        with self._lock:
            self._tools = names
            self._ordered = ordered
            self._frozen = True
            self._generation += 1

    def freeze(self) -> None:
        with self._lock:
            if self._frozen:
                return
            self._ordered = tuple(
                sorted(self._tools.values(), key=lambda entry: (entry.tool_id, entry.name))
            )
            self._frozen = True

    def get_entry(self, name: str) -> ToolEntry[Any] | None:
        with self._lock:
            return self._tools.get(name)

    def entries(self) -> tuple[ToolEntry[Any], ...]:
        with self._lock:
            if not self._frozen:
                raise RuntimeError("freeze the tool registry before reading entries")
            return self._ordered



__all__ = [
    "ToolCheck", "ToolEntry", "ToolExecutionPolicy", "ToolHandler",
    "ToolPolicyResolver", "ToolRegistry",
]
