from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, utcnow


class HomepageCategoryConfiguration(Base):
    __tablename__ = "homepage_category_configurations"
    __table_args__ = (
        CheckConstraint("display_order >= 0 AND display_order <= 4", name="ck_homepage_category_configurations_display_order_range"),
        UniqueConstraint("category_id", name="uq_homepage_category_configurations_category_id"),
        UniqueConstraint("display_order", name="uq_homepage_category_configurations_display_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    icon_key: Mapped[str] = mapped_column(String(40), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    category: Mapped["Category"] = relationship()
