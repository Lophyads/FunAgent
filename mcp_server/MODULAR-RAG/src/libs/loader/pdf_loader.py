"""PDF loader backed by PyMuPDF4LLM."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict

try:
    import pymupdf4llm

    PYMUPDF4LLM_AVAILABLE = True
except ImportError:
    pymupdf4llm = None  # type: ignore[assignment]
    PYMUPDF4LLM_AVAILABLE = False

from src.core.settings import REPO_ROOT
from src.core.types import Document
from src.libs.loader.base_loader import BaseLoader

logger = logging.getLogger(__name__)

_IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)\r\n]+)\)")
_REMOTE_PREFIXES = ("http://", "https://", "data:")


class PdfLoader(BaseLoader):
    """Convert a PDF to Markdown and store extracted images beside the index."""

    def __init__(
        self,
        extract_images: bool = True,
        image_storage_dir: str | Path = "data/images",
    ) -> None:
        if not PYMUPDF4LLM_AVAILABLE:
            raise ImportError(
                "PyMuPDF4LLM is required for PdfLoader. "
                "Install with: pip install pymupdf4llm==1.28.2"
            )
        self.extract_images = extract_images
        self.image_storage_dir = Path(image_storage_dir).resolve()

    def load(self, file_path: str | Path) -> Document:
        path = self._validate_file(file_path)
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"File is not a PDF: {path}")

        doc_hash = self._compute_file_hash(path)
        doc_id = f"doc_{doc_hash[:16]}"
        image_dir = self.image_storage_dir / doc_hash

        try:
            if self.extract_images:
                self._prepare_image_dir(image_dir)
            options = {
                "write_images": self.extract_images,
                "image_path": str(image_dir),
                "image_format": "png",
                "dpi": 150,
                "page_separators": False,
            }
            text_content = pymupdf4llm.to_markdown(
                str(path), header=False, footer=False, **options
            )
            if not self._has_meaningful_text(text_content):
                complete = pymupdf4llm.to_markdown(
                    str(path), header=True, footer=True, **options
                )
                if self._has_meaningful_text(complete):
                    logger.warning(
                        "Header/footer filtering removed all PDF body text; "
                        "using the complete conversion for %s",
                        path,
                    )
                    text_content = complete
            if self.extract_images:
                text_content = self._make_image_links_relative(text_content, image_dir)
        except Exception as exc:
            logger.error("Failed to parse PDF %s: %s", path, exc)
            raise RuntimeError(f"PDF parsing failed: {exc}") from exc

        metadata: Dict[str, Any] = {
            "source_path": str(path),
            "doc_type": "pdf",
            "doc_hash": doc_hash,
            "parser": "pymupdf4llm",
        }
        title = self._extract_title(text_content)
        if title:
            metadata["title"] = title

        return Document(id=doc_id, text=text_content, metadata=metadata)

    @staticmethod
    def _has_meaningful_text(markdown: str) -> bool:
        without_images = _IMAGE_LINK.sub("", markdown)
        return any(character.isalnum() for character in without_images)

    def _prepare_image_dir(self, image_dir: Path) -> None:
        """Start each conversion with an empty, document-scoped image folder."""
        root = self.image_storage_dir.resolve()
        target = image_dir.resolve()
        if target.parent != root:
            raise ValueError(f"Unsafe PDF image directory: {target}")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _make_image_links_relative(markdown: str, image_dir: Path) -> str:
        """Rewrite only images emitted into *image_dir* to repo-relative links."""
        image_root = image_dir.resolve()

        def replace(match: re.Match[str]) -> str:
            alt = match.group(1)
            raw_target = match.group(2).strip()
            wrapped = raw_target.startswith("<") and raw_target.endswith(">")
            target_text = raw_target[1:-1] if wrapped else raw_target
            if target_text.lower().startswith(_REMOTE_PREFIXES):
                return match.group(0)

            candidate = Path(target_text)
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (REPO_ROOT / candidate).resolve()
            )
            try:
                inside = resolved.relative_to(image_root)
                repo_path = (image_root.relative_to(REPO_ROOT.resolve()) / inside).as_posix()
            except ValueError:
                return match.group(0)
            return f"![{alt}]({repo_path})"

        normalized = _IMAGE_LINK.sub(replace, markdown)
        for match in _IMAGE_LINK.finditer(normalized):
            raw_target = match.group(2).strip().strip("<>")
            if raw_target.lower().startswith(_REMOTE_PREFIXES):
                continue
            candidate = Path(raw_target)
            if not candidate.is_absolute():
                continue
            try:
                candidate.resolve().relative_to(image_root)
            except ValueError:
                continue
            raise RuntimeError(f"Generated image path was not normalized: {raw_target}")
        return normalized
