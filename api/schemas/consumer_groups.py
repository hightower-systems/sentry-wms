"""Pydantic schemas for /api/admin/consumer-groups + /api/admin/connector-registry (v1.5.0 #125)."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)


class ConsumerGroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group_id: str = Field(..., min_length=1, max_length=64)
    connector_id: str = Field(..., min_length=1, max_length=64)
    # Empty dict means "no subscription filter"; the polling handler
    # treats absent keys the same as "no extra narrowing" so this is
    # a safe default. Structured fields (warehouse_ids, event_types)
    # are the only keys the polling query currently honours; any
    # other keys are accepted and stored but ignored on the hot path.
    subscription: Dict[str, Any] = Field(default_factory=dict)


class ConsumerGroupUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription: Optional[Dict[str, Any]] = None
