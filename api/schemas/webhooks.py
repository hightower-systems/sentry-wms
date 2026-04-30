"""Pydantic schemas for /api/admin/webhooks (v1.6.0).

Strict-typed bodies for the webhook subscription CRUD endpoints.
``extra='forbid'`` so an unknown field surfaces as a 400 rather
than slipping past the validator and silently dropping at the SQL
projection step. Mirrors the v1.5 token-CRUD schema shape.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.webhook_dispatcher.subscription_filter import SubscriptionFilter


class UpdateWebhookRequest(BaseModel):
    """Body for PATCH /api/admin/webhooks/<id>. All fields are
    optional; an absent field leaves the persisted column
    unchanged. extra='forbid' so an unknown field surfaces as a
    400 rather than silently dropping at the SQL layer.

    ``status`` only accepts the operator transitions
    'active' / 'paused'. The 'revoked' terminal status is reached
    through the DELETE endpoint, not here, so a typo in the
    request cannot accidentally revoke a subscription.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, min_length=1, max_length=128)
    delivery_url: Optional[str] = Field(None, min_length=1, max_length=2048)
    subscription_filter: Optional[SubscriptionFilter] = None
    rate_limit_per_second: Optional[int] = Field(None, ge=1, le=100)
    pending_ceiling: Optional[int] = Field(None, ge=1)
    dlq_ceiling: Optional[int] = Field(None, ge=1)
    status: Optional[str] = None

    @field_validator("delivery_url")
    @classmethod
    def _strip_url(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v is not None else v

    @field_validator("status")
    @classmethod
    def _status_only_active_or_paused(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ("active", "paused"):
            raise ValueError(
                "status accepts only 'active' or 'paused'. Use the "
                "DELETE endpoint to revoke a subscription."
            )
        return v


class CreateWebhookRequest(BaseModel):
    """Body for POST /api/admin/webhooks. Plain types only; the
    server casts to UUIDs / JSONB / int ranges before INSERT."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)
    delivery_url: str = Field(..., min_length=1, max_length=2048)
    subscription_filter: SubscriptionFilter = Field(
        default_factory=SubscriptionFilter
    )
    rate_limit_per_second: int = Field(default=50, ge=1, le=100)
    pending_ceiling: int = Field(default=10_000, ge=1)
    dlq_ceiling: int = Field(default=1_000, ge=1)
    acknowledge_url_reuse: bool = False

    @field_validator("delivery_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        return v.strip()


_REPLAY_STATUS_VALUES = ("dlq", "failed", "succeeded")


class ReplayBatchFilter(BaseModel):
    """Filter narrowing for batch replays. Each absent field
    contributes no SQL clause."""

    model_config = ConfigDict(extra="forbid")

    status: str = "dlq"
    event_type: Optional[str] = Field(None, min_length=1, max_length=64)
    warehouse_id: Optional[int] = Field(None, ge=1)
    completed_at_from: Optional[datetime] = None
    completed_at_to: Optional[datetime] = None

    @field_validator("status")
    @classmethod
    def _status_in_allowed(cls, v: str) -> str:
        if v not in _REPLAY_STATUS_VALUES:
            raise ValueError(
                f"status must be one of {_REPLAY_STATUS_VALUES}; got {v!r}"
            )
        return v


class ReplayBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filter: ReplayBatchFilter = Field(default_factory=ReplayBatchFilter)
    acknowledge_large_replay: bool = False
