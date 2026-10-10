"""A bounded read of a UTF-8 regular file in the trusted workspace."""
from pathlib import Path, PureWindowsPath
import stat

from ..contracts import ControlError, ToolResult


class ReadWorkspaceHandler:
    @staticmethod
    def Execute(args, context):
        if "workspace.read" not in context.access:
            return ToolResult("", "denied", "Workspace read access required")
        relative = Path(args["path"])
        windows = PureWindowsPath(args["path"])
        if relative.anchor or windows.anchor or ".." in relative.parts or ".." in windows.parts or ":" in args["path"]:
            return ToolResult("", "denied", "Path must stay within the workspace")
        try:
            context.CheckCancelled()
            root = Path(context.workspace).resolve(strict=True)
            target = (root / relative).resolve(strict=True)
            if not target.is_relative_to(root):
                return ToolResult("", "denied", "Path must stay within the workspace")
            context.CheckCancelled()
            info = target.stat()
            if not stat.S_ISREG(info.st_mode):
                return ToolResult("", "invalid", "Only regular files can be read")
            if info.st_size > 65536:
                return ToolResult("", "invalid", "File exceeds the 65536 byte limit")
            context.CheckCancelled()
            with target.open("rb") as stream:
                content = stream.read(65537)
            context.CheckCancelled()
            if len(content) > 65536:
                return ToolResult("", "invalid", "File exceeds the 65536 byte limit")
            return ToolResult("", "ok", content.decode("utf8"))
        except ControlError:
            return ToolResult("", "cancelled", "Workspace read interrupted")
        except (OSError, UnicodeError, ValueError, RuntimeError):
            return ToolResult("", "error", "Unable to read a UTF-8 workspace file")
