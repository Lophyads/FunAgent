"""Built-in discovery and per-turn model tool loading."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .context import ToolAvailabilityContext

from .registry import ToolEntry, ToolRegistry
from .toolsets import DefaultToolsetResolver, ToolsetRequest, ToolsetResolver


logger = logging.getLogger(__name__)


def discover_builtin_tools(registry: ToolRegistry) -> tuple[str, ...]:
    """Import every *_tool.py module and call its explicit registration hook."""
    candidate = ToolRegistry()
    imported: list[str] = []
    tools_dir = Path(__file__).resolve().parent
    for path in sorted(tools_dir.glob("*_tool.py")):
        module_name = f"{__package__}.{path.stem}"
        try:
            module = importlib.import_module(module_name)
            register = getattr(module, "register_tools", None)
            if not callable(register):
                raise TypeError("module must define register_tools(registry)")
            register(candidate)
        except Exception as exc:
            raise ValueError(f"tool discovery failed in {module_name}: {exc}") from exc
        imported.append(module_name)
    candidate.freeze()
    registry.replace_all(candidate.entries())
    return tuple(imported)


def get_tool_definitions(
    request: ToolsetRequest,
    registry: ToolRegistry,
    resolver: ToolsetResolver | None = None,
    *, availability: ToolAvailabilityContext,
) -> tuple[dict[str, Any], ...]:
    _, schema_bytes = load_tools(request, registry, resolver, availability=availability)
    return tuple(json.loads(schema_bytes))


def load_tools(
    request: ToolsetRequest,
    registry: ToolRegistry,
    resolver: ToolsetResolver | None = None,
    *, availability: ToolAvailabilityContext,
) -> tuple[Mapping[str, ToolEntry[Any]], bytes]:
    """Load definitions and matching entries from one registry generation."""
    entries = registry.entries()
    selected_names = (resolver or DefaultToolsetResolver()).resolve(request, entries)
    entries_by_name = {entry.name: entry for entry in entries}
    check_results: dict[object, bool] = {}
    selected: list[ToolEntry[Any]] = []

    for name in selected_names:
        # Keep explicit denies effective even for custom resolvers.
        if name in request.disabled_tools:
            continue
        entry = entries_by_name.get(name)
        if entry is None:
            continue
        if not entry.required_permissions.issubset(request.permissions):
            continue
        if any(not getattr(availability.capabilities, name) for name in entry.required_capabilities):
            continue
        if entry.check_fn is not None:
            if entry.check_fn not in check_results:
                try:
                    check_results[entry.check_fn] = bool(entry.check_fn(availability))
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
    return selected_entries, schema_bytes


__all__ = [
    "discover_builtin_tools", "get_tool_definitions",
    "load_tools",
]
