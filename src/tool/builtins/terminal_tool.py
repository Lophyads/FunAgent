from ...frame.contracts import ToolDefinition
from ..tool_registrar import ToolEntry
from ..backends.handler import BackendHandler
from ..policy import ActionPolicy
from ..contracts import ToolDecision


def register_tools(registrar):
    registrar.Add(ToolEntry("terminal", ToolDefinition("terminal", "Execute a workspace command after one approval", {
        "type": "object", "additionalProperties": False, "required": ["command"],
        "properties": {"command": {"type": "string", "minLength": 1, "maxLength": 4000},
                       "workdir": {"type": "string", "minLength": 1, "maxLength": 4096},
                       "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}}}),
        lambda backend: BackendHandler(backend, "terminal"), toolsets=("terminal",), purposes=("main",),
        access={"process.execute"}, capabilities={"process.execute"},
        policy=ActionPolicy({}, default=ToolDecision("approval", "unknown"))))
