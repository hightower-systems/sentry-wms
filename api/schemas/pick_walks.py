"""Picking / pick walk request schemas."""

from typing import List

from pydantic import BaseModel, Field


class CreateBatchRequest(BaseModel):
    so_identifiers: List[str] = Field(..., min_length=1)
    warehouse_id: int = Field(..., gt=0)


class WaveValidateRequest(BaseModel):
    so_barcode: str = Field(..., min_length=1, max_length=128)
    warehouse_id: int = Field(..., gt=0)


class WaveCreateRequest(BaseModel):
    so_ids: List[int] = Field(..., min_length=1)
    warehouse_id: int = Field(..., gt=0)


class ConfirmPickRequest(BaseModel):
    pick_task_id: int = Field(..., gt=0)
    scanned_barcode: str = Field(..., min_length=1, max_length=128)
    quantity_picked: int = Field(..., gt=0, le=100000)


class ShortPickRequest(BaseModel):
    pick_task_id: int = Field(..., gt=0)
    quantity_available: int = Field(0, ge=0, le=100000)


class CompleteBatchRequest(BaseModel):
    batch_id: int = Field(..., gt=0)


class CancelBatchRequest(BaseModel):
    batch_id: int = Field(..., gt=0)
