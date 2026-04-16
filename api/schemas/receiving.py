"""Receiving request schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ReceiveItemEntry(BaseModel):
    item_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100000)
    bin_id: int = Field(..., gt=0)
    lot_number: Optional[str] = Field(None, max_length=100)
    serial_number: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = Field(None, max_length=1000)


class ReceiveItemsRequest(BaseModel):
    po_id: int = Field(..., gt=0)
    items: List[ReceiveItemEntry] = Field(..., min_length=1)


class CancelReceivingRequest(BaseModel):
    receipt_ids: List[int] = Field(default_factory=list)
    po_id: Optional[int] = Field(None, gt=0)
    warehouse_id: Optional[int] = Field(None, gt=0)
