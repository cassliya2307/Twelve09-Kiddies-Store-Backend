from typing import Literal

from pydantic import BaseModel, Field, field_validator


HomepageCategoryIconKey = Literal[
    "backpack",
    "blocks",
    "gift",
    "shirt",
    "shopping-bag",
]


class HomepageCategorySelection(BaseModel):
    category_id: int = Field(gt=0)
    label: str = Field(min_length=1, max_length=100)
    icon_key: HomepageCategoryIconKey
    display_order: int = Field(ge=0, le=4)

    @field_validator("label", mode="before")
    def trim_label(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class HomepageCategorySelectionRequest(BaseModel):
    items: list[HomepageCategorySelection]

    @field_validator("items")
    def validate_selections(cls, items: list[HomepageCategorySelection]) -> list[HomepageCategorySelection]:
        if len(items) > 5:
            raise ValueError("At most five homepage categories may be selected")

        category_ids = [item.category_id for item in items]
        if len(category_ids) != len(set(category_ids)):
            raise ValueError("Category IDs must be unique")

        display_orders = [item.display_order for item in items]
        if len(display_orders) != len(set(display_orders)):
            raise ValueError("Display orders must be unique")
        return items


class HomepageCategoryAdminEntry(HomepageCategorySelection):
    id: int
    category_name: str
    category_is_active: bool


class HomepageCategoryPublicEntry(BaseModel):
    category_id: int
    category_name: str
    label: str
    icon_key: HomepageCategoryIconKey
    display_order: int
