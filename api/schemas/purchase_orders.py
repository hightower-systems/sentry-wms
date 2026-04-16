"""Purchase order request schemas."""

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class POLineEntry(BaseModel):
    item_id: int = Field(..., gt=0)
    quantity_ordered: int = Field(..., gt=0, le=1000000)
    unit_cost: Optional[Decimal] = Field(None, ge=0)
    line_number: Optional[int] = Field(None, ge=1)


class CreatePurchaseOrderRequest(BaseModel):
    po_number: str = Field(..., min_length=1, max_length=128)
    warehouse_id: int = Field(..., gt=0)
    lines: List[POLineEntry] = Field(..., min_length=1)
    po_barcode: Optional[str] = Field(None, max_length=128)
    vendor_name: Optional[str] = Field(None, max_length=256)
    expected_date: Optional[str] = Field(None, max_length=32)
    notes: Optional[str] = Field(None, max_length=1000)


class UpdatePurchaseOrderRequest(BaseModel):
    po_number: Optional[str] = Field(None, min_length=1, max_length=128)
    po_barcode: Optional[str] = Field(None, max_length=128)
    vendor_name: Optional[str] = Field(None, max_length=256)
    expected_date: Optional[str] = Field(None, max_length=32)
    notes: Optional[str] = Field(None, max_length=1000)
