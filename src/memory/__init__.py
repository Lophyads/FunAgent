"""Unified memory API; stores are owned by MemoryMgr after initialization."""
from .contracts import (MemoryRef, MemoryRecord, MemoryQuery, MemoryListQuery, MemoryDraft,
                        MemoryPatch, MemorySearchResult, MemoryPrepareRequest, MemoryContext, EmbeddingProfile)
from .protocols import MemoryStore, WritableMemoryStore, Embedder
from .protocols import MemoryAccess, MemoryReader, MemoryWriter, ProfileReader, MemoryOperationsAccess
from ..frame.contracts import DomainError


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
