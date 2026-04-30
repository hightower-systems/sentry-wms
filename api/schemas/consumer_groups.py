"""Pydantic schemas for /api/admin/consumer-groups + /api/admin/connector-registry (v1.5.0 #125, v1.5.1 #145)."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)


class ConnectorUpdateRequest(BaseModel):
    """display_name only. connector_id is the FK target from
    consumer_groups + webhook_subscriptions; renaming would orphan
    rows so the schema does not accept it."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(..., min_length=1, max_length=128)


class SubscriptionFilter(BaseModel):
    """v1.5.1 V-204 (#145): strict schema for consumer_groups.subscription.

    Pre-v1.5.1 the subscription was a free-form Dict[str, Any]. An
    admin could save {"event_types": "string-not-array"} and the next
    poll would crash with 500 when the handler iterated over the
    string's chars and hit ValueError on int() of a non-digit. Tightening
    the shape + rejecting unknown keys stops that class of mistake
    (and the associated persistence-shaped attack) at the admin
    endpoint rather than at poll time.

    Supported keys are additive; adding a new filter key means
    extending this model, not loosening it.
    """

    model_config = ConfigDict(extra="forbid")

    event_types: Optional[List[str]] = Field(None, max_length=64)
    warehouse_ids: Optional[List[int]] = Field(None, max_length=64)


class ConsumerGroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group_id: str = Field(..., min_length=1, max_length=64)
    connector_id: str = Field(..., min_length=1, max_length=64)
    # v1.5.1 V-204 (#145): strict nested schema. Empty object means
    # "no subscription filter" (both fields default to None); the
    # polling handler treats absent keys as "no narrowing".
    subscription: SubscriptionFilter = Field(default_factory=SubscriptionFilter)
    # v1.5.1 V-207 (#148): explicit opt-in required to recreate a
    # consumer_group whose id carries a tombstone from a prior
    # deletion. Default False forces the admin to confirm the
    # cursor=0 replay is intended.
    acknowledge_replay: bool = False


class ConsumerGroupUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription: Optional[SubscriptionFilter] = None
