"""Pydantic schemas for /api/admin/tokens (v1.5.0 #129)."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class CreateTokenRequest(BaseModel):
    token_name: str = Field(..., min_length=1, max_length=128)
    warehouse_ids: List[int] = Field(default_factory=list)
    event_types: List[str] = Field(default_factory=list, max_length=64)
    endpoints: List[str] = Field(default_factory=list, max_length=64)
    connector_id: Optional[str] = Field(None, max_length=64)
    # Override the migration 023 default (+1 year) when issuing a
    # short-lived or long-lived token explicitly. None = use default.
    expires_at: Optional[datetime] = None


class UpdateTokenRequest(BaseModel):
    """Admin metadata-only edit. Does not rotate the hash; use /rotate for that."""

    token_name: Optional[str] = Field(None, min_length=1, max_length=128)
    warehouse_ids: Optional[List[int]] = None
    event_types: Optional[List[str]] = Field(None, max_length=64)
    endpoints: Optional[List[str]] = Field(None, max_length=64)
    expires_at: Optional[datetime] = None
