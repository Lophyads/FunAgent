"""PDF Loader implementation using MarkItDown.

This module implements PDF parsing with image extraction support,
converting PDFs to standardized Markdown format with image placeholders.

Features:
- Text extraction and Markdown conversion via MarkItDown
- Image extraction and storage
- Image placeholder insertion with metadata tracking
- Graceful degradation if image extraction fails
"""

from __future__ import annotations

import io
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

try:
    from markitdown import MarkItDown
    MARKITDOWN_AVAILABLE = True
except ImportError:
    MARKITDOWN_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

from src.core.types import Document
from src.libs.loader.base_loader import BaseLoader

logger = logging.getLogger(__name__)


class _MissingFontBBoxFilter(logging.Filter):
    """Hide the known missing FontBBox warning from pdfminer.six."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.levelno == logging.WARNING
            and record.getMessage()
            == "Could not get FontBBox from font descriptor because None cannot be parsed as 4 floats"
        )


logging.getLogger("pdfminer.pdffont").addFilter(_MissingFontBBoxFilter())


@dataclass(frozen=True)
class _TextAnchor:
    bbox: tuple[float, float, float, float]
    start: int
    end: int


class PdfLoader(BaseLoader):
    """PDF Loader using MarkItDown for text extraction and Markdown conversion.
    
    This loader:
    1. Extracts text from PDF and converts to Markdown
    2. Extracts images and saves to data/images/{doc_hash}/
    3. Inserts image placeholders in the format [IMAGE: {image_id}]
    4. Records image metadata in Document.metadata.images
    
    Configuration:
        extract_images: Enable/disable image extraction (default: True)
        image_storage_dir: Base directory for image storage (default: data/images)
    
    Graceful Degradation:
        If image extraction fails, logs warning and continues with text-only parsing.
    """
    
    def __init__(
        self,
        extract_images: bool = True,
        image_storage_dir: str | Path = "data/images"
    ):
        """Initialize PDF Loader.
        
        Args:
            extract_images: Whether to extract images from PDFs.
            image_storage_dir: Base directory for storing extracted images.
        """
        if not MARKITDOWN_AVAILABLE:
            raise ImportError(
                "MarkItDown is required for PdfLoader. "
                "Install with: pip install markitdown"
            )
        
        self.extract_images = extract_images
        self.image_storage_dir = Path(image_storage_dir)
        self._markitdown = MarkItDown()
    
    def load(self, file_path: str | Path) -> Document:
        """Load and parse a PDF file.
        
        Args:
            file_path: Path to the PDF file.
            
        Returns:
            Document with Markdown text and metadata.
            
        Raises:
            FileNotFoundError: If the PDF file doesn't exist.
            ValueError: If the file is not a valid PDF.
            RuntimeError: If parsing fails critically.
        """
        # Validate file
        path = self._validate_file(file_path)
        if path.suffix.lower() != '.pdf':
            raise ValueError(f"File is not a PDF: {path}")
        
        # Compute document hash for unique ID and image directory
        doc_hash = self._compute_file_hash(path)
        doc_id = f"doc_{doc_hash[:16]}"
        
        # Parse PDF with MarkItDown
        try:
            result = self._markitdown.convert(str(path))
            text_content = result.text_content if hasattr(result, 'text_content') else str(result)
        except Exception as e:
            logger.error(f"Failed to parse PDF {path}: {e}")
            raise RuntimeError(f"PDF parsing failed: {e}") from e
        
        # Initialize metadata
        metadata: Dict[str, Any] = {
            "source_path": str(path),
            "doc_type": "pdf",
            "doc_hash": doc_hash,
        }
        
        # Extract title from first heading if available
        title = self._extract_title(text_content)
        if title:
            metadata["title"] = title
        
        # Handle image extraction (with graceful degradation)
        logger.debug(f"text_content111={text_content}")
        if self.extract_images:
            try:
                text_content, images_metadata = self._extract_and_process_images(
                    path, text_content, doc_hash
                )
                logger.debug(f"text_content222={text_content}")
                if images_metadata:
                    metadata["images"] = images_metadata
                logger.debug(f"images_metadata={images_metadata}")
            except Exception as e:
                logger.warning(
                    f"Image extraction failed for {path}, continuing with text-only: {e}"
                )
        
        return Document(
            id=doc_id,
            text=text_content,
            metadata=metadata
        )
    
    def _extract_and_process_images(
        self,
        pdf_path: Path,
        text_content: str,
        doc_hash: str
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Extract images from PDF and insert placeholders.
        
        Match PDF text coordinates to each page's Markdown, then insert each
        displayed image beside its nearest text line. Keeping page conversion
        separate prevents repeated headings from matching a different page.
        
        Args:
            pdf_path: Path to PDF file.
            text_content: Extracted text content.
            doc_hash: Document hash for image directory.
            
        Returns:
            Tuple of (modified_text, images_metadata_list)
        """
        if not self.extract_images:
            logger.debug(f"Image extraction disabled for {pdf_path}")
            return text_content, []
        
        if not PYMUPDF_AVAILABLE:
            logger.warning(f"PyMuPDF not available, skipping image extraction for {pdf_path}")
            return text_content, []
        
        try:
            with fitz.open(pdf_path) as doc:
                image_pages = {page.number for page in doc if page.get_image_info()}
                if not image_pages:
                    return text_content, []

                image_dir = self.image_storage_dir / doc_hash
                image_dir.mkdir(parents=True, exist_ok=True)
                parts: List[str] = []
                images_metadata: List[Dict[str, Any]] = []
                text_offset = 0

                for page_num, page in enumerate(doc):
                    # MarkItDown's full-document output does not reliably retain
                    # page boundaries, particularly for tables and blank pages.
                    page_text = (
                        text_content if len(doc) == 1
                        else self._convert_page_to_markdown(doc, page_num)
                    )
                    placements = []
                    if page_num in image_pages:
                        layout = page.get_text("dict", sort=True)
                        anchors = self._match_text_anchors(page_text, layout["blocks"])
                        image_blocks = sorted(
                            (block for block in layout["blocks"] if block["type"] == 1),
                            key=lambda block: (block["bbox"][1], block["bbox"][0]),
                        )
                        for img_index, block in enumerate(image_blocks):
                            try:
                                image_id = self._generate_image_id(doc_hash, page_num + 1, img_index + 1)
                                image_path = image_dir / f"{image_id}.{block['ext']}"
                                image_path.write_bytes(block["image"])
                                bbox = tuple(block["bbox"])
                                insert_position = self._find_image_insert_position(bbox, anchors)
                                placement = "text_anchor"
                                if insert_position is None:
                                    insert_position = len(page_text)
                                    placement = "page_end" if page_text.strip() else "image_only_page"
                                    if page_text.strip():
                                        logger.warning(
                                            "Could not align image %s with text on page %s; "
                                            "placing it at the end of that page",
                                            image_id, page_num + 1,
                                        )
                                try:
                                    stored_path = image_path.relative_to(Path.cwd())
                                except ValueError:
                                    stored_path = image_path.absolute()
                                metadata = {
                                    "id": image_id,
                                    "path": str(stored_path),
                                    "page": page_num + 1,
                                    "position": {
                                        "width": block["width"],
                                        "height": block["height"],
                                        "bbox": list(bbox),
                                        "page": page_num + 1,
                                        "index": img_index,
                                        "placement": placement,
                                    },
                                }
                                placements.append((insert_position, metadata))
                            except Exception as e:
                                logger.warning(
                                    "Failed to extract image %s from page %s: %s",
                                    img_index, page_num + 1, e,
                                )

                    page_text, page_images = self._insert_image_placeholders(page_text, placements)
                    if page_num:
                        parts.append("\n\n")
                        text_offset += 2
                    for image in page_images:
                        image["text_offset"] += text_offset
                    images_metadata.extend(page_images)
                    parts.append(page_text)
                    text_offset += len(page_text)

                if not images_metadata:
                    return text_content, []
                logger.info("Extracted %s image occurrences from %s", len(images_metadata), pdf_path)
                return "".join(parts), images_metadata

        except Exception as e:
            logger.warning(f"Image extraction failed for {pdf_path}: {e}")
            # Graceful degradation: return original text without images
            return text_content, []

    def _convert_page_to_markdown(self, doc: Any, page_num: int) -> str:
        """Use the existing converter on one page without temporary PDF files."""
        with fitz.open() as single_page:
            single_page.insert_pdf(doc, from_page=page_num, to_page=page_num)
            result = self._markitdown.convert_stream(io.BytesIO(single_page.tobytes()))
        return result.text_content.rstrip()

    @staticmethod
    def _normalize_text(text: str) -> tuple[str, List[int]]:
        """Ignore spacing/Markdown syntax while retaining original offsets."""
        chars = []
        offsets = []
        for index, char in enumerate(text):
            for normalized in unicodedata.normalize("NFKD", char).casefold():
                if normalized.isalnum():
                    chars.append(normalized)
                    offsets.append(index)
        return "".join(chars), offsets

    @classmethod
    def _match_text_anchors(cls, text: str, blocks: List[Dict[str, Any]]) -> List[_TextAnchor]:
        """Map PDF lines to Markdown, tolerating different whitespace/ligatures."""
        normalized, offsets = cls._normalize_text(text)
        anchors = []
        used = []
        cursor = 0
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                line_text = "".join(span["text"] for span in line["spans"])
                needle, _ = cls._normalize_text(line_text)
                if not needle:
                    continue
                match = normalized.find(needle, cursor)
                # Converters can choose different orders for columns. Allow an
                # earlier unused match, but never reuse the same text occurrence.
                if match < 0:
                    match = normalized.find(needle)
                while match >= 0 and any(
                    match < end and match + len(needle) > start for start, end in used
                ):
                    match = normalized.find(needle, match + 1)
                if match < 0:
                    continue
                end = match + len(needle)
                start_offset = offsets[match]
                end_offset = offsets[end - 1] + 1
                start_offset, end_offset = cls._markdown_line_bounds(text, start_offset, end_offset)
                anchors.append(_TextAnchor(tuple(line["bbox"]), start_offset, end_offset))
                used.append((match, end))
                cursor = max(cursor, end)
        return anchors

    @staticmethod
    def _markdown_line_bounds(text: str, start: int, end: int) -> tuple[int, int]:
        """Insert between Markdown lines and keep table rows together."""
        start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        end = len(text) if line_end < 0 else line_end
        if text[start:end].lstrip().startswith("|"):
            while start > 0:
                previous = text.rfind("\n", 0, start - 1) + 1
                if not text[previous:start - 1].lstrip().startswith("|"):
                    break
                start = previous
            while end < len(text):
                following_end = text.find("\n", end + 1)
                following_end = len(text) if following_end < 0 else following_end
                if not text[end + 1:following_end].lstrip().startswith("|"):
                    break
                end = following_end
        return start, end

    @staticmethod
    def _find_image_insert_position(
        bbox: tuple[float, float, float, float], anchors: List[_TextAnchor],
    ) -> int | None:
        """Find the nearest line above/below the image in the same column."""
        if not anchors:
            return None
        candidates = []
        for anchor in anchors:
            x0, y0, x1, y1 = anchor.bbox
            overlap = min(x1, bbox[2]) - max(x0, bbox[0])
            if overlap <= 0:
                continue
            if y0 >= bbox[3] - 2:
                candidates.append((max(0, y0 - bbox[3]), 0, anchor.start))
            elif y1 <= bbox[1] + 2:
                candidates.append((max(0, bbox[1] - y1), 1, anchor.end))
        if candidates:
            return min(candidates)[2]
        # Images beside text or behind an OCR text layer have no vertical gap.
        nearest = min(
            anchors,
            key=lambda anchor: (
                abs(anchor.bbox[1] - bbox[1]),
                abs(anchor.bbox[0] - bbox[0]),
            ),
        )
        return nearest.start if nearest.bbox[1] >= bbox[1] else nearest.end

    @staticmethod
    def _insert_image_placeholders(
        text: str, placements: List[tuple[int, Dict[str, Any]]],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Insert in one pass so every recorded offset refers to the final text."""
        parts = []
        images = []
        cursor = 0
        length = 0
        for offset, image in sorted(
            placements,
            key=lambda item: (item[0], item[1]["position"]["bbox"][1], item[1]["position"]["bbox"][0]),
        ):
            segment = text[cursor:offset]
            placeholder = f"[IMAGE: {image['id']}]"
            insertion = f"\n\n{placeholder}\n\n"
            parts.extend((segment, insertion))
            image["text_offset"] = length + len(segment) + 2
            image["text_length"] = len(placeholder)
            images.append(image)
            length += len(segment) + len(insertion)
            cursor = offset
        parts.append(text[cursor:])
        return "".join(parts), images

    @staticmethod
    def _generate_image_id(doc_hash: str, page: int, sequence: int) -> str:
        """Generate unique image ID.
        
        Args:
            doc_hash: Document hash.
            page: Page number (1-based).
            sequence: Image occurrence sequence on page (1-based).
            
        Returns:
            Unique image ID string.
        """
        return f"{doc_hash[:8]}_{page}_{sequence}"
