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


def sha256_with_pepper(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def insert_token(
    name="test-token",
    plaintext="live-plaintext",
    status="active",
    expires_at=None,
    warehouse_ids=None,
    event_types=None,
    endpoints=None,
    connector_id=None,
):
    """Insert a wms_tokens row via autocommit so the row is visible to
    the test's own connection AND the decorator's fresh session.

    expires_at=None means "use the migration-023 default" (~1 year out).
    Pass an explicit datetime to override (e.g. a past-dated value for
    the expired-token decorator test).
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        if expires_at is None:
            cur.execute(
                """
                INSERT INTO wms_tokens (
                    token_name, token_hash, status,
                    warehouse_ids, event_types, endpoints, connector_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING token_id
                """,
                (
                    name,
                    sha256_with_pepper(plaintext),
                    status,
                    warehouse_ids or [1],
                    event_types or [],
                    endpoints or [],
                    connector_id,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO wms_tokens (
                    token_name, token_hash, status,
                    warehouse_ids, event_types, endpoints, connector_id,
                    expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING token_id
                """,
                (
                    name,
                    sha256_with_pepper(plaintext),
                    status,
                    warehouse_ids or [1],
                    event_types or [],
                    endpoints or [],
                    connector_id,
                    expires_at,
                ),
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
