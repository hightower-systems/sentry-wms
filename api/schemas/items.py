"""Item request schemas."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class CreateItemRequest(BaseModel):
    sku: str = Field(..., min_length=1, max_length=128)
    item_name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = Field(None, max_length=1000)
    upc: Optional[str] = Field(None, max_length=128)
    category: Optional[str] = Field(None, max_length=128)
    weight_lbs: Optional[Decimal] = Field(None, ge=0, le=99999)
    default_bin_id: Optional[int] = Field(None, gt=0)
    # avid-overhaul-mk1 mig 058: optional canonical vendor FK. Sent
    # as a UUID string; pydantic accepts the raw str and the route
    # passes it straight into the parameterized INSERT.
    vendor_id: Optional[str] = Field(None, max_length=64)


class UpdateItemRequest(BaseModel):
    sku: Optional[str] = Field(None, min_length=1, max_length=128)
    item_name: Optional[str] = Field(None, min_length=1, max_length=256)
    description: Optional[str] = Field(None, max_length=1000)
    upc: Optional[str] = Field(None, max_length=128)
    category: Optional[str] = Field(None, max_length=128)
    weight_lbs: Optional[Decimal] = Field(None, ge=0, le=99999)
    default_bin_id: Optional[int] = Field(None, gt=0)
    reorder_point: Optional[int] = Field(None, ge=0)
    reorder_qty: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    # avid-overhaul-mk1 mig 058: optional canonical vendor FK. An empty
    # string is normalized to NULL in the route so the picker's "no
    # vendor" option clears the column cleanly.
    vendor_id: Optional[str] = Field(None, max_length=64)


class CreatePreferredBinRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    priority: int = Field(1, ge=1, le=100)


class UpdatePreferredBinRequest(BaseModel):
    priority: int = Field(..., ge=1, le=100)
