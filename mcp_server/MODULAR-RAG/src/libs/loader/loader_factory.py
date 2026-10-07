"""Factory for creating document Loader instances.

This module implements the Factory Pattern to instantiate the appropriate
Loader based on a file's extension, so the ingestion pipeline can accept a
mixed directory of formats without knowing which parser handles which file.

Unlike the other factories in ``src/libs`` -- which read a provider name out
of settings.yaml -- the loader is chosen by the *document*, not by config:
one ingestion run legitimately spans several formats at once.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict, Tuple, Type

from src.libs.loader.base_loader import BaseLoader
from src.libs.loader.markdown_loader import MarkdownLoader
from src.libs.loader.pdf_loader import PdfLoader


class LoaderFactory:
    """Factory for creating Loader instances by file extension.

    Design Principles Applied:
    - Factory Pattern: Centralizes the extension -> loader decision.
    - Open/Closed: New formats register themselves; callers never change.
    - Fail-Fast: An unsupported extension raises with the supported list.

    Example:
        >>> loader = LoaderFactory.create_for("report.pdf", extract_images=True)
        >>> doc = loader.load("report.pdf")
    """

    # Registry of extension -> loader class. Extensions include the leading dot
    # and are stored lowercase.
    _LOADERS: Dict[str, Type[BaseLoader]] = {
        '.pdf': PdfLoader,
        '.md': MarkdownLoader,
        '.markdown': MarkdownLoader,
    }

    @classmethod
    def register_loader(cls, extension: str, loader_class: Type[BaseLoader]) -> None:
        """Register a Loader implementation for a file extension.

        Args:
            extension: File extension including the leading dot (e.g. '.docx').
            loader_class: The BaseLoader subclass handling that extension.

        Raises:
            ValueError: If loader_class doesn't inherit from BaseLoader, or the
                extension is malformed.
        """
        if not (isinstance(extension, str) and extension.startswith('.')):
            raise ValueError(f"Extension must start with a dot, got: {extension!r}")
        if not (inspect.isclass(loader_class) and issubclass(loader_class, BaseLoader)):
            raise ValueError(
                f"Loader class {loader_class!r} must inherit from BaseLoader"
            )
        cls._LOADERS[extension.lower()] = loader_class

    @classmethod
    def supported_extensions(cls) -> Tuple[str, ...]:
        """Return every registered extension, sorted.

        Callers (e.g. scripts/ingest.py) use this as the single source of truth
        for "what can this project ingest", so a newly registered format shows
        up everywhere at once.
        """
        return tuple(sorted(cls._LOADERS))

    @classmethod
    def create_for(cls, file_path: str | Path, **kwargs: Any) -> BaseLoader:
        """Create the Loader that handles *file_path*'s format.

        Args:
            file_path: Path whose extension selects the loader. The file does
                not have to exist yet -- only the suffix is read here.
            **kwargs: Loader options (e.g. extract_images, image_storage_dir).
                Options a given loader does not accept are dropped rather than
                raising, so one call site can serve every format.

        Returns:
            An initialized BaseLoader subclass instance.

        Raises:
            ValueError: If no loader is registered for the extension.
        """
        suffix = Path(file_path).suffix.lower()
        loader_class = cls._LOADERS.get(suffix)
        if loader_class is None:
            raise ValueError(
                f"Unsupported file type: {suffix or '(no extension)'}. "
                f"Supported: {', '.join(cls.supported_extensions())}"
            )

        accepted = inspect.signature(loader_class.__init__).parameters
        relevant = {k: v for k, v in kwargs.items() if k in accepted}
        return loader_class(**relevant)
