"""Strict-typed subscription_filter Pydantic model.

The ``webhook_subscriptions.subscription_filter`` column is JSONB
because the filter shape evolves across v1.6.x. The dispatcher
parses it on every read via :class:`SubscriptionFilter`, which
declares ``extra="forbid"`` so an unknown key surfaces as a
recoverable parse error rather than silently passing through to
a SQL projection that ignores it. This is the strict-typing
pattern from V-204 #145 applied to outbound webhooks.

Empty list semantics: an explicit empty list (``event_types=[]``)
means "no filter clause for this key emitted by the dispatcher",
identical to ``None``. The integration-tests path treats both as
"matches every event"; the admin-time validation path that lands
with the webhook CRUD endpoints will reject ``[]`` explicitly so
an operator does not write what looks like an always-false filter.
"""

from typing import List, Optional, Union
from uuid import UUID

import json

from pydantic import BaseModel, ConfigDict


class SubscriptionFilter(BaseModel):
    """Filter projection over integration_events. Each field is
    optional; absent fields contribute no SQL clause."""

    model_config = ConfigDict(extra="forbid")

    event_types: Optional[List[str]] = None
    warehouse_ids: Optional[List[int]] = None
    aggregate_external_id_allowlist: Optional[List[UUID]] = None


def parse(value: Union[None, str, dict, "SubscriptionFilter"]) -> SubscriptionFilter:
    """Parse the JSONB column value into a :class:`SubscriptionFilter`.
    Accepts:

      * ``None`` / empty string -> empty filter (matches every event).
      * ``dict`` -> validated through the model.
      * JSON string -> json.loads then validated.
      * an existing :class:`SubscriptionFilter` -> returned as-is.

    Raises :class:`pydantic.ValidationError` on a malformed dict
    (unknown key, wrong type, etc.). Raises ``json.JSONDecodeError``
    on a malformed JSON string. The dispatcher catches both and logs
    a warning before falling back to the empty filter.
    """
    if isinstance(value, SubscriptionFilter):
        return value
    if value is None or value == "":
        return SubscriptionFilter()
    if isinstance(value, str):
        value = json.loads(value)
    return SubscriptionFilter.model_validate(value)
