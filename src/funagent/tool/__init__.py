"""A startup-bound registry, shared by schema exposure and dispatch."""
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Protocol

from ..contracts import ControlError, DomainError, RunContext, ToolCall, ToolDefinition, ToolResult


class ToolHandler(Protocol):
    @staticmethod
    def Execute(args: Mapping, context: RunContext) -> ToolResult: ...


class ToolMgr:
    _instance = None

    @classmethod
    def Initialize(cls, definitions: Mapping[str, ToolDefinition], handlers: Mapping[str, ToolHandler]) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Tool registry already initialized")
        if set(definitions) != set(handlers) or any(key != value.name for key, value in definitions.items()):
            raise DomainError("CONFIG_REFERENCE", "Invalid tool bindings")
        instance = cls()
        instance._registry = MappingProxyType({key: (value, handlers[key]) for key, value in definitions.items()})
        cls._instance = instance

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            raise DomainError("NOT_READY", "Tool registry not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        cls._instance = None

    def Definitions(self, allowed_names: tuple[str, ...]) -> tuple[ToolDefinition, ...]:
        if len(set(allowed_names)) != len(allowed_names) or any(name not in self._registry for name in allowed_names):
            raise DomainError("CONFIG_REFERENCE", "Unknown or duplicate allowed tool")
        return tuple(self._registry[name][0] for name in allowed_names)

    def Execute(self, call: ToolCall, allowed_names: tuple[str, ...], context: RunContext) -> ToolResult:
        context.CheckCancelled()
        if call.name not in self._registry:
            return ToolResult(call.call_id, "invalid", "Unknown tool")
        if call.name not in allowed_names:
            return ToolResult(call.call_id, "denied", "Tool not allowed")
        try:
            result = self._registry[call.name][1].Execute(call.args, context)
            if not isinstance(result, ToolResult) or result.status not in {"ok", "denied", "invalid", "error", "unknown", "cancelled"}:
                raise ValueError
            if not isinstance(result.content, str) or result.effect not in {"none", "known", "unknown"}:
                raise ValueError
            return replace(result, call_id=call.call_id)
        except ControlError:
            return ToolResult(call.call_id, "unknown", "Tool interrupted; outcome unknown", effect="unknown")
        except Exception:
            return ToolResult(call.call_id, "unknown", "Tool execution failed", effect="unknown")
