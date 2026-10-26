from pydantic import BaseModel, Field
from decimal import Decimal
from datetime import datetime
from typing import Optional


class PaymentCreate(BaseModel):
    order_id: int = Field(..., gt=0, description="Order ID to associate payment with")
    payment_method: str = Field(
        ..., min_length=1, max_length=100, description="Payment method (e.g., PAYSTACK, BANK_TRANSFER, CASH)"
    )
    transaction_reference: str | None = Field(
        default=None, max_length=255, description="External payment provider transaction reference"
    )
    provider: str | None = Field(default=None, max_length=50, description="Payment provider (e.g., paystack)")
    provider_reference: str | None = Field(default=None, max_length=255, description="Provider transaction reference")
    payment_metadata: Optional[dict] = Field(default=None, description="Additional payment metadata")


class PaymentRead(BaseModel):
    id: int
    order_id: int
    amount: Decimal
    status: str
    payment_method: str
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    transaction_reference: Optional[str] = None
    payment_metadata: Optional[dict] = None
    currency: Optional[str] = None
    paid_at: Optional[datetime] = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Receipt fields
    receipt_filename: Optional[str] = None
    receipt_mime_type: Optional[str] = None
    receipt_size: Optional[int] = None
    receipt_uploaded_at: Optional[datetime] = None
    receipt_reviewed_at: Optional[datetime] = None
    receipt_reviewed_by: Optional[int] = None
    receipt_rejection_reason: Optional[str] = None

    class Config:
        from_attributes = True


class PaymentStatusUpdate(BaseModel):
    status: str = Field(..., description="Payment status: PENDING, SUCCESS, FAILED, REFUNDED")


# Receipt upload/management schemas
class ReceiptUploadResponse(BaseModel):
    receipt_filename: str
    receipt_mime_type: str
    receipt_size: int
    receipt_uploaded_at: datetime


class ReceiptReviewRequest(BaseModel):
    rejection_reason: str | None = Field(
        default=None, description="Required when rejecting (max 1000 characters)"
    )


class ReceiptReviewResponse(BaseModel):
    payment_id: int
    status: str  # "APPROVED" | "REJECTED"
    reviewed_at: datetime
    reviewed_by: int
    rejection_reason: Optional[str] = None

    class Config:
        from_attributes = True