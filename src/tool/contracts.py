"""A tool driver reports a stable failure code and known/unknown effect."""
from dataclasses import dataclass
from ..frame.contracts import ToolDefinition, freeze
from collections.abc import Mapping


def ComputerReadOnly(args):
    action = args.get("action")
    return action in {"capture", "list_apps", "list_windows", "wait"} or action == "focus_app" and not args.get("raise_window", False)


@dataclass(frozen=True)
class ToolDecision:
    outcome: str = "allow"
    effect: str = "none"


@dataclass(frozen=True)
class BackendResult:
    status: str
    content: str
    effect: str = "none"


@dataclass(frozen=True)
class Availability:
    available: bool = True
    capabilities: frozenset[str] = frozenset()
    code: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True)
class RemoteToolSpec:
    name: str
    description: str
    parameters: Mapping
    decision: ToolDecision = ToolDecision("approval", "unknown")

    def __post_init__(self):
        object.__setattr__(self, "parameters", freeze(self.parameters))


@dataclass(frozen=True)
class ToolSelectionRequest:
    toolsets: tuple[str, ...] = ()
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()

    def __post_init__(self):
        for field in ("toolsets", "enable", "disable"):
            object.__setattr__(self, field, tuple(getattr(self, field)))


@dataclass(frozen=True)
class ToolSelection:
    definitions: tuple[ToolDefinition, ...]
    allowed_names: tuple[str, ...]
    registry_id: str
    request: ToolSelectionRequest

    def __post_init__(self):
        object.__setattr__(self, "definitions", tuple(self.definitions))
        object.__setattr__(self, "allowed_names", tuple(self.allowed_names))


class ToolFailure(Exception):
    def __init__(self, code, *, effect="none"):
        self.code, self.effect = code, effect
        super().__init__(code)
