"""Borrow a startup-owned backend; the handler never closes its connection."""
from ...frame.contracts import ControlError, ToolResult
from ..contracts import BackendResult


class BackendHandler:
    def __init__(self, backend, operation, *, read_only=False):
        self._backend, self._operation, self._read_only = backend, operation, read_only

    def Execute(self, args, context):
        try:
            result = self._backend.Execute(self._operation, args, context)
        except ControlError:
            if self._read_only(args) if callable(self._read_only) else self._read_only:
                return ToolResult("", "cancelled", '{"code":"CANCELLED"}')
            raise
        if not isinstance(result, BackendResult):
            raise ValueError("Invalid backend result")
        return ToolResult("", result.status, result.content, result.effect)
