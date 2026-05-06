"""Pydantic schemas for /api/admin/tokens (v1.5.0 #129, v1.5.1 #140, v1.7.0 Pipe B)."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from middleware.auth_middleware import (
    V150_ENDPOINT_SLUGS,
    V170_INBOUND_RESOURCE_BY_ENDPOINT,
)


_INBOUND_RESOURCE_KEYS = frozenset(V170_INBOUND_RESOURCE_BY_ENDPOINT.values())


class CreateTokenRequest(BaseModel):
    token_name: str = Field(..., min_length=1, max_length=128)
    warehouse_ids: List[int] = Field(default_factory=list)
    event_types: List[str] = Field(default_factory=list, max_length=64)
    # v1.5.1 V-200 (#140): endpoints (outbound slug list) WAS required.
    # v1.7.0 relaxes that: an inbound-only token has no outbound slugs.
    # The model validator below enforces "at least one direction set":
    # either endpoints OR (source_system + inbound_resources) must be
    # non-empty. Tokens with both opted in are valid (connector
    # framework shape at v1.9).
    endpoints: List[str] = Field(default_factory=list, max_length=64)
    connector_id: Optional[str] = Field(None, max_length=64)
    # Override the migration 023 default (+1 year) when issuing a
    # short-lived or long-lived token explicitly. None = use default.
    expires_at: Optional[datetime] = None
    # v1.7.0 Pipe B inbound scope dimensions.
    source_system: Optional[str] = Field(None, max_length=64)
    inbound_resources: List[str] = Field(default_factory=list, max_length=16)
    mapping_override: bool = False

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

    @field_validator("inbound_resources")
    @classmethod
    def _known_inbound_resources_only(cls, v: List[str]) -> List[str]:
        unknown = sorted({s for s in v if s not in _INBOUND_RESOURCE_KEYS})
        if unknown:
            raise ValueError(
                f"unknown inbound_resources: {unknown}. "
                f"valid: {sorted(_INBOUND_RESOURCE_KEYS)}"
            )
        return v

    @model_validator(mode="after")
    def _at_least_one_direction(self) -> "CreateTokenRequest":
        outbound = bool(self.endpoints)
        inbound = bool(self.source_system) and bool(self.inbound_resources)
        if not outbound and not inbound:
            raise ValueError(
                "Token must have at least one direction set: either "
                "endpoints (outbound) or source_system + inbound_resources "
                "(inbound)."
            )
        # An inbound_resources without source_system, or vice versa, is a
        # half-configured inbound token -- the decorator would refuse to
        # let it through (V170 cross-direction guard, #252). Catch at
        # creation so admin sees a clear error rather than discovering
        # the issue at first POST.
        if bool(self.source_system) ^ bool(self.inbound_resources):
            raise ValueError(
                "source_system and inbound_resources must be set together "
                "or both omitted."
            )
        if self.mapping_override and not self.inbound_resources:
            raise ValueError(
                "mapping_override capability only applies to inbound "
                "tokens; set inbound_resources or clear mapping_override."
            )
        return self


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
