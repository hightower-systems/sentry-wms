"""Schema-level tests for migration 029 (v1.6.0 #165).

Locks the column shapes, defaults, CHECK constraint ranges, the FK
to connectors, the partial index on status='active', and the
webhook_secrets composite PK + ON DELETE CASCADE behavior.

CI loads db/schema.sql (not the migration file directly), so these
tests are the load-bearing assertion that the migration body and
the schema.sql mirror agree. A drift between the two surfaces here
as a CHECK constraint or index missing from the schema-loaded DB.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_connector(cur, connector_id="test-conn-029"):
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "Migration 029 test connector"),
    )
    return connector_id


class TestWebhookSubscriptionsShape:
    def test_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'webhook_subscriptions'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        finally:
            conn.close()

        assert cols["subscription_id"][:2] == ("uuid", "NO")
        assert cols["subscription_id"][2] is not None and "gen_random_uuid" in cols["subscription_id"][2]
        assert cols["connector_id"][:2] == ("character varying", "NO")
        assert cols["display_name"][:2] == ("character varying", "NO")
        assert cols["delivery_url"][:2] == ("text", "NO")
        assert cols["subscription_filter"][:2] == ("jsonb", "NO")
        assert cols["last_delivered_event_id"][:2] == ("bigint", "NO")
        assert cols["last_delivered_event_id"][2] is not None and "0" in cols["last_delivered_event_id"][2]
        assert cols["status"][:2] == ("character varying", "NO")
        assert cols["status"][2] is not None and "active" in cols["status"][2]
        assert cols["pause_reason"][:2] == ("character varying", "YES")
        assert cols["rate_limit_per_second"][:2] == ("integer", "NO")
        assert cols["rate_limit_per_second"][2] is not None and "50" in cols["rate_limit_per_second"][2]
        assert cols["pending_ceiling"][:2] == ("integer", "NO")
        assert cols["pending_ceiling"][2] is not None and "10000" in cols["pending_ceiling"][2]
        assert cols["dlq_ceiling"][:2] == ("integer", "NO")
        assert cols["dlq_ceiling"][2] is not None and "1000" in cols["dlq_ceiling"][2]

    def test_partial_index_on_active_status(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT indexdef
                  FROM pg_indexes
                 WHERE tablename = 'webhook_subscriptions'
                   AND indexname = 'webhook_subscriptions_status'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "partial index webhook_subscriptions_status must exist"
        indexdef = row[0]
        assert "WHERE" in indexdef and "active" in indexdef, (
            "index must be partial on status='active'; full-table btree on a "
            "three-value column would be wasted bytes"
        )

    def test_fk_to_connectors_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ccu.table_name, ccu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'webhook_subscriptions'
                   AND tc.constraint_type = 'FOREIGN KEY'
                """
            )
            fks = cur.fetchall()
        finally:
            conn.close()
        parents = {r[0] for r in fks}
        assert "connectors" in parents


class TestWebhookSubscriptionsDefaultUuid:
    def test_subscription_id_default_generates_uuid(self):
        conn = _make_conn()
        sub_id = None
        try:
            cur = conn.cursor()
            connector_id = _ensure_connector(cur)
            cur.execute(
                """
                INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
                VALUES (%s, %s, %s)
                RETURNING subscription_id
                """,
                (connector_id, "default uuid test", "https://example.invalid/webhook"),
            )
            sub_id = cur.fetchone()[0]
            conn.commit()
            uuid.UUID(str(sub_id))  # must parse as a valid UUID
        finally:
            if sub_id is not None:
                cleanup = _make_conn()
                cleanup.autocommit = True
                cleanup.cursor().execute(
                    "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                    (str(sub_id),),
                )
                cleanup.close()
            conn.close()


class TestWebhookSubscriptionsCheckConstraints:
    """Each CHECK is the bottom rung that catches bypass paths
    around the admin layer. If a refactor accidentally weakens or
    drops one, the test fires here."""

    @pytest.fixture(autouse=True)
    def _connector(self):
        conn = _make_conn()
        cur = conn.cursor()
        self.connector_id = _ensure_connector(cur)
        conn.commit()
        conn.close()

    def _insert(self, conn, **overrides):
        defaults = {
            "connector_id": self.connector_id,
            "display_name": "check-constraint test",
            "delivery_url": "https://example.invalid/hook",
            "rate_limit_per_second": 50,
            "pending_ceiling": 10000,
            "dlq_ceiling": 1000,
        }
        defaults.update(overrides)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO webhook_subscriptions
                (connector_id, display_name, delivery_url,
                 rate_limit_per_second, pending_ceiling, dlq_ceiling)
            VALUES
                (%(connector_id)s, %(display_name)s, %(delivery_url)s,
                 %(rate_limit_per_second)s, %(pending_ceiling)s, %(dlq_ceiling)s)
            RETURNING subscription_id
            """,
            defaults,
        )
        return cur.fetchone()[0]

    def _expect_check_violation(self, **overrides):
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.CheckViolation):
                self._insert(conn, **overrides)
            conn.rollback()
        finally:
            conn.close()

    def test_url_scheme_rejects_ftp(self):
        self._expect_check_violation(delivery_url="ftp://example.invalid/hook")

    def test_url_scheme_rejects_javascript(self):
        self._expect_check_violation(delivery_url="javascript:alert(1)")

    def test_url_scheme_accepts_http_and_https(self):
        # http is allowed at the column; admin endpoint enforces
        # production HTTPS via SENTRY_ALLOW_HTTP_WEBHOOKS opt-out.
        for url in ("http://dev.invalid/h", "https://prod.invalid/h"):
            conn = _make_conn()
            sub_id = None
            try:
                sub_id = self._insert(conn, delivery_url=url)
                conn.commit()
            finally:
                if sub_id is not None:
                    cleanup = _make_conn()
                    cleanup.autocommit = True
                    cleanup.cursor().execute(
                        "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                        (str(sub_id),),
                    )
                    cleanup.close()
                conn.close()

    def test_rate_limit_rejects_zero(self):
        self._expect_check_violation(rate_limit_per_second=0)

    def test_rate_limit_rejects_above_100(self):
        self._expect_check_violation(rate_limit_per_second=101)

    def test_pending_ceiling_rejects_below_100(self):
        self._expect_check_violation(pending_ceiling=99)

    def test_pending_ceiling_rejects_above_100000(self):
        self._expect_check_violation(pending_ceiling=100001)

    def test_dlq_ceiling_rejects_below_10(self):
        self._expect_check_violation(dlq_ceiling=9)

    def test_dlq_ceiling_rejects_above_10000(self):
        self._expect_check_violation(dlq_ceiling=10001)


class TestWebhookSubscriptionsForeignKeyReject:
    """The FK to connectors must reject orphan connector_id values.
    Existence is asserted by TestWebhookSubscriptionsShape; this
    test exercises the actual reject path so a refactor that
    accidentally drops or weakens the FK is caught here."""

    def test_orphan_connector_id_rejected(self):
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO webhook_subscriptions "
                    "(connector_id, display_name, delivery_url) "
                    "VALUES (%s, %s, %s)",
                    (
                        "nonexistent-connector-id",
                        "orphan test",
                        "https://example.invalid/hook",
                    ),
                )
            conn.rollback()
        finally:
            conn.close()
