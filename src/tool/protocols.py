"""The shared Run consumes only schema selection and execution."""
from collections.abc import Mapping
from typing import Protocol
from ..frame.contracts import RunContext, ToolCall, ToolDefinition, ToolResult
from .contracts import ToolSelection, ToolSelectionRequest, ToolDecision, BackendResult, Availability, RemoteToolSpec


class ToolHandler(Protocol):
    @staticmethod
    def Execute(args: Mapping, context: RunContext) -> ToolResult: ...


class ToolPolicy(Protocol):
    def Decide(self, call: ToolCall, context: RunContext) -> ToolDecision: ...


class ToolDispatch(Protocol):
    def Select(self, request: ToolSelectionRequest, context: RunContext) -> ToolSelection: ...
    def Execute(self, call: ToolCall, selection: ToolSelection, context: RunContext) -> ToolResult: ...


class ToolBackend(Protocol):
    def Execute(self, operation: str, args: Mapping, context: RunContext) -> BackendResult: ...
    def Close(self) -> None: ...


class BackendAvailability(Protocol):
    def Probe(self) -> Availability: ...


class MCPDiscovery(Protocol):
    def ListTools(self, context: RunContext) -> tuple[RemoteToolSpec, ...]: ...


class KnowledgeBackend(Protocol):
    def ToolSpec(self, operation: str) -> RemoteToolSpec | None: ...
    def Execute(self, operation: str, args: Mapping, context: RunContext) -> BackendResult: ...


class ToolDiagnostics(Protocol):
    def Inspect(self, request: ToolSelectionRequest, context: RunContext) -> list[dict]: ...
    def Connections(self) -> list[dict]: ...
