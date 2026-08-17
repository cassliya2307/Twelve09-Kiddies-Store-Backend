from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, Boolean, DateTime, Enum as SQLAlchemyEnum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class UserRole(str, Enum):
    CUSTOMER = "CUSTOMER"
    STAFF = "STAFF"
    ADMIN = "ADMIN"


class Permission(str, Enum):
    MANAGE_PRODUCTS = "MANAGE_PRODUCTS"
    MANAGE_INVENTORY = "MANAGE_INVENTORY"
    MANAGE_ORDERS = "MANAGE_ORDERS"
    MANAGE_CUSTOMERS = "MANAGE_CUSTOMERS"
    MANAGE_DELIVERIES = "MANAGE_DELIVERIES"
    VIEW_REPORTS = "VIEW_REPORTS"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SQLAlchemyEnum(UserRole), nullable=False, default=UserRole.CUSTOMER)
    permissions: Mapped[list[Permission]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    addresses: Mapped[list["Address"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    expenses: Mapped[list["Expense"]] = relationship(back_populates="recorded_by_user")

    def has_permission(self, permission: Permission) -> bool:
        if self.role == UserRole.ADMIN:
            return True
        if self.role != UserRole.STAFF:
            return False
        return permission in (self.permissions or [])
