from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, DECIMAL, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, utcnow


class DeliveryConfig(Base):
    __tablename__ = "delivery_config"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    fee_amount: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        {"sqlite_autoincrement": True},
    )