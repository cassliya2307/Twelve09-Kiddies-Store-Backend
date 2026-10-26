from __future__ import annotations

import logging
from decimal import Decimal
from io import BytesIO
from typing import Set, Optional, Tuple
from pathlib import Path

from PIL import Image as PILImage
from pypdf import PdfReader

from app.core.database import settings

logger = logging.getLogger(__name__)

# Allowed MIME types and extensions for receipts
ALLOWED_RECEIPT_MIME_TYPES: set[str] = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}

ALLOWED_RECEIPT_EXTENSIONS: set[str] = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}

# 5 MiB limit
MAX_RECEIPT_SIZE = Decimal(5 * 1024 * 1024)


# MIME type signatures for file signature validation
MIME_SIGNATURES: dict[str, list[bytes]] = {
    "application/pdf": [b"%PDF"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/webp": [b"RIFF", b"WEBP"],
}


def _get_extension(filename: str) -> str:
    """Get the lowercase file extension from a filename."""
    return Path(filename).suffix.lower()


def _is_allowed_extension(ext: str) -> bool:
    """Check if the extension is in the allowed list."""
    return ext in ALLOWED_RECEIPT_EXTENSIONS


def _is_allowed_mime_type(mime_type: str) -> bool:
    """Check if the MIME type is in the allowed list."""
    return mime_type in ALLOWED_RECEIPT_MIME_TYPES


def _validate_file_signature(file_content: bytes, mime_type: str) -> bool:
    """
    Validate file signature (magic bytes) matches the expected MIME type.
    """
    signatures = MIME_SIGNATURES.get(mime_type, [])
    return any(file_content.startswith(sig) for sig in signatures)


def validate_receipt_file(
    file_content: bytes,
    original_filename: str,
    client_mime_type: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """
    Validate receipt file content and return (success, error_message, detected_mime_type).
    On success: returns (True, None, detected_mime_type)
    On failure: returns (False, error_message, None)
    """
    # 1. Check extension
    ext = _get_extension(original_filename)
    if not _is_allowed_extension(ext):
        return False, f"Invalid file extension '{ext}'. Allowed: .pdf, .jpg, .jpeg, .png, .webp", None

    # 2. Enforce file size limit
    file_size = len(file_content)
    if file_size > int(MAX_RECEIPT_SIZE):
        return False, f"File too large: {file_size / 1024 / 1024:.2f} MB. Maximum: 5 MB", None

    # 3. Validate MIME type if provided by client
    if client_mime_type and not _is_allowed_mime_type(client_mime_type):
        return False, f"Unsupported MIME type '{client_mime_type}'. Allowed: application/pdf, image/jpeg, image/png, image/webp", None

    # 3. Validate file signature (magic bytes)
    detected_mime_type = None
    for mime_type, signatures in MIME_SIGNATURES.items():
        if any(file_content.startswith(sig) for sig in signatures):
            detected_mime_type = mime_type
            break

    if not detected_mime_type:
        return False, "Unable to detect file type from content. File may be corrupted or unsupported.", None

    if not _is_allowed_mime_type(detected_mime_type):
        return False, f"Detected file type '{detected_mime_type}' is not allowed. Allowed: application/pdf, image/jpeg, image/png, image/webp", None

    # 4. If client provided MIME type, ensure it matches detected type
    if client_mime_type and client_mime_type != detected_mime_type:
        return False, f"MIME type mismatch: client sent '{client_mime_type}', but file content indicates '{detected_mime_type}'", None

    # 4. Validate actual content based on type
    try:
        if detected_mime_type == "application/pdf":
            # Validate PDF structure
            reader = PdfReader(BytesIO(file_content))
            # Just accessing pages validates the PDF structure
            _ = len(reader.pages)
        else:
            # Validate image content with Pillow
            img = PILImage.open(BytesIO(file_content))
            img.load()
            fmt = (img.format or "").lower()
            # Normalize format to match our allowed types
            if fmt == "jpeg":
                fmt = "jpg"
            if f".{fmt}" not in ALLOWED_RECEIPT_EXTENSIONS:
                return False, f"Image format '{fmt}' is not allowed. Allowed: .pdf, .jpg, .jpeg, .png, .webp", None
    except Exception as e:
        return False, f"Invalid file content: {e}", None

    return True, None, detected_mime_type