from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from .registry import ToolEntry


# Named compositions are policy, while ToolEntry.toolset is the tool's base category.
TOOLSETS: dict[str, dict[str, tuple[str, ...]]] = {
    "memory": {"tools": (), "includes": ()},
    "mcp": {"tools": (), "includes": ()},
    "default": {
        "tools": (
            "calculator", "current_time", "list_directory", "read_file",
            "search_files",
            "web_search", "web_extract", "clarify",
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images", "browser_console",
            "computer_use",
        ),
        "includes": (),
    },
    "file": {
        "tools": (
            "list_directory", "read_file", "search_files", "write_file",
            "patch_file",
        ),
        "includes": (),
    },
    "terminal": {"tools": ("terminal",), "includes": ()},
    "coding": {"tools": (), "includes": ("default", "file", "terminal")},
}


@dataclass(frozen=True, slots=True)
class ToolsetRequest:
    enabled_toolsets: tuple[str, ...]
    disabled_toolsets: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    enabled_tools: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()


def select_tool_names(
    request: ToolsetRequest, candidates: Iterable[str], *, disabled_names: Iterable[str] = (),
) -> tuple[str, ...]:
    """Apply individual switches; all explicit denies take precedence."""
    enabled = set(candidates).union(request.enabled_tools)
    disabled = set(disabled_names).union(request.disabled_tools)
    return tuple(sorted(enabled - disabled))


class ToolsetResolver(Protocol):
    def resolve(
        self, request: ToolsetRequest, entries: Sequence[ToolEntry]
    ) -> tuple[str, ...]: ...


class DefaultToolsetResolver:
    """Resolve Session toolset policy to deterministic candidate tool names."""

    def resolve(
        self, request: ToolsetRequest, entries: Sequence[ToolEntry]
    ) -> tuple[str, ...]:
        by_toolset: dict[str, set[str]] = {}
        registered_names: set[str] = set()
        for entry in entries:
            registered_names.add(entry.name)
            by_toolset.setdefault(entry.toolset, set()).add(entry.name)

        enabled: set[str] = set()
        for name in request.enabled_toolsets:
            enabled.update(_resolve_toolset(name, by_toolset, set()))

        disabled: set[str] = set()
        for name in request.disabled_toolsets:
            disabled.update(_resolve_toolset(name, by_toolset, set()))

        return tuple(
            name for name in select_tool_names(request, enabled, disabled_names=disabled)
            if name in registered_names
        )


def _resolve_toolset(
    name: str, registered: dict[str, set[str]], visiting: set[str]
) -> set[str]:
    if name in visiting:
        cycle = " -> ".join((*sorted(visiting), name))
        raise ValueError(f"toolset include cycle: {cycle}")
    definition = TOOLSETS.get(name)
    if definition is None and name not in registered:
        raise ValueError(f"unknown toolset: {name}")

    visiting.add(name)
    tools = set(registered.get(name, ()))
    if definition is not None:
        tools.update(definition["tools"])
        for included in definition["includes"]:
            tools.update(_resolve_toolset(included, registered, visiting))
    visiting.remove(name)
    return tools


__all__ = [
    "DefaultToolsetResolver", "TOOLSETS", "ToolsetRequest", "ToolsetResolver",
    "select_tool_names",
]
