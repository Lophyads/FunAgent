"""Workspace file tools use one policy and one bounded filesystem implementation."""
from ...frame.contracts import ToolDefinition, ToolResult
from ..registry import ToolEntry
from ..policy import ActionPolicy
from ..contracts import ToolDecision
from .. import files
from ...frame.contracts import ControlError, DomainError
import json


class FileHandler:
    def __init__(self, operation):
        self.operation = operation

    def Execute(self, args, context):
        operation = files.OPERATIONS.get(self.operation)
        if operation is None:
            return ToolResult("", "invalid", "File operation is not implemented")
        try:
            data = operation(args, context)
            return ToolResult("", "ok", json.dumps(data, ensure_ascii=False),
                              "known" if self.operation in {"write_file", "patch_file"} else "none")
        except ControlError:
            return ToolResult("", "cancelled", '{"code":"CANCELLED"}')
        except DomainError as error:
            return ToolResult("", "denied" if error.code == "DENIED" else "error", json.dumps({"code": error.code}))
        except (OSError, UnicodeError, ValueError):
            return ToolResult("", "error", '{"code":"FILE_IO"}')


def register_tools(registrar):
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    number = lambda maximum: {"type": "integer", "minimum": 1, "maximum": maximum}
    digest = {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"}
    specs = {
        "list_directory": ({"path": path, "max_entries": number(500), "cursor": {"type": "integer", "minimum": 0, "maximum": 5000}, "depth": number(5)}, []),
        "search_files": ({"path": path, "query": {"type": "string", "minLength": 1, "maxLength": 500},
                          "glob": {"type": "string", "minLength": 1, "maxLength": 200},
                          "max_results": number(200), "mode": {"enum": ["content", "name", "both"]}, "depth": number(8)}, ["query"]),
        "read_file": ({"path": path, "start_line": number(1000000), "max_lines": number(500)}, ["path"]),
        "write_file": ({"path": path, "content": {"type": "string", "maxLength": 500000}, "expected_sha256": digest}, ["path", "content"]),
        "patch_file": ({"path": path, "old_text": {"type": "string", "minLength": 1, "maxLength": 200000},
                        "new_text": {"type": "string", "maxLength": 200000}, "expected_sha256": digest}, ["path", "old_text", "new_text"]),
    }
    for name, (properties, required) in specs.items():
        write = name in {"write_file", "patch_file"}
        registrar.Add(ToolEntry(name, ToolDefinition(name, "Workspace " + name, {
            "type": "object", "properties": properties, "required": required, "additionalProperties": False}),
            lambda backend, operation=name: FileHandler(operation), toolsets=("workspace",), purposes=("main",),
            access={"workspace.write" if write else "workspace.read"},
            policy=ActionPolicy({}, default=ToolDecision("approval" if write else "allow"))))
