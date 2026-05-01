"""Schema-level tests for migration 030 (v1.6.0 #166).

Locks the column shapes, the CHECK constraints on attempt_number
and status, the RESTRICT FK on subscription_id, the absence of an
event_id FK (deliberate; integration_events partitions in v2.1),
and the four index predicates that the dispatcher and admin
query paths depend on.

CI loads db/schema.sql, so these tests are the load-bearing
assertion that the migration body and the schema.sql mirror agree.
A drift between the two (a missing WHERE clause on a partial
index, a swapped RESTRICT for CASCADE) surfaces here as a
failed assertion.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_connector(cur, connector_id="test-conn-030"):
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "Migration 030 test connector"),
    )
    return connector_id


def _make_subscription():
    """Return (subscription_id, cleanup_fn). The cleanup deletes
    the subscription, which RESTRICT-FKs against any remaining
    delivery rows; tests that insert deliveries are responsible
    for clearing them before calling cleanup."""
    conn = _make_conn()
    conn.autocommit = True
    cur = conn.cursor()
    connector_id = _ensure_connector(cur)
    cur.execute(
        """
        INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
        VALUES (%s, %s, %s)
        RETURNING subscription_id
        """,
        (connector_id, "deliveries test", "https://example.invalid/hook"),
    )
    sub_id = cur.fetchone()[0]
    conn.close()

    def cleanup():
        c = _make_conn()
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "DELETE FROM webhook_deliveries WHERE subscription_id = %s",
            (str(sub_id),),
        )
        cur.execute(
            "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
            (str(sub_id),),
        )
        c.close()

    return sub_id, cleanup


class TestWebhookDeliveriesShape:
    def test_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'webhook_deliveries'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()

        assert cols["delivery_id"] == ("bigint", "NO")
        assert cols["subscription_id"] == ("uuid", "NO")
        assert cols["event_id"] == ("bigint", "NO")
        assert cols["attempt_number"] == ("smallint", "NO")
        assert cols["status"] == ("character varying", "NO")
        assert cols["scheduled_at"] == ("timestamp with time zone", "NO")
        assert cols["attempted_at"] == ("timestamp with time zone", "YES")
        assert cols["completed_at"] == ("timestamp with time zone", "YES")
        assert cols["http_status"] == ("smallint", "YES")
        assert cols["response_body_hash"] == ("character", "YES")
        assert cols["response_time_ms"] == ("integer", "YES")
        assert cols["error_kind"] == ("character varying", "YES")
        assert cols["error_detail"] == ("character varying", "YES")
        assert cols["secret_generation"] == ("smallint", "NO")

    def test_response_body_hash_length(self):
        """sha256 hex is 64 chars; CHAR(64) is the storage shape."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT character_maximum_length
                  FROM information_schema.columns
                 WHERE table_name = 'webhook_deliveries'
                   AND column_name = 'response_body_hash'
                """
            )
            length = cur.fetchone()[0]
        finally:
            conn.close()
        assert length == 64

    def test_event_id_has_no_fk(self):
        """event_id is BIGINT NOT NULL but no FK to integration_events.
        v2.1 partitioning is the documented reason; this test makes
        the deliberate gap loud so a future helpful contributor does
        not silently add the FK and break partitioning later."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ccu.table_name, kcu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'webhook_deliveries'
                   AND tc.constraint_type = 'FOREIGN KEY'
                """
            )
            fks = cur.fetchall()
        finally:
            conn.close()
        local_cols = {r[1] for r in fks}
        assert "event_id" not in local_cols, (
            "event_id must NOT carry an FK to integration_events; "
            "v2.1 partitions integration_events and the FK would block that"
        )


class TestWebhookDeliveriesIndexes:
    """Each index is pinned to a specific query path. The
    predicates are the load-bearing part; a btree without the
    WHERE clause silently bloats the table footprint and slows
    the dispatcher's hot loop."""

    def _indexdef(self, indexname):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'webhook_deliveries' AND indexname = %s",
                (indexname,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def test_dispatch_index_is_partial_on_pending(self):
        idef = self._indexdef("webhook_deliveries_dispatch")
        assert idef is not None
        assert "subscription_id" in idef and "scheduled_at" in idef
        assert "WHERE" in idef and "pending" in idef, (
            "dispatch index must be partial on status='pending'; the full-table "
            "form would scan in_flight + succeeded + failed + dlq rows the "
            "dispatcher does not care about"
        )

    def test_latest_index_has_descending_delivery_id(self):
        idef = self._indexdef("webhook_deliveries_latest")
        assert idef is not None
        assert "subscription_id" in idef and "event_id" in idef
        assert "delivery_id DESC" in idef, (
            "latest index must sort delivery_id DESC so the first hit is the "
            "most recent attempt for a (subscription, event)"
        )
        # No predicate on this index; it covers cross-status lookups.
        assert "WHERE" not in idef.upper().split("(", 1)[1]

    def test_dlq_index_is_partial_on_dlq(self):
        idef = self._indexdef("webhook_deliveries_dlq")
        assert idef is not None
        assert "subscription_id" in idef and "completed_at" in idef
        assert "WHERE" in idef and "dlq" in idef

    def test_pending_count_index_predicate(self):
        idef = self._indexdef("webhook_deliveries_pending_count")
        assert idef is not None
        assert "subscription_id" in idef
        assert "WHERE" in idef
        assert "pending" in idef and "in_flight" in idef, (
            "pending-count index must include both 'pending' and 'in_flight' "
            "so the auto-pause check at the pending ceiling counts correctly"
        )


class TestWebhookDeliveriesAttemptNumberCheck:
    @pytest.fixture(autouse=True)
    def _subscription(self):
        self.sub_id, self._cleanup = _make_subscription()
        yield
        self._cleanup()

    def _insert(self, conn, **overrides):
        defaults = {
            "subscription_id": str(self.sub_id),
            "event_id": 1,
            "attempt_number": 1,
            "status": "pending",
            "scheduled_at": "NOW()",
            "secret_generation": 1,
        }
        defaults.update(overrides)
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, secret_generation)
            VALUES
                (%(subscription_id)s, %(event_id)s, %(attempt_number)s,
                 %(status)s, {defaults['scheduled_at']},
                 %(secret_generation)s)
            """,
            defaults,
        )

    def test_attempt_number_zero_rejected(self):
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.CheckViolation):
                self._insert(conn, attempt_number=0)
            conn.rollback()
        finally:
            conn.close()

    def test_attempt_number_nine_rejected(self):
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.CheckViolation):
                self._insert(conn, attempt_number=9)
            conn.rollback()
        finally:
            conn.close()

    def test_attempt_number_one_and_eight_accepted(self):
        for n in (1, 8):
            conn = _make_conn()
            try:
                self._insert(conn, attempt_number=n, event_id=100 + n)
                conn.commit()
            finally:
                conn.close()


class TestWebhookDeliveriesStatusCheck:
    @pytest.fixture(autouse=True)
    def _subscription(self):
        self.sub_id, self._cleanup = _make_subscription()
        yield
        self._cleanup()

    def _insert_status(self, conn, status, event_id=42):
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, secret_generation)
            VALUES (%s, %s, 1, %s, NOW(), 1)
            """,
            (str(self.sub_id), event_id, status),
        )

    def test_bogus_status_rejected(self):
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.CheckViolation):
                self._insert_status(conn, "bogus")
            conn.rollback()
        finally:
            conn.close()

    def test_each_enumerated_status_accepted(self):
        # One row per enumerated status; distinct event_id keeps inserts
        # independent and lets a future migration add a uniqueness
        # constraint without breaking this test.
        for i, status in enumerate(("pending", "in_flight", "succeeded", "failed", "dlq")):
            conn = _make_conn()
            try:
                self._insert_status(conn, status, event_id=200 + i)
                conn.commit()
            finally:
                conn.close()


class TestWebhookDeliveriesRestrictForeignKey:
    """ON DELETE RESTRICT is the forensic-state-preservation
    invariant: a subscription with active delivery rows cannot
    be hard-deleted, so soft delete (status='revoked') becomes
    the supported path."""

    def test_subscription_delete_blocked_by_delivery_row(self):
        sub_id, cleanup = _make_subscription()
        try:
            conn = _make_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO webhook_deliveries
                        (subscription_id, event_id, attempt_number, status,
                         scheduled_at, secret_generation)
                    VALUES (%s, 1, 1, 'pending', NOW(), 1)
                    """,
                    (str(sub_id),),
                )
                conn.commit()

                with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                    cur.execute(
                        "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                        (str(sub_id),),
                    )
                conn.rollback()
            finally:
                conn.close()
        finally:
            cleanup()


class TestWebhookDeliveriesBigSerial:
    def test_delivery_id_increases_monotonically(self):
        sub_id, cleanup = _make_subscription()
        try:
            conn = _make_conn()
            try:
                cur = conn.cursor()
                ids = []
                for i in range(3):
                    cur.execute(
                        """
                        INSERT INTO webhook_deliveries
                            (subscription_id, event_id, attempt_number, status,
                             scheduled_at, secret_generation)
                        VALUES (%s, %s, 1, 'pending', NOW(), 1)
                        RETURNING delivery_id
                        """,
                        (str(sub_id), 500 + i),
                    )
                    ids.append(cur.fetchone()[0])
                conn.commit()
                assert ids == sorted(ids), "BIGSERIAL must produce monotonically increasing ids"
            finally:
                conn.close()
        finally:
            cleanup()
