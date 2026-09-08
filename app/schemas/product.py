from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class CategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=1000)


class CategoryCreate(CategoryBase):
    pass


class CategoryRead(CategoryBase):
    id: int
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class ProductBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal = Field(..., gt=0, decimal_places=2)
    cost_price: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    category_id: int
    image_url: str | None = Field(default=None, max_length=500)


class ProductCreate(ProductBase):
    stock_quantity: int = Field(default=0, ge=0)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    category_id: int | None = None
    image_url: str | None = Field(default=None, max_length=500)
    price: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    cost_price: Decimal | None = Field(default=None, ge=0, decimal_places=2)


class StockUpdate(BaseModel):
    quantity: int = Field(..., ge=0)
    reason: str | None = Field(default=None, max_length=500)


class ProductRead(ProductBase):
    id: int
    stock_quantity: int = 0
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    category: CategoryRead | None = None

    class Config:
        from_attributes = True


class ProductListRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    price: Decimal
    cost_price: Decimal | None = None
    image_url: str | None = None
    is_active: bool = True
    category_id: int
    created_at: datetime | None = None

    class Config:
        from_attributes = True
