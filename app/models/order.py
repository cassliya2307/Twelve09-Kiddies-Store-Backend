from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import DECIMAL, DateTime, Enum as SQLAlchemyEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, utcnow


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


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    address_id: Mapped[int | None] = mapped_column(ForeignKey("addresses.id"), nullable=True, index=True)
    total_amount: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(SQLAlchemyEnum(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    fulfillment_method: Mapped[FulfillmentMethod] = mapped_column(SQLAlchemyEnum(FulfillmentMethod), nullable=False, default=FulfillmentMethod.STORE_PICKUP)
    delivery_fee: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False, default=0)
    delivery_recipient_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    delivery_phone_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    delivery_address_line: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_additional_directions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="orders")
    address: Mapped["Address"] = relationship(back_populates="orders")
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="order_items")
    product: Mapped["Product"] = relationship(back_populates="order_items")
