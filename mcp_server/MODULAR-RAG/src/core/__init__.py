"""
Core Layer - Core business logic.

This package contains the core business logic including:
- Configuration management (settings.py)
- Core data types (types.py) - shared contracts for all pipeline stages
- Query engine
- Response building
- Trace collection
"""

from src.core.types import Chunk, ChunkRecord, Metadata, Vector, SparseVector

__all__ = [
    "Chunk", 
    "ChunkRecord",
    "Metadata",
    "Vector",
    "SparseVector"
]
