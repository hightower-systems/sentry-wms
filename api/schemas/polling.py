"""Pydantic query schemas for /api/v1/events (v1.5.0 #122)."""

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PollQuery(BaseModel):
    """Query-param validation for GET /api/v1/events.

    ``after`` and ``consumer_group`` are mutually exclusive (plan 2.1).
    Sending both raises at validation time; the handler turns that into a
    400 with a readable error body.

    ``limit`` defaults to 500 and caps at 2000 (plan 2.2). ``types`` is a
    comma-separated string the handler splits into a list before the
    query runs.
    """

    after: Optional[int] = Field(None, ge=0)
    consumer_group: Optional[str] = Field(None, max_length=64)
    types: Optional[str] = Field(None, max_length=2048)
    warehouse_id: Optional[int] = Field(None, gt=0)
    limit: int = Field(500, ge=1, le=2000)

    @model_validator(mode="after")
    def _mutex(self):
        if self.after is not None and self.consumer_group is not None:
            raise ValueError("after and consumer_group are mutually exclusive")
        return self
