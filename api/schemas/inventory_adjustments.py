"""Inventory adjustment request schemas."""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AdjustmentAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bin_id: int = Field(..., gt=0, strict=True)
    quantity: int = Field(..., gt=0, le=1000000, strict=True)
    lot_number: Optional[str] = Field(None, min_length=1, max_length=50)
    serial_number: Optional[str] = Field(None, min_length=1, max_length=100)

    @field_validator("lot_number", "serial_number")
    @classmethod
    def validate_identity(cls, value, info):
        if value is not None and (value != value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Provide one exact scanned tracking identity")
        if value is not None and info.field_name == "serial_number" and "," in value:
            raise ValueError("Provide one serial identity per allocation")
        return value


class AdjustmentDecision(BaseModel):
    adjustment_id: int = Field(..., gt=0)
    action: str = Field(..., min_length=1)
    allocations: Optional[List[AdjustmentAllocation]] = Field(None, min_length=1, max_length=500)

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ("approve", "reject"):
            raise ValueError("action must be 'approve' or 'reject'")
        return v


class ReviewAdjustmentsRequest(BaseModel):
    decisions: List[AdjustmentDecision] = Field(..., min_length=1, max_length=500)

    @field_validator("decisions")
    @classmethod
    def unique_decisions(cls, decisions):
        if len({d.adjustment_id for d in decisions}) != len(decisions):
            raise ValueError("Each adjustment can be reviewed only once per request")
        return decisions


class DirectAdjustmentRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    warehouse_id: int = Field(..., gt=0)
    adjustment_type: str = Field(..., min_length=1, max_length=10)
    quantity: int = Field(..., gt=0, le=1000000)
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("adjustment_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        v = v.upper()
        if v not in ("ADD", "REMOVE"):
            raise ValueError("adjustment_type must be ADD or REMOVE")
        return v
