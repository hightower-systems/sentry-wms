"""Pydantic schemas for /api/v1/events (v1.5.0 #122 + #123)."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class SnapshotQuery(BaseModel):
    """Query params for GET /api/v1/snapshot/inventory (v1.5.0 #133).

    ``cursor`` is an opaque base64 blob produced by the endpoint on
    every non-final page. ``warehouse_id`` is required; ``limit``
    caps at 2000 same as the polling endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    warehouse_id: int = Field(..., gt=0)
    cursor: Optional[str] = Field(None, max_length=2048)
    limit: int = Field(500, ge=1, le=2000)


class AckBody(BaseModel):
    """POST /api/v1/events/ack body (plan 2.4).

    ``cursor`` is the highest event_id the consumer has finished
    processing. The server advances ``last_cursor = max(last_cursor,
    cursor)`` via an atomic ``UPDATE ... WHERE last_cursor <= :cursor``
    so out-of-order acks are no-ops without a separate race-free read.
    """

    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(..., min_length=1, max_length=64)
    cursor: int = Field(..., ge=0)
