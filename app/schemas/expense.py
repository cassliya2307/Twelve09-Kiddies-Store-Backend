from pydantic import BaseModel, Field, validator
from decimal import Decimal
from datetime import datetime


class ExpenseCreate(BaseModel):
    description: str = Field(..., min_length=2, description="Expense description")
    amount: Decimal = Field(..., gt=0, decimal_places=2, description="Expense amount, must be > 0")
    category: str = Field(..., min_length=1, description="Expense category")

    @validator("description")
    def description_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Description cannot be whitespace-only")
        return v

    @validator("category")
    def category_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Category cannot be whitespace-only")
        return v

    model_config = {
        "from_attributes": True,
    }


class ExpenseRead(BaseModel):
    id: int
    recorded_by: int
    description: str
    amount: Decimal
    category: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {
        "from_attributes": True,
    }