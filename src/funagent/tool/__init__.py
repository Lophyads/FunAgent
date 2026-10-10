"""A startup-bound registry, shared by schema exposure and dispatch."""
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Protocol
import json

from ..contracts import ControlError, DomainError, RunContext, ToolCall, ToolDefinition, ToolResult, thaw

from ..schema import ToolValidator
from ..interaction import InteractionMgr


class ToolFailure(Exception):
    """A driver can explicitly confirm a failed call's effect."""
    def __init__(self, code, *, effect="none"):
        self.code, self.effect = code, effect
        super().__init__(code)


class ToolHandler(Protocol):
    @staticmethod
    def Execute(args: Mapping, context: RunContext) -> ToolResult: ...


class ToolMgr:
    _instance = None

    @classmethod
    def Initialize(cls, definitions: Mapping[str, ToolDefinition], handlers: Mapping[str, ToolHandler], *,
                   approval_names: tuple[str, ...] = ()) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Tool registry already initialized")
        if set(definitions) != set(handlers) or any(key != value.name for key, value in definitions.items()):
            raise DomainError("CONFIG_REFERENCE", "Invalid tool bindings")
        if len(set(approval_names)) != len(approval_names) or not set(approval_names) <= set(definitions):
            raise DomainError("CONFIG_REFERENCE", "Invalid approval tool bindings")
        instance = cls()
        instance._approval_names = frozenset(approval_names)
        instance._registry = MappingProxyType({key: (value, handlers[key], ToolValidator(value.parameters))
                                               for key, value in definitions.items()})
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
        _, handler, validator = self._registry[call.name]
        try:
            valid = validator.is_valid(thaw(call.args))
        except Exception:
            valid = False
        if not valid:
            return ToolResult(call.call_id, "invalid", "Invalid tool arguments")
        if call.name in self._approval_names:
            if context.purpose != "main" or not context.task_id or not context.run_id:
                return ToolResult(call.call_id, "denied", "This run cannot request approval")
            prompt = f"Approve {call.name} ({call.call_id}) with arguments: {json.dumps(thaw(call.args), ensure_ascii=False)}"
            try:
                approved = InteractionMgr.GetInstance().Wait("approval", prompt, context)
            except ControlError:
                return ToolResult(call.call_id, "cancelled", "Approval interrupted")
            except DomainError as error:
                return ToolResult(call.call_id, "error", str(error))
            if not approved:
                return ToolResult(call.call_id, "denied", "Approval declined")
        context.CheckCancelled()
        try:
            result = handler.Execute(call.args, context)
            if not isinstance(result, ToolResult) or result.status not in {"ok", "denied", "invalid", "error", "unknown", "cancelled"}:
                raise ValueError
            if not isinstance(result.content, str) or result.effect not in {"none", "known", "unknown"}:
                raise ValueError
            return replace(result, call_id=call.call_id)
        except ToolFailure as error:
            effect = error.effect if error.effect in {"none", "known", "unknown"} else "unknown"
            return ToolResult(call.call_id, "unknown" if effect == "unknown" else "error",
                              "Tool execution failed", effect=effect)
        except ControlError:
            return ToolResult(call.call_id, "unknown", "Tool interrupted; outcome unknown", effect="unknown")
        except Exception:
            return ToolResult(call.call_id, "unknown", "Tool execution failed", effect="unknown")
