"""Cycle count request schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field


class CreateCycleCountRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    bin_ids: List[int] = Field(..., min_length=1)


class CycleCountLineEntry(BaseModel):
    count_line_id: Optional[int] = Field(None, gt=0)
    counted_quantity: int = Field(..., ge=0, le=1000000)
    unexpected: bool = Field(False)
    item_id: Optional[int] = Field(None, gt=0)
    sku: Optional[str] = Field(None, max_length=128)


class SubmitCycleCountRequest(BaseModel):
    count_id: int = Field(..., gt=0)
    lines: List[CycleCountLineEntry] = Field(..., min_length=1)
