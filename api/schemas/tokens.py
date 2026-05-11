"""Pydantic schemas for /api/admin/tokens (v1.5.0 #129, v1.5.1 #140, v1.7.0 Pipe B)."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from middleware.auth_middleware import (
    V150_ENDPOINT_SLUGS,
    V170_INBOUND_RESOURCE_BY_ENDPOINT,
    V190_DOCKD_SLUG,
    V1100_POS_SLUG,
)


_INBOUND_RESOURCE_KEYS = frozenset(V170_INBOUND_RESOURCE_BY_ENDPOINT.values())

# Direction-bearing slugs accepted by the create/update token validators.
# V150 outbound slugs map 1:1 to a Flask endpoint; V190 dockd and V1100
# POS each cover a 1:N surface under a single slug. Listed together so
# the validator's "unknown slug" message is honest about every value the
# auth middleware will actually honor at request time.
_KNOWN_ENDPOINT_SLUGS = (
    frozenset(V150_ENDPOINT_SLUGS.keys())
    | {V190_DOCKD_SLUG, V1100_POS_SLUG}
)


class CreateTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    # v1.8.0 (#270): per-token static override map. Keys are canonical
    # field names; values are the values to write (replacing any
    # source-derived value) at apply time. Only consulted when
    # mapping_override is also True. Canonical-field-name allowlist
    # validation happens at the admin route level (needs DB access for
    # information_schema.columns lookup) -- this schema only enforces
    # shape + size + the capability-flag pairing rule.
    mapping_overrides: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("endpoints")
    @classmethod
    def _known_slugs_only(cls, v: List[str]) -> List[str]:
        unknown = sorted({s for s in v if s not in _KNOWN_ENDPOINT_SLUGS})
        if unknown:
            raise ValueError(
                f"unknown endpoint slugs: {unknown}. "
                f"valid: {sorted(_KNOWN_ENDPOINT_SLUGS)}"
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
        # v1.8.0 (#270): non-empty mapping_overrides requires the
        # capability flag. The handler only consults the JSONB when
        # the flag is True; rejecting the half-configured shape at
        # admin time prevents a silent no-op token where the operator
        # set overrides but forgot the gate.
        if self.mapping_overrides and not self.mapping_override:
            raise ValueError(
                "mapping_overrides requires mapping_override=true; the "
                "inbound handler ignores the JSONB unless the capability "
                "flag is set."
            )
        # Reject keys that are obviously not canonical field names.
        # The deeper "must be a column on a token-resource canonical
        # table" check lives at the admin route (needs DB access).
        for key in self.mapping_overrides:
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"mapping_overrides key {key!r} must be a non-empty string"
                )
            if not key.replace("_", "").isalnum():
                raise ValueError(
                    f"mapping_overrides key {key!r} must be alphanumeric "
                    "with underscores (canonical-field-name shape)"
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
        unknown = sorted({s for s in v if s not in _KNOWN_ENDPOINT_SLUGS})
        if unknown:
            raise ValueError(
                f"unknown endpoint slugs: {unknown}. "
                f"valid: {sorted(_KNOWN_ENDPOINT_SLUGS)}"
            )
        return v
