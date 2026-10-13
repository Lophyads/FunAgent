"""Markdown Loader implementation.

Markdown needs no conversion -- it goes through the same loader contract so
the rest of the pipeline never learns what a document originally was.

Plain .txt is deliberately NOT handled: tests/e2e/test_data_ingestion.py pins
it as the example of a file this project refuses, and a loose text file is a
document nobody chose to structure. Register one here if that changes.

Features:
- Markdown loading with the same Document contract as PdfLoader
- Title and SHA256 identity inherited from BaseLoader (format-independent)
- Direct UTF-8 reads preserve Markdown structure without a conversion layer

No image extraction: Markdown carries no embedded binaries. An image in it is
a link to a file the loader does not own, so there is nothing to pull out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.core.types import Document
from src.libs.loader.base_loader import BaseLoader


class MarkdownLoader(BaseLoader):
    """Loader for Markdown documents.

    This loader:
    1. Reads the file and normalizes it to Markdown (a no-op for .md itself)
    2. Derives a stable doc_id from the file's SHA256, exactly like PdfLoader
    3. Extracts a title from the first heading, falling back to the first line

    Supported extensions:
        .md, .markdown

    Markdown is read directly as UTF-8 because it is already the canonical
    text format consumed by the rest of the pipeline.
    """

    SUPPORTED_EXTENSIONS = ('.md', '.markdown')

    def __init__(self) -> None:
        """Initialize the Markdown Loader.

        Unlike PdfLoader there is nothing to configure.
        """
        pass

    def load(self, file_path: str | Path) -> Document:
        """Load and parse a Markdown file.

        Args:
            file_path: Path to the document.

        Returns:
            Document with Markdown text and metadata.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            ValueError: If the extension is not supported.
            RuntimeError: If the file cannot be read at all.
        """
        path = self._validate_file(file_path)
        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"File is not a supported Markdown document: {path} "
                f"(expected one of {', '.join(self.SUPPORTED_EXTENSIONS)})"
            )

        doc_hash = self._compute_file_hash(path)
        doc_id = f"doc_{doc_hash[:16]}"

        text_content = self._read_text(path)

        metadata: Dict[str, Any] = {
            "source_path": str(path),
            "doc_type": "markdown",
            "doc_hash": doc_hash,
        }

        title = self._extract_title(text_content)
        if title:
            metadata["title"] = title

        return Document(
            id=doc_id,
            text=text_content,
            metadata=metadata
        )

    def _read_text(self, path: Path) -> str:
        """Read Markdown without passing it through a document converter.

        Args:
            path: Path to the document.

        Returns:
            The document's Markdown text.

        Raises:
            RuntimeError: If the file cannot be read.
        """
        try:
            return path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            raise RuntimeError(f"Text loading failed: {e}") from e
