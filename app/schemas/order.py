from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    PROCESSING = "PROCESSING"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class FulfillmentMethod(str, Enum):
    STORE_DELIVERY = "STORE_DELIVERY"
    CUSTOMER_DISPATCH = "CUSTOMER_DISPATCH"
    STORE_PICKUP = "STORE_PICKUP"


class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0, le=1000)


class OrderItemRead(BaseModel):
    id: int
    order_id: int
    product_id: int
    quantity: int
    unit_price: float
    subtotal: float
    image_url: str | None = None

    class Config:
        from_attributes = True


class OrderCreate(BaseModel):
    fulfillment_method: FulfillmentMethod = FulfillmentMethod.STORE_PICKUP
    address_id: int | None = None
    delivery_recipient_name: str | None = Field(default=None, max_length=150)
    delivery_phone_number: str | None = Field(default=None, max_length=30)
    delivery_address_line: str | None = Field(default=None, max_length=255)
    delivery_city: str | None = Field(default=None, max_length=100)
    delivery_state: str | None = Field(default=None, max_length=100)
    delivery_additional_directions: str | None = Field(default=None, max_length=1000)
    items: list[OrderItemCreate]


class OrderRead(BaseModel):
    id: int
    user_id: int
    address_id: int | None = None
    total_amount: float
    subtotal: float = 0.00
    status: OrderStatus = OrderStatus.PENDING
    fulfillment_method: FulfillmentMethod
    delivery_fee: float = 0.00
    delivery_recipient_name: str | None = None
    delivery_phone_number: str | None = None
    delivery_address_line: str | None = None
    delivery_city: str | None = None
    delivery_state: str | None = None
    delivery_additional_directions: str | None = None
    order_items: list[OrderItemRead] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class OrderStatusUpdate(BaseModel):
    status: OrderStatus
