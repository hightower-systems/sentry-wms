"""Pydantic schemas for /api/admin/webhooks (v1.6.0).

Strict-typed bodies for the webhook subscription CRUD endpoints.
``extra='forbid'`` so an unknown field surfaces as a 400 rather
than slipping past the validator and silently dropping at the SQL
projection step. Mirrors the v1.5 token-CRUD schema shape.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.webhook_dispatcher.subscription_filter import SubscriptionFilter


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
