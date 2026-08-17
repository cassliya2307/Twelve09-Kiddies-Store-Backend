from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_serializer, ConfigDict


class CartItemInput(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0, le=1000)


class CartItemDetails(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    product_id: int
    quantity: int
    product_name: str
    unit_price: Decimal
    subtotal: Decimal

    @field_serializer("unit_price", "subtotal")
    def serialize_decimal(self, value: Decimal, _info):
        return float(value)


class CheckoutRequest(BaseModel):
    items: list[CartItemInput]
    fulfillment_method: str = Field(..., min_length=1, max_length=50)
    address_id: int | None = None
    delivery_recipient_name: str | None = Field(default=None, max_length=150)
    delivery_phone_number: str | None = Field(default=None, max_length=30)
    delivery_address_line: str | None = Field(default=None, max_length=255)
    delivery_city: str | None = Field(default=None, max_length=100)
    delivery_state: str | None = Field(default=None, max_length=100)
    delivery_additional_directions: str | None = Field(default=None, max_length=1000)


class CheckoutSummary(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[CartItemDetails]
    subtotal: Decimal
    delivery_fee: Decimal
    total_amount: Decimal
    fulfillment_method: str
    delivery_address: dict | None = None

    @field_serializer("subtotal", "delivery_fee", "total_amount")
    def serialize_decimal(self, value: Decimal, _info):
        return float(value)


class CheckoutValidationResponse(BaseModel):
    valid: bool
    message: str
    summary: CheckoutSummary | None = None
    errors: list[str] | None = None
