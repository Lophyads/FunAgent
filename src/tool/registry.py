"""Explicit declarations collected privately during application startup."""
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
import re

from ..frame.contracts import DomainError, ToolDefinition, freeze
from ..frame.schema import ToolValidator
from .protocols import ToolPolicy


@dataclass(frozen=True)
class ToolEntry:
    tool_id: str
    definition: ToolDefinition
    handler_factory: Callable
    toolsets: tuple[str, ...] = ()
    purposes: tuple[str, ...] = ("main", "recall", "review")
    access: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    check_fn: Callable | None = None
    policy: ToolPolicy | None = None
    backend_id: str | None = None
    purpose_definitions: Mapping[str, ToolDefinition] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "toolsets", tuple(self.toolsets))
        object.__setattr__(self, "purposes", tuple(self.purposes))
        object.__setattr__(self, "access", frozenset(self.access))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "purpose_definitions", freeze(self.purpose_definitions))


class ToolRegistrar:
    def __init__(self):
        self._entries = []
        self._sealed = False

    def Add(self, entry: ToolEntry):
        if self._sealed:
            raise DomainError("CONFIG_INVALID", "Tool candidate is frozen")
        if (not isinstance(entry, ToolEntry) or not isinstance(entry.tool_id, str) or not entry.tool_id
                or not isinstance(entry.definition, ToolDefinition)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry.definition.name)
                or not isinstance(entry.definition.description, str) or not callable(entry.handler_factory)):
            raise DomainError("CONFIG_INVALID", "Invalid tool declaration")
        for names in (entry.toolsets, entry.purposes, entry.access, entry.capabilities):
            if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
                raise DomainError("CONFIG_INVALID", "Invalid tool policy metadata")
        if ((entry.check_fn is not None and not callable(entry.check_fn))
                or (entry.policy is not None and not callable(getattr(entry.policy, "Decide", None)))
                or (entry.backend_id is not None and (not isinstance(entry.backend_id, str) or not entry.backend_id))):
            raise DomainError("CONFIG_INVALID", "Invalid tool policy or backend declaration")
        if any(e.tool_id == entry.tool_id or e.definition.name == entry.definition.name for e in self._entries):
            raise DomainError("CONFIG_INVALID", "Duplicate tool identity or name")
        ToolValidator(entry.definition.parameters)
        for purpose, definition in entry.purpose_definitions.items():
            if (purpose not in entry.purposes or not isinstance(definition, ToolDefinition)
                    or definition.name != entry.definition.name):
                raise DomainError("CONFIG_INVALID", "Invalid purpose-specific tool schema")
            ToolValidator(definition.parameters)
        self._entries.append(entry)

    def Entries(self):
        return tuple(self._entries)

    def Freeze(self):
        self._sealed = True
        return self.Entries()
