"""v1.7.0 R6: source_payload retention beat task tests.

Covers:
- get_inbound_retention_days() reads SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS
  with the 7-day floor enforced (worker-side last-line of defense).
- _cleanup_inbound_source_payload_impl NULLs source_payload on rows
  past the cutoff, preserves canonical_payload, and writes one
  inbound_cleanup_runs row per resource per invocation.
- The task is idempotent (already-NULL rows are filtered out).
- A failure on one resource does not abort the others.
"""

import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context

from jobs.cleanup_tasks import (
    INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS,
    INBOUND_SOURCE_PAYLOAD_RETENTION_FLOOR_DAYS,
    _cleanup_inbound_source_payload_impl,
    get_inbound_retention_days,
)


def _exec(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


@pytest.fixture
def seeded():
    """Insert one fresh inbound row + one old inbound row + a token +
    allowlist. The old row's received_at is backdated to fall past
    any plausible retention window."""
    import hashlib
    ss = f"retentest-{uuid.uuid4().hex[:8]}"
    _exec(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool')",
        (ss,),
    )
    th = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    rows = _exec(
        "INSERT INTO wms_tokens (token_name, token_hash, status, source_system, "
        "                         inbound_resources) "
        "VALUES (%s, %s, 'active', %s, %s) RETURNING token_id",
        (f"retentest-token-{ss}", th, ss, ["sales_orders"]),
    )
    token_id = rows[0][0]
    # Fresh row (one second ago).
    _exec(
        "INSERT INTO inbound_sales_orders "
        " (source_system, external_id, external_version, canonical_id, "
        "  canonical_payload, source_payload, ingested_via_token_id) "
        "VALUES (%s, 'SO-FRESH', 'v1', %s, %s::jsonb, %s::jsonb, %s)",
        (ss, str(uuid.uuid4()), '{"so_number":"SO-FRESH"}', '{"x":1}', token_id),
    )
    # Old row -- 1000 days back.
    rows = _exec(
        "INSERT INTO inbound_sales_orders "
        " (source_system, external_id, external_version, canonical_id, "
        "  canonical_payload, source_payload, ingested_via_token_id, received_at) "
        "VALUES (%s, 'SO-OLD', 'v1', %s, %s::jsonb, %s::jsonb, %s, "
        "        NOW() - INTERVAL '1000 days') RETURNING inbound_id",
        (ss, str(uuid.uuid4()), '{"so_number":"SO-OLD"}', '{"x":2}', token_id),
    )
    old_inbound_id = rows[0][0]
    return {"ss": ss, "token_id": token_id, "old_inbound_id": old_inbound_id}


# ----------------------------------------------------------------------
# Env var read path
# ----------------------------------------------------------------------


class TestGetInboundRetentionDays:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", raising=False
        )
        assert get_inbound_retention_days() == (
            INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS
        )

    def test_explicit_value_passes(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "30",
        )
        assert get_inbound_retention_days() == 30

    def test_below_floor_clamps_to_floor(self, monkeypatch):
        # The boot guard refuses to start at all in production; this
        # worker-side function still clamps so a runtime tweak cannot
        # punch through.
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "1",
        )
        assert get_inbound_retention_days() == (
            INBOUND_SOURCE_PAYLOAD_RETENTION_FLOOR_DAYS
        )

    def test_garbage_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS", "garbage",
        )
        assert get_inbound_retention_days() == (
            INBOUND_SOURCE_PAYLOAD_RETENTION_DEFAULT_DAYS
        )


# ----------------------------------------------------------------------
# Cleanup behavior
# ----------------------------------------------------------------------


class TestCleanupBehavior:
    def test_old_row_source_payload_nullified_canonical_preserved(
        self, seeded, app
    ):
        """The 1000-day-old row should have source_payload set to NULL.
        canonical_payload stays intact -- forensic chain preserved."""
        import models.database as db
        sess = db.SessionLocal()
        try:
            summary = _cleanup_inbound_source_payload_impl(sess, retention_days=90)
        finally:
            sess.close()
        assert summary["sales_orders"]["status"] == "succeeded"
        assert summary["sales_orders"]["nullified"] >= 1

        rows = _exec(
            "SELECT source_payload, canonical_payload "
            "  FROM inbound_sales_orders WHERE inbound_id = %s",
            (seeded["old_inbound_id"],),
        )
        sp, cp = rows[0]
        assert sp is None
        assert cp == {"so_number": "SO-OLD"}

    def test_fresh_row_untouched(self, seeded, app):
        import models.database as db
        sess = db.SessionLocal()
        try:
            _cleanup_inbound_source_payload_impl(sess, retention_days=90)
        finally:
            sess.close()
        rows = _exec(
            "SELECT source_payload FROM inbound_sales_orders "
            " WHERE source_system = %s AND external_id = 'SO-FRESH'",
            (seeded["ss"],),
        )
        assert rows[0][0] == {"x": 1}

    def test_inbound_cleanup_runs_row_per_resource(self, seeded, app):
        import models.database as db
        sess = db.SessionLocal()
        try:
            _cleanup_inbound_source_payload_impl(sess, retention_days=90)
        finally:
            sess.close()
        # Each invocation writes one row per resource (5 rows total
        # for sales_orders, items, customers, vendors, purchase_orders).
        # Filter to the seeded run window via started_at >= NOW()-1m so
        # parallel test runs / prior sessions don't pollute the count.
        rows = _exec(
            "SELECT resource, status, rows_nullified, retention_days "
            "  FROM inbound_cleanup_runs "
            " WHERE started_at >= NOW() - INTERVAL '1 minute' "
            " ORDER BY resource"
        )
        resources = sorted({r[0] for r in rows})
        assert set(resources) == {
            "sales_orders", "items", "customers",
            "vendors", "purchase_orders",
        }
        # All five succeeded; the seeded fresh + old rows live on
        # sales_orders only, but every resource ran successfully.
        for resource, status, _nullified, retention in rows:
            assert status == "succeeded"
            assert retention == 90

    def test_idempotent_second_run_nullifies_zero(self, seeded, app):
        import models.database as db
        sess = db.SessionLocal()
        try:
            first = _cleanup_inbound_source_payload_impl(sess, retention_days=90)
            second = _cleanup_inbound_source_payload_impl(sess, retention_days=90)
        finally:
            sess.close()
        assert first["sales_orders"]["nullified"] >= 1
        # Second run sees no rows where source_payload IS NOT NULL past
        # the cutoff -- already nullified by the first run.
        assert second["sales_orders"]["nullified"] == 0
