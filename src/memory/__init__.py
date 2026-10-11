"""Public memory contracts, protocols and startup bindings."""

from ..frame.contracts import DomainError
from .memory_mgr import MemoryMgr
from .memory_operations_mgr import MemoryOperationsMgr
from .contracts import (
    EmbeddingProfile,
    MemoryContext,
    MemoryDraft,
    MemoryListQuery,
    MemoryPatch,
    MemoryPrepareRequest,
    MemoryQuery,
    MemoryRecord,
    MemoryRef,
    MemorySearchResult,
)
from .protocols import (
    Embedder,
    MemoryAccess,
    MemoryOperationsAccess,
    MemoryReader,
    MemoryStore,
    MemoryWriter,
    ProfileReader,
    WritableMemoryStore,
)

__all__ = [
    "DomainError",
    "Embedder",
    "EmbeddingProfile",
    "MemoryAccess",
    "MemoryContext",
    "MemoryDraft",
    "MemoryListQuery",
    "MemoryMgr",
    "MemoryOperationsAccess",
    "MemoryOperationsMgr",
    "MemoryPatch",
    "MemoryPrepareRequest",
    "MemoryQuery",
    "MemoryReader",
    "MemoryRecord",
    "MemoryRef",
    "MemorySearchResult",
    "MemoryStore",
    "MemoryWriter",
    "ProfileReader",
    "WritableMemoryStore",
]
