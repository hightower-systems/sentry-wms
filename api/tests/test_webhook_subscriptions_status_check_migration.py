"""Schema-level tests for migration 036 (#236).

Locks the column-level CHECK constraints on
webhook_subscriptions.status + pause_reason so a privileged-role
error or malicious migration cannot write an out-of-band value.
The application layer rejects the same values via Pydantic and
the auto-pause helpers; this is the bottom rung.

CI loads db/schema.sql, so these tests are the load-bearing
assertion that the migration body and the schema.sql mirror
agree -- a missing CHECK predicate would silently re-open the
gap on the column.
"""

import os
import sys
import uuid

os.environ.setdefault(
    "DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry"
)
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault(
    "SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8="
)
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_connector(cur, connector_id="test-conn-036"):
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "Migration 036 status-CHECK test connector"),
    )
    return connector_id


def _make_subscription(status="active", pause_reason=None):
    """Insert a subscription with the requested status/pause_reason
    via raw SQL. Returns (sub_id, cleanup)."""
    conn = _make_conn()
    conn.autocommit = True
    cur = conn.cursor()
    _ensure_connector(cur)
    cur.execute(
        """
        INSERT INTO webhook_subscriptions
            (connector_id, display_name, delivery_url, status, pause_reason)
        VALUES ('test-conn-036', %s, %s, %s, %s)
        RETURNING subscription_id
        """,
        (
            f"036-status-{uuid.uuid4().hex[:8]}",
            f"https://example.invalid/{uuid.uuid4()}",
            status,
            pause_reason,
        ),
    )
    sub_id = cur.fetchone()[0]
    conn.close()

    def cleanup():
        c = _make_conn()
        c.autocommit = True
        c.cursor().execute(
            "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
            (str(sub_id),),
        )
        c.close()

    return sub_id, cleanup


class TestStatusCheckConstraint:
    """status IN ('active','paused','revoked') is enforced at
    the column level. The pre-#236 column accepted any 16-char
    string."""

    @pytest.mark.parametrize(
        "status",
        ["pwn", "ACTIVE", "Pause", "", " active "],
    )
    def test_invalid_status_rejected(self, status):
        with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
            _, _ = _make_subscription(status=status)
        assert "webhook_subscriptions_status_enum" in str(excinfo.value)

    @pytest.mark.parametrize(
        "status", ["active", "paused", "revoked"]
    )
    def test_valid_status_accepted(self, status):
        sub_id, cleanup = _make_subscription(status=status)
        try:
            assert sub_id is not None
        finally:
            cleanup()

    def test_update_to_invalid_status_rejected(self):
        sub_id, cleanup = _make_subscription(status="active")
        try:
            conn = _make_conn()
            conn.autocommit = True
            cur = conn.cursor()
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "UPDATE webhook_subscriptions "
                    "   SET status = 'sneaky' "
                    " WHERE subscription_id = %s",
                    (str(sub_id),),
                )
            conn.close()
        finally:
            cleanup()


class TestPauseReasonCheckConstraint:
    """pause_reason IS NULL OR IN
    ('manual','pending_ceiling','dlq_ceiling','malformed_filter').
    Nullable because an active subscription has no pause_reason."""

    @pytest.mark.parametrize(
        "pause_reason",
        ["unknown", "MANUAL", "pause", "auto"],
    )
    def test_invalid_pause_reason_rejected(self, pause_reason):
        with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
            _, _ = _make_subscription(
                status="paused", pause_reason=pause_reason
            )
        assert "webhook_subscriptions_pause_reason_enum" in str(excinfo.value)

    @pytest.mark.parametrize(
        "pause_reason",
        ["manual", "pending_ceiling", "dlq_ceiling", "malformed_filter"],
    )
    def test_valid_pause_reason_accepted(self, pause_reason):
        sub_id, cleanup = _make_subscription(
            status="paused", pause_reason=pause_reason
        )
        try:
            assert sub_id is not None
        finally:
            cleanup()

    def test_null_pause_reason_accepted(self):
        sub_id, cleanup = _make_subscription(status="active", pause_reason=None)
        try:
            assert sub_id is not None
        finally:
            cleanup()


class TestConstraintsRegistered:
    """Direct catalog probe so a refactor that drops the named
    CHECK constraint surfaces here, not via a downstream
    integration test failure."""

    @pytest.mark.parametrize(
        "constraint",
        [
            "webhook_subscriptions_status_enum",
            "webhook_subscriptions_pause_reason_enum",
        ],
    )
    def test_constraint_present(self, constraint):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.table_constraints
                 WHERE table_name = 'webhook_subscriptions'
                   AND constraint_name = %s
                   AND constraint_type = 'CHECK'
                """,
                (constraint,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, (
            f"{constraint} CHECK constraint must be present on "
            "webhook_subscriptions; #236 expects bottom-rung "
            "enforcement on status / pause_reason"
        )
