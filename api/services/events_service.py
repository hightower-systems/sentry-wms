"""emit_event: append a row to integration_events inside the caller's transaction.

Shaped after ``write_audit_log`` so every emit site reads identically.
The helper runs inside the caller's open transaction (no commit, no
rollback); ``integration_events.visible_at`` gets set by the deferred
trigger at COMMIT time so readers see events in commit order even when
BIGSERIAL assigned ``event_id`` values out of commit order (see
migration 020 for the full trigger contract).

Idempotency: the table's ``(aggregate_type, aggregate_id, event_type,
source_txn_id)`` UNIQUE constraint collapses retries of the same
logical request into a single row. ``INSERT ... ON CONFLICT DO
NOTHING RETURNING event_id`` returns the new ``event_id`` on first
emit and ``None`` when the row was a duplicate; callers treat
``None`` as "already emitted, no-op."

Schema validation is gated by ``SENTRY_VALIDATE_EVENT_SCHEMAS`` (Decision U):

- ``true`` / ``1`` / ``yes``: validate every payload against the
  registered schema before insert. A mismatch raises; tests and CI run
  this way so code bugs fail loudly.
- anything else (default in production): skip validation and rely on
  the consumer's own validator. Prevents a payload-schema drift from
  blocking mobile writes in the warehouse.
"""

import json
import os
import uuid
from typing import Optional

from sqlalchemy import text

from services.events_schema_registry import get_validator

_VALIDATION_ENABLED = os.getenv("SENTRY_VALIDATE_EVENT_SCHEMAS", "").lower() in (
    "1",
    "true",
    "yes",
)


def emit_event(
    db,
    event_type: str,
    event_version: int,
    aggregate_type: str,
    aggregate_id: int,
    aggregate_external_id,
    warehouse_id: int,
    source_txn_id,
    payload: dict,
) -> Optional[int]:
    """Append one row to ``integration_events`` inside the caller's transaction.

    Returns the new ``event_id`` on first emit, or ``None`` if a row with
    the same ``(aggregate_type, aggregate_id, event_type, source_txn_id)``
    already exists (idempotent replay).

    Validation failure raises ``jsonschema.ValidationError`` when the
    ``SENTRY_VALIDATE_EVENT_SCHEMAS`` env var is set. An unknown
    ``(event_type, event_version)`` always raises ``KeyError`` - that's
    a code bug regardless of env.
    """
    if _VALIDATION_ENABLED:
        validator = get_validator(event_type, event_version)
        validator.validate(payload)

    result = db.execute(
        text(
            """
            INSERT INTO integration_events (
                event_type, event_version, aggregate_type, aggregate_id,
                aggregate_external_id, warehouse_id, source_txn_id, payload
            )
            VALUES (
                :event_type, :event_version, :aggregate_type, :aggregate_id,
                :aggregate_external_id, :warehouse_id, :source_txn_id, CAST(:payload AS JSONB)
            )
            ON CONFLICT (aggregate_type, aggregate_id, event_type, source_txn_id) DO NOTHING
            RETURNING event_id
            """
        ),
        {
            "event_type": event_type,
            "event_version": event_version,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_external_id": _as_str(aggregate_external_id),
            "warehouse_id": warehouse_id,
            "source_txn_id": _as_str(source_txn_id),
            "payload": json.dumps(payload),
        },
    )
    row = result.fetchone()
    return row[0] if row else None


def _as_str(value):
    """Normalise a uuid.UUID or str input to the string form Postgres expects."""
    if isinstance(value, uuid.UUID):
        return str(value)
    return value
