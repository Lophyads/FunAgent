"""Built-in discovery and per-turn model tool loading."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .registry import ToolEntry, ToolRegistry
from .toolsets import DefaultToolsetResolver, ToolsetRequest, ToolsetResolver


logger = logging.getLogger(__name__)


def discover_builtin_tools(registry: ToolRegistry) -> tuple[str, ...]:
    """Import every *_tool.py module and call its explicit registration hook."""
    imported: list[str] = []
    tools_dir = Path(__file__).resolve().parent
    for path in sorted(tools_dir.glob("*_tool.py")):
        module_name = f"{__package__}.{path.stem}"
        module = importlib.import_module(module_name)
        register = getattr(module, "register_tools", None)
        if not callable(register):
            raise TypeError(f"{module_name} must define register_tools(registry)")
        register(registry)
        imported.append(module_name)
    return tuple(imported)


def default_tool_registry(
    *, workspace_root: str | Path | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(workspace_root=workspace_root)
    discover_builtin_tools(registry)
    return registry


def get_tool_definitions(
    request: ToolsetRequest,
    registry: ToolRegistry,
    resolver: ToolsetResolver | None = None,
) -> tuple[dict[str, Any], ...]:
    definitions, _, _, _ = load_tools(request, registry, resolver)
    return definitions


def load_tools(
    request: ToolsetRequest,
    registry: ToolRegistry,
    resolver: ToolsetResolver | None = None,
) -> tuple[
    tuple[dict[str, Any], ...],
    frozenset[str],
    Mapping[str, ToolEntry[Any]],
    bytes,
]:
    """Load definitions and matching entries from one registry generation."""
    _, entries = registry.snapshot()
    selected_names = (resolver or DefaultToolsetResolver()).resolve(request, entries)
    entries_by_name = {entry.name: entry for entry in entries}
    check_results: dict[object, bool] = {}
    selected: list[ToolEntry[Any]] = []

    for name in selected_names:
        entry = entries_by_name.get(name)
        if entry is None:
            continue
        if not entry.required_permissions.issubset(request.permissions):
            continue
        if entry.check_fn is not None:
            if entry.check_fn not in check_results:
                try:
                    check_results[entry.check_fn] = bool(entry.check_fn())
                except Exception as exc:
                    check_results[entry.check_fn] = False
                    logger.warning(
                        "tool availability check failed tool=%s error_type=%s",
                        entry.name, type(exc).__name__,
                    )
            if not check_results[entry.check_fn]:
                continue
        selected.append(entry)

    selected.sort(key=lambda entry: (entry.tool_id, entry.name))
    definitions = tuple(entry.definition() for entry in selected)
    schema_bytes = json.dumps(
        definitions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    selected_entries = MappingProxyType({entry.name: entry for entry in selected})
    return (
        definitions,
        frozenset(selected_entries),
        selected_entries,
        schema_bytes,
    )


__all__ = [
    "default_tool_registry", "discover_builtin_tools", "get_tool_definitions",
    "load_tools",
]
