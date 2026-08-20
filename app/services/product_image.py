from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from io import BytesIO
from typing import Set
from urllib.parse import urlparse

import cloudinary
import cloudinary.uploader
from PIL import Image as PILImage

from app.core.database import settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_SIZE = Decimal(settings.PRODUCT_IMAGE_MAX_SIZE)

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)


def _get_extension(filename: str) -> str:
    """Get the lowercase file extension from a filename."""
    from pathlib import Path
    return Path(filename).suffix.lower()


def _is_allowed_extension(ext: str) -> bool:
    """Check if the extension is in the allowed list."""
    return ext in ALLOWED_EXTENSIONS


def _generate_public_id(product_id: int) -> str:
    """Generate a Cloudinary public ID for a product image."""
    return f"twelve09/products/{product_id}/{uuid.uuid4()}"


def is_our_cloudinary_url(image_url: str) -> bool:
    """Check if the URL belongs to our configured Cloudinary cloud."""
    if not image_url or not settings.CLOUDINARY_CLOUD_NAME:
        return False
    try:
        parsed = urlparse(image_url)
        # Check if it's a Cloudinary URL and belongs to our cloud
        if "res.cloudinary.com" not in parsed.netloc:
            return False
        # Cloudinary URLs are typically: https://res.cloudinary.com/{cloud_name}/image/upload/...
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            return False
        return path_parts[0] == settings.CLOUDINARY_CLOUD_NAME
    except Exception:
        return False


def extract_our_public_id(image_url: str) -> str | None:
    """Extract the Cloudinary public ID from our managed URL."""
    if not is_our_cloudinary_url(image_url):
        return None
    try:
        parsed = urlparse(image_url)
        path = parsed.path.strip("/")
        # Cloudinary path format: {cloud_name}/image/upload/v1234567890/folder/name
        parts = path.split("/")
        if len(parts) < 4:
            return None
        # Skip cloud_name, resource_type (image), action (upload)
        # Then optional version (v123...), then the actual public_id
        remaining = parts[3:]
        # Skip version segment if present
        if remaining and remaining[0].startswith("v") and remaining[0][1:].isdigit():
            remaining = remaining[1:]
        # Join remaining parts and remove file extension from the last part
        public_id_parts = remaining
        if public_id_parts:
            # Remove extension from last part
            last_part = public_id_parts[-1]
            if "." in last_part:
                public_id_parts[-1] = last_part.rsplit(".", 1)[0]
            return "/".join(public_id_parts)
        return None
    except Exception:
        return None


def _validate_and_prepare_upload(file_content: bytes, original_filename: str) -> tuple[bool, str | None, str | None]:
    """
    Validate file content and return (success, error_message, extension).
    On success: returns (True, None, extension)
    On failure: returns (False, error_message, None)
    """
    # 1. Check extension
    ext = _get_extension(original_filename)
    if not _is_allowed_extension(ext):
        return False, f"Invalid file extension '{ext}'. Allowed: .jpg, .jpeg, .png, .webp, .gif", None

    # 2. Enforce file size limit
    file_size = len(file_content)
    if file_size > int(MAX_SIZE):
        return False, f"File too large: {file_size / 1024 / 1024:.2f} MB. Maximum: {int(MAX_SIZE / 1024 / 1024)} MB", None

    # 3. Validate actual image content with Pillow
    try:
        img = PILImage.open(BytesIO(file_content))
        img.load()
        fmt = (img.format or "").lower()
        if fmt not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            pass
    except Exception as e:
        return False, f"Invalid image file: {e}", None

    return True, None, ext


def upload_to_cloudinary(file_content: bytes, product_id: int, extension: str) -> tuple[bool, str | None, str | None, int | None]:
    """
    Upload image to Cloudinary.
    Returns (success, secure_url, public_id, size_bytes) or (False, error_message, None, None).
    """
    if not settings.CLOUDINARY_CLOUD_NAME or not settings.CLOUDINARY_API_KEY or not settings.CLOUDINARY_API_SECRET:
        return False, "Cloudinary not configured. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET.", None, None

    public_id = _generate_public_id(settings.CLOUDINARY_CLOUD_NAME)

    try:
        result = cloudinary.uploader.upload(
            file_content,
            public_id=public_id,
            resource_type="image",
            folder=f"twelve09/products/{product_id}",
            overwrite=False,
        )
        secure_url = result.get("secure_url")
        public_id_result = result.get("public_id")
        size_bytes = result.get("bytes")
        if not secure_url or not public_id_result:
            return False, "Cloudinary upload succeeded but response missing secure_url or public_id", None, None
        return True, secure_url, public_id_result, size_bytes
    except Exception as e:
        logger.error("Cloudinary upload failed: %s", e)
        return False, f"Cloudinary upload failed: {e}", None, None


def destroy_cloudinary_image(public_id: str) -> bool:
    """Destroy a Cloudinary image by public_id. Returns True if destroyed, False otherwise."""
    if not settings.CLOUDINARY_CLOUD_NAME or not settings.CLOUDINARY_API_KEY or not settings.CLOUDINARY_API_SECRET:
        return False
    try:
        result = cloudinary.uploader.destroy(public_id, resource_type="image")
        return result.get("result") == "ok"
    except Exception as e:
        logger.error("Cloudinary destroy failed for %s: %s", public_id, e)
        return False