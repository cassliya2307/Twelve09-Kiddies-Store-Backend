from enum import Enum

from pydantic import BaseModel, EmailStr, Field


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


class UserBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    email: EmailStr
    role: UserRole = UserRole.CUSTOMER


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=255)


class UserRead(UserBase):
    id: int
    is_active: bool = True
    permissions: list[Permission] = Field(default_factory=list)

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    email: EmailStr | None = None
    role: UserRole | None = None
    is_active: bool | None = None
    permissions: list[Permission] | None = None
