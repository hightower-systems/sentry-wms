"""Schema-level tests for migration 020 (v1.5.0).

These tests lock in the structural invariants of the integration_events
outbox: the table and four indexes exist, the idempotency UNIQUE
constraint rejects duplicate (aggregate_type, aggregate_id, event_type,
source_txn_id) tuples, and the deferred visible_at trigger sets the
column at COMMIT time so readers can rely on commit order instead of
event_id order.

Uses raw psycopg2 connections (not the Flask test client) because the
commit-ordering test spans two concurrent transactions; the conftest's
savepoint-per-test wrapper cannot represent that shape.
"""

import os
import sys
import threading
import uuid
from time import sleep

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _emit(cur, aggregate_id, event_type="test.event", source_txn_id=None):
    """Helper: INSERT one row into integration_events using the caller's cursor."""
    cur.execute(
        """
        INSERT INTO integration_events (
            event_type, event_version, aggregate_type, aggregate_id,
            aggregate_external_id, warehouse_id, source_txn_id, payload
        ) VALUES (%s, 1, 'test_aggregate', %s, %s, 1, %s, '{}'::jsonb)
        RETURNING event_id, visible_at
        """,
        (
            event_type,
            aggregate_id,
            str(uuid.uuid4()),
            str(source_txn_id or uuid.uuid4()),
        ),
    )
    return cur.fetchone()


class TestSchemaShape:
    def test_integration_events_table_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'integration_events'
                 ORDER BY ordinal_position
                """
            )
            columns = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        finally:
            conn.close()

        expected = {
            "event_id": ("bigint", "NO"),
            "event_type": ("character varying", "NO"),
            "event_version": ("smallint", "NO"),
            "event_timestamp": ("timestamp with time zone", "NO"),
            "aggregate_type": ("character varying", "NO"),
            "aggregate_id": ("bigint", "NO"),
            "aggregate_external_id": ("uuid", "NO"),
            "warehouse_id": ("integer", "NO"),
            "source_txn_id": ("uuid", "NO"),
            "visible_at": ("timestamp with time zone", "YES"),
            "payload": ("jsonb", "NO"),
        }
        for name, (dtype, nullable) in expected.items():
            assert name in columns, f"integration_events missing column {name}"
            assert columns[name] == (dtype, nullable), (
                f"integration_events.{name} has {columns[name]}, expected ({dtype}, {nullable})"
            )

    def test_expected_indexes_exist(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'integration_events'"
            )
            indexes = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

        # PK index is named by Postgres, variable; assert the explicit index names
        # we care about.
        for expected in (
            "ix_integration_events_warehouse_event",
            "ix_integration_events_type_event",
            "ix_integration_events_visible_at",
            "integration_events_idempotency_key",
        ):
            assert expected in indexes, (
                f"expected index/constraint {expected} on integration_events, found {indexes}"
            )

    def test_deferred_visible_at_trigger_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tgname, tgdeferrable, tginitdeferred
                  FROM pg_trigger
                 WHERE tgrelid = 'integration_events'::regclass
                   AND tgname = 'tr_integration_events_visible_at'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()

        assert row is not None, (
            "tr_integration_events_visible_at trigger must exist on integration_events"
        )
        name, deferrable, initdeferred = row
        assert deferrable and initdeferred, (
            "tr_integration_events_visible_at must be DEFERRABLE INITIALLY DEFERRED"
        )


class TestIdempotencyConstraint:
    def test_duplicate_source_txn_id_rejected(self):
        """(aggregate_type, aggregate_id, event_type, source_txn_id) must be unique.

        This is the idempotent-replay key emit_event relies on: a retried
        handler for the same request reuses the same source_txn_id and
        lands a no-op instead of a duplicate event.
        """
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            aggregate_id = 99_999_001
            source_txn_id = uuid.uuid4()

            _emit(cur, aggregate_id=aggregate_id, source_txn_id=source_txn_id)

            with pytest.raises(psycopg2.errors.UniqueViolation):
                _emit(cur, aggregate_id=aggregate_id, source_txn_id=source_txn_id)
        finally:
            # Clean up so re-runs stay isolated.
            cur2 = conn.cursor()
            cur2.execute(
                "DELETE FROM integration_events WHERE aggregate_id = %s",
                (aggregate_id,),
            )
            conn.close()

    def test_same_source_txn_id_with_different_event_type_allowed(self):
        """Retried request may emit multiple distinct event types with the
        same source_txn_id (one transaction, many events). Only the full
        four-tuple is constrained."""
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            aggregate_id = 99_999_002
            source_txn_id = uuid.uuid4()

            _emit(cur, aggregate_id=aggregate_id, event_type="a.x", source_txn_id=source_txn_id)
            _emit(cur, aggregate_id=aggregate_id, event_type="a.y", source_txn_id=source_txn_id)
        finally:
            cur2 = conn.cursor()
            cur2.execute(
                "DELETE FROM integration_events WHERE aggregate_id = %s",
                (aggregate_id,),
            )
            conn.close()


class TestVisibleAtTrigger:
    def test_visible_at_is_null_before_commit_and_set_after(self):
        """The trigger is AFTER INSERT DEFERRABLE INITIALLY DEFERRED, so the
        row is visible inside the transaction with visible_at still NULL;
        COMMIT fires the trigger and sets visible_at."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            aggregate_id = 99_999_101

            cur.execute("BEGIN")
            event_id, visible_at_pre_commit = _emit(cur, aggregate_id=aggregate_id)
            assert visible_at_pre_commit is None, (
                "visible_at must be NULL before COMMIT; trigger is DEFERRED"
            )

            conn.commit()

            cur.execute(
                "SELECT visible_at FROM integration_events WHERE event_id = %s",
                (event_id,),
            )
            visible_at_post_commit = cur.fetchone()[0]
            assert visible_at_post_commit is not None, (
                "visible_at must be set by the deferred trigger at COMMIT"
            )
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cur2 = cleanup.cursor()
            cur2.execute(
                "DELETE FROM integration_events WHERE aggregate_id = %s",
                (aggregate_id,),
            )
            cleanup.close()
            conn.close()

    def test_visible_at_respects_commit_order_across_sessions(self):
        """Two sessions emit on the same aggregate; the later-committing one
        gets the later visible_at even if its event_id came first.

        This is the whole point of the deferred trigger. BIGSERIAL allocates
        event_ids at INSERT time; commit order is what a reader cares about.
        """
        conn_a = _make_conn()
        conn_b = _make_conn()
        try:
            cur_a = conn_a.cursor()
            cur_b = conn_b.cursor()
            aggregate_id = 99_999_201

            cur_a.execute("BEGIN")
            event_a_id, _ = _emit(cur_a, aggregate_id=aggregate_id, event_type="order.first_insert")

            cur_b.execute("BEGIN")
            event_b_id, _ = _emit(cur_b, aggregate_id=aggregate_id, event_type="order.second_insert")

            # Both event_ids are allocated now; event_a_id < event_b_id.
            assert event_a_id < event_b_id

            # Commit B first. Its visible_at stamp comes from this commit.
            conn_b.commit()
            # Small gap so A's clock_timestamp() lands after B's even on
            # clocks with coarse resolution.
            sleep(0.01)
            conn_a.commit()

            check = _make_conn()
            try:
                ccur = check.cursor()
                ccur.execute(
                    """
                    SELECT event_id, visible_at
                      FROM integration_events
                     WHERE aggregate_id = %s
                     ORDER BY event_id
                    """,
                    (aggregate_id,),
                )
                rows = ccur.fetchall()
                assert len(rows) == 2
                (a_id, a_vis), (b_id, b_vis) = rows
                assert a_id == event_a_id and b_id == event_b_id
                assert a_vis is not None and b_vis is not None
                # A committed after B, so A's visible_at is newer than B's.
                assert a_vis > b_vis, (
                    f"expected A (committed second) to have visible_at > B "
                    f"(committed first); got A={a_vis} B={b_vis}"
                )
            finally:
                check.close()
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cur2 = cleanup.cursor()
            cur2.execute(
                "DELETE FROM integration_events WHERE aggregate_id = %s",
                (aggregate_id,),
            )
            cleanup.close()
            conn_a.close()
            conn_b.close()


class TestExternalIdRetrofit:
    """The ten aggregate/actor tables carry an external_id UUID column
    post-migration 025: UUID, UNIQUE, NOT NULL, and NO DEFAULT. Every
    INSERT site (production routes, test fixtures, seed SQL) supplies
    a value explicitly via uuid.uuid4() in Python or gen_random_uuid()
    in SQL. A callsite that forgets the column now fails with a NOT
    NULL violation; the CI guardrail in test_external_id_inserts.py
    catches it during review.
    """

    RETROFITTED_TABLES = [
        "users",
        "items",
        "bins",
        "sales_orders",
        "purchase_orders",
        "item_receipts",
        "inventory_adjustments",
        "bin_transfers",
        "cycle_counts",
        "item_fulfillments",
    ]

    def test_every_retrofitted_table_has_external_id_column(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            for table in self.RETROFITTED_TABLES:
                cur.execute(
                    """
                    SELECT data_type, is_nullable, column_default
                      FROM information_schema.columns
                     WHERE table_name = %s AND column_name = 'external_id'
                    """,
                    (table,),
                )
                row = cur.fetchone()
                assert row is not None, f"{table}.external_id column missing"
                dtype, nullable, default = row
                assert dtype == "uuid", f"{table}.external_id must be UUID, got {dtype}"
                assert nullable == "NO", f"{table}.external_id must be NOT NULL"
                assert default is None, (
                    f"{table}.external_id must have no DEFAULT post-migration 025; "
                    f"got {default!r}"
                )
        finally:
            conn.close()

    def test_seed_rows_carry_unique_non_null_external_ids(self):
        """Every seed row supplies external_id via gen_random_uuid() inline;
        values must be non-NULL and unique per table."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            for table in self.RETROFITTED_TABLES:
                cur.execute(
                    f"SELECT COUNT(*), COUNT(external_id), COUNT(DISTINCT external_id) FROM {table}"
                )
                total, non_null, distinct = cur.fetchone()
                if total == 0:
                    continue  # seed did not populate this table, nothing to assert
                assert non_null == total, (
                    f"{table}: {total - non_null} of {total} rows have NULL external_id"
                )
                assert distinct == total, (
                    f"{table}: {total - distinct} of {total} rows share an external_id"
                )
        finally:
            conn.close()
