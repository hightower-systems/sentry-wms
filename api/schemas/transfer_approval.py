"""Physical allocations for an existing transfer approval."""

from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class TransferApprovalAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    to_line_id: int = Field(..., gt=0)
    source_bin_id: int = Field(..., gt=0)
    destination_bin_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100000)
    lot_number: Optional[str] = Field(None, min_length=1, max_length=50)
    serial_number: Optional[str] = Field(None, min_length=1, max_length=100)


class TransferApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allocations: Optional[List[TransferApprovalAllocation]] = Field(None, min_length=1, max_length=500)
