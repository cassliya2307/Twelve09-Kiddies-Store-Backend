from __future__ import annotations

from pydantic import Field, validator

from app.schemas.common import BaseSchema


class DeliveryFeeRead(BaseSchema):
    id: int
    fee_amount: float
    is_active: bool

    class Config:
        from_attributes = True


class DeliveryFeeUpdate(BaseSchema):
    fee_amount: float = Field(..., ge=0, description="Delivery fee amount in decimal currency")

    @validator("fee_amount")
    def fee_must_be_non_negative(cls, v):
        if v < 0:
            raise ValueError("Delivery fee must be non-negative")
        return v

    class Config:
        from_attributes = True