"""Packing / pack verification request schemas."""

from pydantic import BaseModel, Field


class VerifyPackItemRequest(BaseModel):
    so_id: int = Field(..., gt=0)
    scanned_barcode: str = Field(..., min_length=1, max_length=128)
    quantity: int = Field(1, gt=0, le=100000)


class CompletePackingRequest(BaseModel):
    so_id: int = Field(..., gt=0)
