"""Sales order request schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field


class SOLineEntry(BaseModel):
    item_id: int = Field(..., gt=0)
    quantity_ordered: int = Field(..., gt=0, le=1000000)
    line_number: Optional[int] = Field(None, ge=1)


class CreateSalesOrderRequest(BaseModel):
    so_number: str = Field(..., min_length=1, max_length=128)
    warehouse_id: int = Field(..., gt=0)
    lines: List[SOLineEntry] = Field(..., min_length=1)
    so_barcode: Optional[str] = Field(None, max_length=128)
    customer_name: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_address: Optional[str] = Field(None, max_length=512)
    ship_method: Optional[str] = Field(None, max_length=100)
    ship_address: Optional[str] = Field(None, max_length=512)
    ship_by_date: Optional[str] = Field(None, max_length=32)


class UpdateSalesOrderRequest(BaseModel):
    so_number: Optional[str] = Field(None, min_length=1, max_length=128)
    so_barcode: Optional[str] = Field(None, max_length=128)
    customer_name: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_address: Optional[str] = Field(None, max_length=512)
    ship_method: Optional[str] = Field(None, max_length=100)
    ship_address: Optional[str] = Field(None, max_length=512)
    ship_by_date: Optional[str] = Field(None, max_length=32)
    priority: Optional[int] = Field(None, ge=0, le=10)
