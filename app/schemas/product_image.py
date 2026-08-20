from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ProductImageUploadResponse(BaseModel):
    """Response returned after a successful image upload."""

    image_url: str = Field(
        ...,
        description="URL path to the uploaded image, e.g. /static/uploads/uuid.jpg",
    )
    filename: str = Field(
        ...,
        description="The server-generated filename (UUID + extension)",
    )
    size_bytes: int = Field(
...,
        description="Size of the uploaded file in bytes",
    )


class ProductImageDeleteResponse(BaseModel):
    """Response returned after attempting to delete a product image."""

    deleted: bool = Field(
        ...,
        description="Whether the image file was successfully deleted",
    )
    image_url_before: Optional[str] = Field(
        default=None,
        description="The image URL that was removed, if any",
    )


class ProductImageInfo(BaseModel):
    """Current image information for a product."""

    has_image: bool = Field(
        ...,
        description="Whether the product has an uploaded image",
    )
    image_url: Optional[str] = Field(
        default=None,
        description="URL path to the image, or None",
    )
    filename: Optional[str] = Field(
        default=None,
        description="The server-generated filename, or None",
    )