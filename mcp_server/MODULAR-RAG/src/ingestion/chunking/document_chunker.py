"""Document chunking module - adapts libs.splitter for business layer.

This module serves as the adapter layer between libs.splitter (pure text splitting)
and Ingestion Pipeline (business object transformation). It transforms Document
objects into Chunk objects with proper ID generation, metadata inheritance, and
traceability.

Core Value-Add (vs libs.splitter):
1. Chunk ID Generation: Deterministic and unique IDs for each chunk
2. Metadata Inheritance: Propagates Document metadata to all chunks
3. chunk_index: Records sequential position within document
4. source_ref: Establishes parent-child traceability
5. Type Conversion: str → Chunk object (core.types contract)

Design Principles:
- Adapter Pattern: Bridges text splitter tool with business objects
- Config-Driven: Uses SplitterFactory for configuration-based strategy selection
- Deterministic: Same Document produces same Chunk IDs on repeat splits
- Type-Safe: Enforces core.types.Chunk contract
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, List

from src.core.types import Chunk, Document
from src.libs.splitter.splitter_factory import SplitterFactory

if TYPE_CHECKING:
    from src.core.settings import Settings


class DocumentChunker:
    """Converts Documents into Chunks with business-level enrichment.
    
    This class wraps a text splitter (from libs) and adds business logic:
    - Generates stable chunk IDs
    - Inherits and extends metadata
    - Maintains document traceability
    
    Attributes:
        _splitter: The underlying text splitter from libs layer
        _settings: Configuration settings for chunking behavior
    
    Example:
        >>> from src.core.settings import load_settings
        >>> from src.core.types import Document
        >>> settings = load_settings("config/settings.yaml")
        >>> chunker = DocumentChunker(settings)
        >>> document = Document(
        ...     id="doc_123",
        ...     text="Long document content...",
        ...     metadata={"source_path": "data/report.pdf"}
        ... )
        >>> chunks = chunker.split_document(document)
        >>> print(f"Generated {len(chunks)} chunks")
        >>> print(f"First chunk ID: {chunks[0].id}")
        >>> print(f"First chunk index: {chunks[0].metadata['chunk_index']}")
    """
    
    def __init__(self, settings: Settings):
        """Initialize DocumentChunker with configuration.
        
        Args:
            settings: Configuration settings containing splitter configuration.
                     The splitter config is expected at settings.splitter.*
        
        Raises:
            ValueError: If splitter configuration is invalid or provider unknown
        """
        self._settings = settings
        self._splitter = SplitterFactory.create(settings)
    
    def split_document(self, document: Document) -> List[Chunk]:
        """Split a Document into Chunks with full business enrichment.
        
        This is the main entry point that orchestrates the transformation:
        1. Uses underlying splitter to get text fragments
        2. Generates deterministic IDs for each chunk
        3. Inherits and extends metadata from document
        4. Creates Chunk objects conforming to core.types contract
        
        Args:
            document: Source document to split into chunks
        
        Returns:
            List of Chunk objects with:
            - Unique, deterministic IDs
            - Inherited metadata + chunk_index + source_ref
            - Proper type contract (core.types.Chunk)
        
        Raises:
            ValueError: If document has no text or invalid structure
        
        Example:
            >>> doc = Document(
            ...     id="doc_abc",
            ...     text="Section 1 content.\\n\\nSection 2 content.",
            ...     metadata={"source_path": "file.pdf", "title": "Report"}
            ... )
            >>> chunker = DocumentChunker(settings)
            >>> chunks = chunker.split_document(doc)
            >>> len(chunks) >= 1
            True
            >>> chunks[0].metadata["source_path"]
            'file.pdf'
            >>> chunks[0].metadata["chunk_index"]
            0
            >>> chunks[0].metadata["source_ref"]
            'doc_abc'
        """
        if not document.text or not document.text.strip():
            raise ValueError(f"Document {document.id} has no text content to split")
        
        # Step 1: Use underlying splitter to get text fragments
        text_fragments = self._splitter.split_text(document.text)
        
        if not text_fragments:
            raise ValueError(
                f"Splitter returned no chunks for document {document.id}. "
                f"Text length: {len(document.text)}"
            )
        
        # Step 2: Transform text fragments into Chunk objects with enrichment
        chunks: List[Chunk] = []
        for index, text in enumerate(text_fragments):
            chunk_id = self._generate_chunk_id(document.id, index, text)
            chunk_metadata = self._inherit_metadata(document, index, text)
            
            chunk = Chunk(
                id=chunk_id,
                text=text,
                metadata=chunk_metadata
            )
            chunks.append(chunk)
        
        return chunks
    
    def _generate_chunk_id(self, doc_id: str, index: int, text: str) -> str:
        """Generate unique and deterministic chunk ID.
        
        ID format: {doc_id}_{index:04d}_{content_hash}
        - doc_id: Parent document identifier
        - index: Sequential position (zero-padded to 4 digits)
        - content_hash: First 8 chars of text SHA256 hash
        
        This ensures:
        - Uniqueness: Combination of doc_id + index + content_hash
        - Determinism: Same input always produces same ID
        - Debuggability: Human-readable structure
        
        Args:
            doc_id: Parent document ID
            index: Sequential position of chunk (0-based)
            text: Chunk text content
        
        Returns:
            Unique chunk ID string
        
        Example:
            >>> chunker._generate_chunk_id("doc_123", 0, "Hello world")
            'doc_123_0000_c0535e4b'
        """
        # Compute content hash for uniqueness
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        
        # Format: {doc_id}_{index:04d}_{hash_8chars}
        return f"{doc_id}_{index:04d}_{content_hash}"
    
    def _inherit_metadata(self, document: Document, chunk_index: int, chunk_text: str = "") -> dict:
        """Inherit metadata from document and add chunk-specific fields.
        
        This creates a new metadata dict containing:
        - All fields from document.metadata (copied, not referenced)
        - chunk_index: Sequential position (0-based)
        - source_ref: Reference to parent document ID
        
        Args:
            document: Source document whose metadata to inherit
            chunk_index: Sequential position of this chunk
            chunk_text: Reserved for compatibility with the existing call site.
        
        Returns:
            Metadata dict with inherited and chunk-specific fields
        
        Example:
            >>> doc = Document(
            ...     id="doc_123",
            ...     text="Content",
            ...     metadata={"source_path": "file.pdf", "title": "Report"}
            ... )
            >>> metadata = chunker._inherit_metadata(doc, 2, "Content")
            >>> metadata["source_path"]
            'file.pdf'
            >>> metadata["chunk_index"]
            2
            >>> metadata["source_ref"]
            'doc_123'
        """
        chunk_metadata = document.metadata.copy()
        chunk_metadata.pop("images", None)
        chunk_metadata["chunk_index"] = chunk_index
        chunk_metadata["source_ref"] = document.id
        return chunk_metadata
