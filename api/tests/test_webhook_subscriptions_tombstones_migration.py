"""Schema-level tests for migration 033 (v1.6.0 #168).

Locks the column shapes, the FKs to users on deleted_by +
acknowledged_by, the BIGSERIAL on tombstone_id, and the partial
index that backs the URL-reuse gate query path.

CI loads db/schema.sql, so these tests are the load-bearing
assertion that the migration body and the schema.sql mirror
agree -- a missing partial-index WHERE clause would silently
turn the URL-reuse gate into a full-table TEXT scan on every
webhook create.
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


def _admin_user_id(cur):
    """Return the admin user_id; the seed always inserts an admin
    user as the first row, so user_id=1 is reliable across the
    full-seed and SKIP_SEED paths."""
    cur.execute("SELECT user_id FROM users WHERE role = 'ADMIN' ORDER BY user_id LIMIT 1")
    row = cur.fetchone()
    assert row is not None, "expected at least one ADMIN user; seed should always create one"
    return row[0]


class TestWebhookSubscriptionsTombstonesShape:
    def test_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'webhook_subscriptions_tombstones'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()

        assert cols["tombstone_id"] == ("bigint", "NO")
        assert cols["subscription_id"] == ("uuid", "NO")
        assert cols["delivery_url_at_delete"] == ("text", "NO")
        assert cols["connector_id"] == ("character varying", "NO")
        assert cols["deleted_at"] == ("timestamp with time zone", "NO")
        assert cols["deleted_by"] == ("integer", "NO")
        assert cols["acknowledged_at"] == ("timestamp with time zone", "YES")
        assert cols["acknowledged_by"] == ("integer", "YES")

    def test_partial_index_on_unacknowledged_url(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT indexdef FROM pg_indexes
                 WHERE tablename = 'webhook_subscriptions_tombstones'
                   AND indexname = 'webhook_subscriptions_tombstones_url_unack'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, (
            "partial index on (delivery_url_at_delete) WHERE acknowledged_at IS NULL "
            "must exist; full-table btree on the TEXT column would scan acknowledged "
            "rows on every webhook create"
        )
        idef = row[0]
        assert "delivery_url_at_delete" in idef
        assert "WHERE" in idef
        assert "acknowledged_at IS NULL" in idef, (
            "the partial index predicate is the load-bearing part: a refactor "
            "that drops the WHERE clause would silently double the gate query "
            "cost as acknowledged tombstones accumulate"
        )

    def test_fks_to_users_on_deleted_by_and_acknowledged_by(self):
        """Both deleted_by and acknowledged_by FK to users(user_id);
        deleted_by NOT NULL, acknowledged_by nullable. The two
        columns can be different users (a two-admin recreate flow)."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT kcu.column_name, ccu.table_name, ccu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'webhook_subscriptions_tombstones'
                   AND tc.constraint_type = 'FOREIGN KEY'
                """
            )
            fks = cur.fetchall()
        finally:
            conn.close()
        local_to_target = {r[0]: (r[1], r[2]) for r in fks}
        assert local_to_target.get("deleted_by") == ("users", "user_id")
        assert local_to_target.get("acknowledged_by") == ("users", "user_id")


class TestWebhookSubscriptionsTombstonesBigSerial:
    def test_tombstone_id_increases_monotonically(self):
        conn = _make_conn()
        ids = []
        try:
            cur = conn.cursor()
            admin_id = _admin_user_id(cur)
            for i in range(3):
                cur.execute(
                    """
                    INSERT INTO webhook_subscriptions_tombstones
                        (subscription_id, delivery_url_at_delete, connector_id, deleted_by)
                    VALUES (%s, %s, %s, %s)
                    RETURNING tombstone_id
                    """,
                    (
                        str(uuid.uuid4()),
                        f"https://example.invalid/bigserial-{i}",
                        "test-conn-033",
                        admin_id,
                    ),
                )
                ids.append(cur.fetchone()[0])
            conn.commit()
            assert ids == sorted(ids), "BIGSERIAL must produce monotonically increasing tombstone_ids"
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM webhook_subscriptions_tombstones WHERE tombstone_id = ANY(%s)",
                (ids,),
            )
            cleanup.close()
            conn.close()


class TestWebhookSubscriptionsTombstonesForeignKeyReject:
    def test_orphan_deleted_by_rejected(self):
        bogus_user = 999_999_999
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO webhook_subscriptions_tombstones
                        (subscription_id, delivery_url_at_delete, connector_id, deleted_by)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        "https://example.invalid/orphan-deleted-by",
                        "test-conn-033",
                        bogus_user,
                    ),
                )
            conn.rollback()
        finally:
            conn.close()

    def test_orphan_acknowledged_by_rejected(self):
        bogus_user = 999_999_998
        conn = _make_conn()
        try:
            cur = conn.cursor()
            admin_id = _admin_user_id(cur)
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute(
                    """
                    INSERT INTO webhook_subscriptions_tombstones
                        (subscription_id, delivery_url_at_delete, connector_id,
                         deleted_by, acknowledged_at, acknowledged_by)
                    VALUES (%s, %s, %s, %s, NOW(), %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        "https://example.invalid/orphan-ack-by",
                        "test-conn-033",
                        admin_id,
                        bogus_user,
                    ),
                )
            conn.rollback()
        finally:
            conn.close()


class TestWebhookSubscriptionsTombstonesAcknowledgedNullableInsert:
    """A tombstone is written with acknowledged_at + acknowledged_by
    NULL; the URL-reuse gate query depends on the NULL gate."""

    def test_insert_without_acknowledgement_succeeds(self):
        conn = _make_conn()
        tombstone_id = None
        try:
            cur = conn.cursor()
            admin_id = _admin_user_id(cur)
            cur.execute(
                """
                INSERT INTO webhook_subscriptions_tombstones
                    (subscription_id, delivery_url_at_delete, connector_id, deleted_by)
                VALUES (%s, %s, %s, %s)
                RETURNING tombstone_id, acknowledged_at, acknowledged_by
                """,
                (
                    str(uuid.uuid4()),
                    "https://example.invalid/unack",
                    "test-conn-033",
                    admin_id,
                ),
            )
            tombstone_id, ack_at, ack_by = cur.fetchone()
            conn.commit()
            assert ack_at is None
            assert ack_by is None
        finally:
            if tombstone_id is not None:
                cleanup = _make_conn()
                cleanup.autocommit = True
                cleanup.cursor().execute(
                    "DELETE FROM webhook_subscriptions_tombstones WHERE tombstone_id = %s",
                    (tombstone_id,),
                )
                cleanup.close()
            conn.close()


class TestWebhookSubscriptionsTombstonesPartialIndexBehavior:
    """The partial index covers only unacknowledged tombstones.
    Once acknowledged, the row remains in the table for forensic
    history but no longer appears via the partial index path."""

    def test_acknowledged_rows_drop_from_partial_index(self):
        conn = _make_conn()
        tombstone_id = None
        try:
            cur = conn.cursor()
            admin_id = _admin_user_id(cur)
            url = "https://example.invalid/partial-index"

            cur.execute(
                """
                INSERT INTO webhook_subscriptions_tombstones
                    (subscription_id, delivery_url_at_delete, connector_id, deleted_by)
                VALUES (%s, %s, %s, %s)
                RETURNING tombstone_id
                """,
                (str(uuid.uuid4()), url, "test-conn-033", admin_id),
            )
            tombstone_id = cur.fetchone()[0]
            conn.commit()

            # Gate query: unacknowledged tombstones for this URL.
            cur.execute(
                "SELECT COUNT(*) FROM webhook_subscriptions_tombstones "
                "WHERE delivery_url_at_delete = %s AND acknowledged_at IS NULL",
                (url,),
            )
            assert cur.fetchone()[0] == 1

            # Acknowledge the row.
            cur.execute(
                """
                UPDATE webhook_subscriptions_tombstones
                   SET acknowledged_at = NOW(), acknowledged_by = %s
                 WHERE tombstone_id = %s
                """,
                (admin_id, tombstone_id),
            )
            conn.commit()

            # Same gate query now finds zero -- the row is still
            # in the table but no longer participates in the URL-
            # reuse gate.
            cur.execute(
                "SELECT COUNT(*) FROM webhook_subscriptions_tombstones "
                "WHERE delivery_url_at_delete = %s AND acknowledged_at IS NULL",
                (url,),
            )
            assert cur.fetchone()[0] == 0

            # Forensic history: row still readable without the gate predicate.
            cur.execute(
                "SELECT COUNT(*) FROM webhook_subscriptions_tombstones WHERE tombstone_id = %s",
                (tombstone_id,),
            )
            assert cur.fetchone()[0] == 1, (
                "acknowledged tombstones must remain in the table for forensic "
                "history; only the partial-index participation drops"
            )
        finally:
            if tombstone_id is not None:
                cleanup = _make_conn()
                cleanup.autocommit = True
                cleanup.cursor().execute(
                    "DELETE FROM webhook_subscriptions_tombstones WHERE tombstone_id = %s",
                    (tombstone_id,),
                )
                cleanup.close()
            conn.close()
