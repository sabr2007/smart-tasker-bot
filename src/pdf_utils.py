# src/pdf_utils.py
"""
PDF text extraction utilities.

Uses pdfplumber for text extraction from text-based PDFs.
Does NOT support scanned PDFs (image-only).
"""

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Limits to prevent token overflow
MAX_PAGES = 1
MAX_CHARS = 8000  # ~2000 tokens


def extract_pdf_text(file_bytes: bytes) -> Optional[str]:
    """
    Extract text from PDF file.
    
    Returns:
        Extracted text (first 3 pages, max 8000 chars) or None if no text layer.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber not installed")
        return None
    
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text_parts = []
            
            for i, page in enumerate(pdf.pages[:MAX_PAGES]):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(page_text.strip())
            
            if not text_parts:
                # No text layer - probably a scanned PDF
                return None
            
            full_text = "\n\n".join(text_parts)
            
            # Truncate if too long
            if len(full_text) > MAX_CHARS:
                full_text = full_text[:MAX_CHARS] + "\n...[текст обрезан]"
            
            return full_text
            
    except Exception as e:
        logger.exception("PDF extraction error: %s", e)
        return None
