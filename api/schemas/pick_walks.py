"""Picking / pick walk request schemas."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CreateBatchRequest(BaseModel):
    so_identifiers: List[str] = Field(..., min_length=1)
    warehouse_id: int = Field(..., gt=0)
    resume_existing: bool = Field(False)
    # SOs the picker explicitly chose to drop after a prior 409
    # insufficient_coverage response. Empty/omitted on the first call.
    exclude_so_ids: Optional[List[int]] = Field(default=None)


class WaveValidateRequest(BaseModel):
    # The scan input is barcode-shaped (could be SO number, SO barcode,
    # or transfer-order number), so the field is named `barcode`. The
    # original `so_barcode` name stays accepted via alias for older
    # mobile builds still in the field.
    model_config = ConfigDict(populate_by_name=True)

    barcode: str = Field(..., min_length=1, max_length=128, alias="so_barcode")
    warehouse_id: int = Field(..., gt=0)


class WaveCreateRequest(BaseModel):
    so_ids: List[int] = Field(..., min_length=1)
    warehouse_id: int = Field(..., gt=0)
    resume_existing: bool = Field(False)
    exclude_so_ids: Optional[List[int]] = Field(default=None)


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
