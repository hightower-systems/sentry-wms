"""Shared helpers for the v1.5.0 polling-endpoint test suites (#126).

test_polling.py, test_consumer_groups.py, and test_events_schema_registry.py
all need the same token-issuance and event-insertion helpers. Keeping
them here avoids triplicating the logic while letting each file own
its own fixture shape.
"""

import hashlib
import json
import os
import uuid

from db_test_context import get_raw_connection


PEPPER = os.environ.get("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")


def hash_token(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def insert_token(plaintext: str, warehouse_ids, event_types):
    """Insert a wms_tokens row via the fixture's raw connection so the
    row is visible to the handler's SessionLocal through the shared
    outer transaction. Returns the new token_id.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO wms_tokens (token_name, token_hash, warehouse_ids, event_types) "
        "VALUES (%s, %s, %s, %s) RETURNING token_id",
        (
            f"polling-test-{uuid.uuid4()}",
            hash_token(plaintext),
            warehouse_ids,
            event_types,
        ),
    )
    token_id = cur.fetchone()[0]
    cur.close()
    return token_id


def insert_event(
    event_type="receipt.completed",
    warehouse_id=1,
    visible_at="NOW() - INTERVAL '5 seconds'",
    aggregate_type="item_receipt",
    aggregate_id=None,
    payload=None,
):
    """Insert a row into integration_events with visible_at forced to
    a past or future timestamp as the caller needs. The deferred
    trigger never fires inside the fixture's outer transaction, so
    the caller's ``visible_at`` expression wins.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    if aggregate_id is None:
        aggregate_id = abs(hash(uuid.uuid4())) % 10_000_000
    cur.execute(
        f"""
        INSERT INTO integration_events (
            event_type, event_version, aggregate_type, aggregate_id,
            aggregate_external_id, warehouse_id, source_txn_id, visible_at, payload
        ) VALUES (%s, 1, %s, %s, %s, %s, %s, {visible_at}, %s)
        RETURNING event_id
        """,
        (
            event_type,
            aggregate_type,
            aggregate_id,
            str(uuid.uuid4()),
            warehouse_id,
            str(uuid.uuid4()),
            json.dumps(payload or {"synthesized": True}),
        ),
    )
    event_id = cur.fetchone()[0]
    cur.close()
    return event_id


def poll(client, token_plaintext, **params):
    """GET /api/v1/events with the given X-WMS-Token and query params."""
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = "/api/v1/events" + ("?" + qs if qs else "")
    return client.get(url, headers={"X-WMS-Token": token_plaintext})
