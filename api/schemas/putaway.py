"""Put-away request schemas."""

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ConfirmPutawayRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    from_bin_id: int = Field(..., gt=0)
    to_bin_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100000)
    lot_number: Optional[str] = Field(None, max_length=100)

    @model_validator(mode="after")
    def bins_must_differ(self):
        if self.from_bin_id == self.to_bin_id:
            raise ValueError("from_bin_id and to_bin_id must be different")
        return self


class UpdatePreferredRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    set_as_primary: bool = Field(True)
