"""Pydantic schemas for /api/admin/tokens (v1.5.0 #129, v1.5.1 #140)."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from middleware.auth_middleware import V150_ENDPOINT_SLUGS


class CreateTokenRequest(BaseModel):
    token_name: str = Field(..., min_length=1, max_length=128)
    warehouse_ids: List[int] = Field(default_factory=list)
    event_types: List[str] = Field(default_factory=list, max_length=64)
    # v1.5.1 V-200 (#140): endpoints is required and non-empty.
    # Pre-v1.5.1 the field was accepted but never enforced by the
    # decorator, so admins could not have relied on "empty = deny"
    # in production. Migration 026 backfills existing empty rows
    # with the full v1 slug set; new tokens must be explicit.
    endpoints: List[str] = Field(..., min_length=1, max_length=64)
    connector_id: Optional[str] = Field(None, max_length=64)
    # Override the migration 023 default (+1 year) when issuing a
    # short-lived or long-lived token explicitly. None = use default.
    expires_at: Optional[datetime] = None

    @field_validator("endpoints")
    @classmethod
    def _known_slugs_only(cls, v: List[str]) -> List[str]:
        unknown = sorted({s for s in v if s not in V150_ENDPOINT_SLUGS})
        if unknown:
            raise ValueError(
                f"unknown endpoint slugs: {unknown}. "
                f"valid: {sorted(V150_ENDPOINT_SLUGS.keys())}"
            )
        return v


class UpdateTokenRequest(BaseModel):
    """Admin metadata-only edit. Does not rotate the hash; use /rotate for that."""

    token_name: Optional[str] = Field(None, min_length=1, max_length=128)
    warehouse_ids: Optional[List[int]] = None
    event_types: Optional[List[str]] = Field(None, max_length=64)
    endpoints: Optional[List[str]] = Field(None, max_length=64)
    expires_at: Optional[datetime] = None

    @field_validator("endpoints")
    @classmethod
    def _known_slugs_only(cls, v):
        if v is None:
            return v
        if not v:
            raise ValueError("endpoints must be non-empty when provided")
        unknown = sorted({s for s in v if s not in V150_ENDPOINT_SLUGS})
        if unknown:
            raise ValueError(
                f"unknown endpoint slugs: {unknown}. "
                f"valid: {sorted(V150_ENDPOINT_SLUGS.keys())}"
            )
        return v
