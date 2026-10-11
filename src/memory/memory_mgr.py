"""Startup binding for MemoryMgr."""

from ..frame.contracts import DomainError
from .protocols import MemoryAccess


class MemoryMgr:
    _instance: MemoryAccess | None = None

    @classmethod
    def Initialize(cls, memory: MemoryAccess) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Memory already initialized")
        cls._instance = memory

    @classmethod
    def GetInstance(cls) -> MemoryAccess:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Memory not initialized")
        return cls._instance

    @classmethod
    def Close(cls) -> None:
        instance, cls._instance = cls._instance, None
        if instance is not None:
            close = getattr(instance, "Close", None)
            if close is not None:
                close()
