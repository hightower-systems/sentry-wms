"""Shipping request schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class ShippingAllocation(BaseModel):
    so_line_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100000)
    lot_number: Optional[str] = Field(None, max_length=50)
    serial_number: Optional[str] = Field(None, max_length=100)


class FulfillRequest(BaseModel):
    so_id: int = Field(..., gt=0)
    tracking_number: str = Field(..., min_length=1, max_length=255)
    carrier: str = Field(..., min_length=1, max_length=100)
    ship_method: Optional[str] = Field(None, max_length=100)
    allocations: Optional[List[ShippingAllocation]] = Field(None, min_length=1, max_length=500)

    @field_validator("tracking_number", "carrier")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Field cannot be blank")
        return stripped
