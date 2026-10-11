"""Startup binding for MemoryOperationsMgr."""

from ..frame.contracts import DomainError
from .protocols import MemoryOperationsAccess


class MemoryOperationsMgr:
    """The shared CLI/Web management use case is bound once at startup."""

    _instance: MemoryOperationsAccess | None = None

    @classmethod
    def Initialize(cls, operations: MemoryOperationsAccess) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Memory operations already initialized")
        cls._instance = operations

    @classmethod
    def GetInstance(cls) -> MemoryOperationsAccess:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Memory operations not initialized")
        return cls._instance

    @classmethod
    def Close(cls) -> None:
        instance, cls._instance = cls._instance, None
        if instance is not None:
            instance.Close()
