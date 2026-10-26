from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import DECIMAL, DateTime, Enum as SQLAlchemyEnum, ForeignKey, String, JSON, Integer, Text, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, utcnow


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(SQLAlchemyEnum(PaymentStatus), nullable=False, default=PaymentStatus.PENDING)
    payment_method: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    transaction_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    # Receipt fields for bank-transfer workflow
    # Use LargeBinary which maps to BYTEA on PostgreSQL and BLOB on SQLite
    receipt_data: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    receipt_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    receipt_size: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    receipt_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_reviewed_by: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    receipt_rejection_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)

    order: Mapped["Order"] = relationship(back_populates="payments")

    if TYPE_CHECKING:
        from app.models.order import Order
