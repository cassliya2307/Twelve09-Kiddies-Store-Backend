from pydantic import BaseModel, Field
from decimal import Decimal
from datetime import datetime


class PaymentCreate(BaseModel):
    order_id: int = Field(..., gt=0, description="Order ID to associate payment with")
    payment_method: str = Field(
        ..., min_length=1, max_length=100, description="Payment method (e.g., STRIPE, PAYSTACK, CASH)"
    )
    transaction_reference: str | None = Field(
        default=None, max_length=255, description="External payment provider transaction reference"
    )


class PaymentRead(BaseModel):
    id: int
    order_id: int
    amount: Decimal
    status: str
    payment_method: str
    transaction_reference: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class PaymentStatusUpdate(BaseModel):
    status: str = Field(..., description="Payment status: PENDING, SUCCESS, FAILED, REFUNDED")