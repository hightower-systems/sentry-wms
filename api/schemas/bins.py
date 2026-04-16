"""Bin request schemas."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator

VALID_BIN_TYPES = ("Staging", "PickableStaging", "Pickable")


class CreateBinRequest(BaseModel):
    zone_id: int = Field(..., gt=0)
    warehouse_id: int = Field(..., gt=0)
    bin_code: str = Field(..., min_length=1, max_length=64)
    bin_barcode: str = Field(..., min_length=1, max_length=128)
    bin_type: str = Field(..., min_length=1, max_length=32)
    aisle: Optional[str] = Field(None, max_length=32)
    row_num: Optional[int] = Field(None, ge=0)
    level_num: Optional[int] = Field(None, ge=0)
    position_num: Optional[int] = Field(None, ge=0)
    pick_sequence: int = Field(0, ge=0)
    putaway_sequence: int = Field(0, ge=0)

    @field_validator("bin_type")
    @classmethod
    def validate_bin_type(cls, v: str) -> str:
        if v not in VALID_BIN_TYPES:
            raise ValueError(f"bin_type must be one of: {', '.join(VALID_BIN_TYPES)}")
        return v


class UpdateBinRequest(BaseModel):
    bin_code: Optional[str] = Field(None, min_length=1, max_length=64)
    bin_barcode: Optional[str] = Field(None, max_length=128)
    bin_type: Optional[str] = Field(None, min_length=1, max_length=32)
    aisle: Optional[str] = Field(None, max_length=32)
    row_num: Optional[int] = Field(None, ge=0)
    level_num: Optional[int] = Field(None, ge=0)
    position_num: Optional[int] = Field(None, ge=0)
    pick_sequence: Optional[int] = Field(None, ge=0)
    putaway_sequence: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    zone_id: Optional[int] = Field(None, gt=0)

    @field_validator("bin_type")
    @classmethod
    def validate_bin_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_BIN_TYPES:
            raise ValueError(f"bin_type must be one of: {', '.join(VALID_BIN_TYPES)}")
        return v
