from pydantic import BaseModel, Field


class AddressBase(BaseModel):
    recipient_name: str = Field(..., min_length=2, max_length=150)
    phone_number: str = Field(..., min_length=7, max_length=30)
    address_line: str = Field(..., min_length=5, max_length=255)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    additional_directions: str | None = Field(default=None, max_length=1000)
    is_default: bool = False


class AddressCreate(AddressBase):
    pass


class AddressUpdate(BaseModel):
    recipient_name: str | None = Field(default=None, min_length=2, max_length=150)
    phone_number: str | None = Field(default=None, min_length=7, max_length=30)
    address_line: str | None = Field(default=None, min_length=5, max_length=255)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    state: str | None = Field(default=None, min_length=2, max_length=100)
    additional_directions: str | None = Field(default=None, max_length=1000)
    is_default: bool | None = None


class AddressRead(AddressBase):
    id: int
    user_id: int

    class Config:
        from_attributes = True


AddressResponse = AddressRead
