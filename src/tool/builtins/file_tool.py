"""File tools share bounded IO; reads accept external paths and writes stay scoped."""
from ...frame.contracts import ToolDefinition, ToolResult
from ..tool_registrar import ToolEntry
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
            return ToolResult("", "denied" if error.code == "DENIED" else "error",
                              json.dumps({"code": error.code, "message": str(error)}))
        except FileNotFoundError:
            return ToolResult("", "error", '{"code":"FILE_IO","message":"File or directory does not exist"}')
        except PermissionError:
            return ToolResult("", "error", '{"code":"FILE_IO","message":"No permission to access the file or directory"}')
        except UnicodeError:
            return ToolResult("", "error", '{"code":"FILE_IO","message":"File is not valid UTF-8 text"}')
        except OSError:
            return ToolResult("", "error", '{"code":"FILE_IO","message":"Unable to access the file or directory"}')
        except (ValueError, RuntimeError):
            return ToolResult("", "error", '{"code":"FILE_IO","message":"Invalid file path"}')


def register_tools(registrar):
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    read_path = {**path, "description": "Absolute path or path relative to the session workspace; '..' is allowed."}
    write_path = {**path, "description": "Path relative to the session workspace; absolute paths and '..' are forbidden."}
    number = lambda maximum: {"type": "integer", "minimum": 1, "maximum": maximum}
    digest = {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"}
    specs = {
        "list_directory": ({"path": {**read_path, "description": read_path["description"] + " Must be a directory; defaults to '.'."},
                            "max_entries": number(500), "cursor": {"type": "integer", "minimum": 0, "maximum": 5000}, "depth": number(5)}, []),
        "search_files": ({"path": {**read_path, "description": read_path["description"] + " File or directory to search; defaults to '.'."},
                          "query": {"type": "string", "minLength": 1, "maxLength": 500,
                                    "description": "Case-insensitive literal substring; use glob for wildcard filtering."},
                          "glob": {"type": "string", "minLength": 1, "maxLength": 200,
                                   "description": "Wildcard filter for paths relative to the search directory, or the filename for a single file."},
                          "max_results": number(200), "mode": {"enum": ["content", "name", "both"], "default": "content"},
                          "depth": {**number(8), "description": "Directory recursion depth; ignored for a single file."}}, ["query"]),
        "read_file": ({"path": {**read_path, "description": read_path["description"] + " Must be a UTF-8 text file."},
                       "start_line": number(1000000), "max_lines": number(500)}, ["path"]),
        "write_file": ({"path": write_path, "content": {"type": "string", "maxLength": 500000}, "expected_sha256": digest}, ["path", "content"]),
        "patch_file": ({"path": write_path, "old_text": {"type": "string", "minLength": 1, "maxLength": 200000},
                        "new_text": {"type": "string", "maxLength": 200000}, "expected_sha256": digest}, ["path", "old_text", "new_text"]),
    }
    descriptions = {
        "list_directory": "List a directory at an absolute or workspace-relative path. The result path is absolute; entry names are relative to it.",
        "search_files": "Search one file or recursively search a directory by literal substring. Accepts absolute or workspace-relative paths; match paths are absolute and can be passed to read_file.",
        "read_file": "Read numbered UTF-8 text lines at an absolute or workspace-relative path. The result path is absolute.",
        "write_file": "Write a UTF-8 text file within the workspace; requires approval and an expected hash when replacing a file.",
        "patch_file": "Patch a UTF-8 text file within the workspace; requires approval and a unique matching old_text.",
    }
    for name, (properties, required) in specs.items():
        write = name in {"write_file", "patch_file"}
        registrar.Add(ToolEntry(name, ToolDefinition(name, descriptions[name], {
            "type": "object", "properties": properties, "required": required, "additionalProperties": False}),
            lambda backend, operation=name: FileHandler(operation), toolsets=("workspace",), purposes=("main",),
            access={"workspace.write" if write else "workspace.read"},
            policy=ActionPolicy({}, default=ToolDecision("approval" if write else "allow"))))
