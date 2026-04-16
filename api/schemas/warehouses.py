"""Warehouse request schemas."""

from typing import Optional

from pydantic import BaseModel, Field


class CreateWarehouseRequest(BaseModel):
    warehouse_code: str = Field(..., min_length=1, max_length=32)
    warehouse_name: str = Field(..., min_length=1, max_length=256)
    address: Optional[str] = Field(None, max_length=512)


class UpdateWarehouseRequest(BaseModel):
    warehouse_code: Optional[str] = Field(None, min_length=1, max_length=32)
    warehouse_name: Optional[str] = Field(None, min_length=1, max_length=256)
    address: Optional[str] = Field(None, max_length=512)
    is_active: Optional[bool] = None


class InterWarehouseTransferRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    from_bin_id: int = Field(..., gt=0)
    from_warehouse_id: int = Field(..., gt=0)
    to_bin_id: int = Field(..., gt=0)
    to_warehouse_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100000)
    reason: Optional[str] = Field(None, max_length=500)
