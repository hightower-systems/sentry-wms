"""Shipping request schemas."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class FulfillRequest(BaseModel):
    so_id: int = Field(..., gt=0)
    tracking_number: str = Field(..., min_length=1, max_length=255)
    carrier: str = Field(..., min_length=1, max_length=100)
    ship_method: Optional[str] = Field(None, max_length=100)

    @field_validator("tracking_number", "carrier")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Field cannot be blank")
        return stripped
