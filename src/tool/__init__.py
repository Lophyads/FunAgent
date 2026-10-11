"""Startup-bound dispatch entry; importing it never constructs a registry."""
from ..frame.contracts import DomainError
from .contracts import ToolFailure
from .protocols import ToolDispatch, ToolHandler


class ToolMgr:
    _instance: ToolDispatch | None = None

    @classmethod
    def Initialize(cls, dispatch: ToolDispatch) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Tool registry already initialized")
        cls._instance = dispatch

    @classmethod
    def GetInstance(cls) -> ToolDispatch:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Tool registry not initialized")
        return cls._instance

    @classmethod
    def Close(cls) -> None:
        try:
            if cls._instance is not None:
                close = getattr(cls._instance, "Close", None)
                if close is not None:
                    close()
        finally:
            cls._instance = None
