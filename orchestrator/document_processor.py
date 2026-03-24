"""
Document text extraction and file management for the Document Vault.

Extracts searchable text from PDFs (native + OCR fallback), images (OCR),
and plain text files.
"""

import logging
import os
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("DOCUMENT_VAULT_PATH", "/mnt/documents")
MAX_SIZE_MB = int(os.environ.get("DOCUMENT_MAX_SIZE_MB", "100"))
VALID_CATEGORIES = {"auto", "financial", "medical", "legal", "insurance", "personal", "housing", "other"}
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp", ".txt", ".md"}

# Minimum chars from PDF text extraction before falling back to OCR
_PDF_OCR_THRESHOLD = 50


def extract_text(file_path: str) -> str:
    """Extract text from a document file. Returns empty string on failure."""
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf":
            return _extract_from_pdf(file_path)
        elif ext in {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}:
            return _extract_from_image(file_path)
        elif ext in {".txt", ".md"}:
            return Path(file_path).read_text(errors="replace")
        else:
            logger.warning(f"[DOCVAULT] Unsupported extension for text extraction: {ext}")
            return ""
    except Exception as e:
        logger.warning(f"[DOCVAULT] Text extraction failed for {file_path}: {e}")
        return ""


def _extract_from_pdf(file_path: str) -> str:
    """Extract text from PDF. Falls back to OCR for scanned pages."""
    import fitz  # PyMuPDF

    doc = fitz.open(file_path)
    text_parts = []
    for page in doc:
        text_parts.append(page.get_text())
    doc.close()

    text = "\n".join(text_parts).strip()

    # If very little text extracted, it's likely a scanned PDF — OCR each page
    if len(text) < _PDF_OCR_THRESHOLD:
        logger.info(f"[DOCVAULT] PDF has sparse text ({len(text)} chars), falling back to OCR")
        text = _ocr_pdf_pages(file_path)

    return text


def _ocr_pdf_pages(file_path: str) -> str:
    """Render PDF pages to images and OCR them."""
    import io

    import fitz
    import pytesseract
    from PIL import Image

    doc = fitz.open(file_path)
    text_parts = []
    for page_num, page in enumerate(doc):
        # Render at 300 DPI for good OCR quality
        mat = fitz.Matrix(300 / 72, 300 / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        page_text = pytesseract.image_to_string(img)
        if page_text.strip():
            text_parts.append(page_text)
        logger.debug(f"[DOCVAULT] OCR page {page_num + 1}: {len(page_text)} chars")
    doc.close()
    return "\n".join(text_parts).strip()


def _extract_from_image(file_path: str) -> str:
    """OCR an image file."""
    import pytesseract
    from PIL import Image

    img = Image.open(file_path)
    # Convert to RGB to ensure pytesseract compatibility (handles RGBA, palette, etc.)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return pytesseract.image_to_string(img).strip()


def save_uploaded_file(file_bytes: bytes, original_name: str, category: str) -> str:
    """Save file to vault, return relative path within VAULT_PATH."""
    ext = Path(original_name).suffix.lower() or ".bin"
    safe_name = re.sub(r"[^\w\-.]", "_", Path(original_name).stem)[:60]
    short_id = uuid.uuid4().hex[:8]
    today = date.today().isoformat()

    cat_dir = Path(VAULT_PATH) / category
    cat_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{today}_{safe_name}_{short_id}{ext}"
    full_path = cat_dir / filename
    full_path.write_bytes(file_bytes)

    # Return path relative to VAULT_PATH
    return f"{category}/{filename}"


def _safe_vault_path(relative_path: str) -> Path:
    """Resolve a vault-relative path and verify it stays inside the vault (prevents path traversal)."""
    full = (Path(VAULT_PATH) / relative_path).resolve()
    vault_root = Path(VAULT_PATH).resolve()
    if not str(full).startswith(str(vault_root)):
        raise ValueError(f"Path traversal detected: {relative_path}")
    return full


def get_full_path(relative_path: str) -> str:
    """Get the absolute path for a vault-relative path."""
    return str(_safe_vault_path(relative_path))


def delete_file(relative_path: str) -> bool:
    """Delete a file from the vault."""
    full = _safe_vault_path(relative_path)
    if full.exists():
        full.unlink()
        return True
    return False


def validate_upload(file_bytes: bytes, filename: str, category: str) -> Optional[str]:
    """Validate an upload. Returns error message or None if valid."""
    max_bytes = MAX_SIZE_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        return f"File too large (max {MAX_SIZE_MB} MB)"

    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return f"Unsupported file type: {ext}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"

    if category not in VALID_CATEGORIES:
        return f"Invalid category: {category}. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"

    return None
