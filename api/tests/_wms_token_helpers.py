"""Shared helpers for the v1.5.0 wms_tokens test suites (#130).

test_wms_token_decorator.py, test_token_cache.py, and
test_token_rate_limit.py all need the same token-issuance and
plaintext-hash helpers. Keeping them here avoids triplicating the
logic. Each test file owns its own fixture shape.
"""

import hashlib
import os

import psycopg2


PEPPER = os.environ.get("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry"
)

# v1.5.1 V-200 (#140): the @require_wms_token decorator now enforces
# the endpoints slug list ("empty = deny" matches warehouse_ids /
# event_types). Tests that only care about auth/TTL/rate semantics
# pass endpoints=None and get the full v1 slug set by default so
# they keep passing the endpoint-scope check. Tests that specifically
# exercise endpoint-scope behaviour override this explicitly.
DEFAULT_TEST_ENDPOINTS = [
    "events.poll",
    "events.ack",
    "events.types",
    "events.schema",
    "snapshot.inventory",
]


def sha256_with_pepper(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def insert_token(
    name="test-token",
    plaintext="live-plaintext",
    status="active",
    expires_at=None,
    revoked_at=None,
    warehouse_ids=None,
    event_types=None,
    endpoints=None,
    connector_id=None,
    source_system=None,
    inbound_resources=None,
    mapping_override=False,
):
    """Insert a wms_tokens row via autocommit so the row is visible to
    the test's own connection AND the decorator's fresh session.

    expires_at=None means "use the migration-023 default" (~1 year out).
    Pass an explicit datetime to override (e.g. a past-dated value for
    the expired-token decorator test).

    v1.7.0 Pipe B: source_system / inbound_resources / mapping_override
    default to None / [] / False so existing v1.5 / v1.6 tests are
    unchanged. When source_system is supplied, the helper INSERTs the
    matching inbound_source_systems_allowlist row first (kind='internal_tool')
    so the FK is satisfied; the row is left in place at test scope and
    is wiped by the session TRUNCATE in conftest.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        if source_system is not None:
            cur.execute(
                "INSERT INTO inbound_source_systems_allowlist "
                "(source_system, kind) VALUES (%s, 'internal_tool') "
                "ON CONFLICT DO NOTHING",
                (source_system,),
            )
        cols = [
            "token_name", "token_hash", "status",
            "warehouse_ids", "event_types", "endpoints", "connector_id",
            "source_system", "inbound_resources", "mapping_override",
        ]
        vals = [
            name,
            sha256_with_pepper(plaintext),
            status,
            warehouse_ids or [1],
            event_types or [],
            list(DEFAULT_TEST_ENDPOINTS) if endpoints is None else endpoints,
            connector_id,
            source_system,
            inbound_resources or [],
            mapping_override,
        ]
        if expires_at is not None:
            cols.append("expires_at")
            vals.append(expires_at)
        # v1.7.0 #278: tests asserting the auth-side revoked_at gate need
        # to seed a row directly with revoked_at populated. INSERT does
        # not fire the AFTER UPDATE OF revoked_at trigger, so this path
        # produces the "active status + revoked_at populated" shape that
        # would otherwise require bypassing the trigger.
        if revoked_at is not None:
            cols.append("revoked_at")
            vals.append(revoked_at)
        placeholders = ", ".join(["%s"] * len(cols))
        cur.execute(
            f"INSERT INTO wms_tokens ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING token_id",
            vals,
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def delete_token(token_id):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
    finally:
        conn.close()
