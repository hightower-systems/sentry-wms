"""Pydantic body schemas for /api/v1/inbound/* (v1.7.0 Pipe B).

One body shape across all five resource endpoints
(sales_orders / items / customers / vendors / purchase_orders).
The handler dispatches on URL, not body shape; the body's
canonical-side translation lives in the per-source mapping document
(services.mapping_loader). Strict-typed (extra='forbid', V-204
alignment) so a typo'd field name fails fast at 422 rather than
silently disappearing into source_payload.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class InboundBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    external_version: str = Field(min_length=1, max_length=64)
    source_payload: Dict[str, Any]
    # Per-request override for ad-hoc data fixes. Rejected by the handler
    # unless the token's mapping_override capability flag is set; the
    # decorator does not gate at this level (capability is a body-content
    # check, not a route-level gate).
    mapping_overrides: Optional[Dict[str, Any]] = None
