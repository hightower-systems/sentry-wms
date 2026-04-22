"""Schema-level tests for migration 024 (v1.5.0 #131).

Locks in the column shapes, the (status, started_at) index, the FK to
wms_tokens, and the NOTIFY trigger. The NOTIFY test opens a dedicated
psycopg2 connection so it can both LISTEN and observe the pg_notify
payload without fighting the fixture's rolled-back outer transaction.
"""

import os
import select
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


class TestSnapshotScansShape:
    def test_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'snapshot_scans'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert cols["scan_id"][:2] == ("uuid", "NO")
        assert cols["pg_snapshot_id"][:2] == ("text", "YES")
        assert cols["snapshot_event_id"][:2] == ("bigint", "YES")
        assert cols["warehouse_id"][:2] == ("integer", "NO")
        assert cols["started_at"][:2] == ("timestamp with time zone", "NO")
        assert cols["last_accessed_at"][:2] == ("timestamp with time zone", "NO")
        status = cols["status"]
        assert status[0] == "character varying"
        assert status[1] == "NO"
        assert status[2] is not None and "pending" in status[2]
        assert cols["created_by_token_id"][:2] == ("bigint", "YES")

    def test_status_started_index_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'snapshot_scans'"
            )
            names = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        assert "snapshot_scans_status_started" in names

    def test_fk_to_wms_tokens_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tc.constraint_type, ccu.table_name, ccu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'snapshot_scans'
                   AND tc.constraint_type = 'FOREIGN KEY'
                """
            )
            fks = cur.fetchall()
        finally:
            conn.close()
        parent_tables = {r[1] for r in fks}
        assert "wms_tokens" in parent_tables


class TestSnapshotScansNotifyTrigger:
    def test_pending_insert_fires_notify(self):
        """LISTEN on 'snapshot_scans_pending' and observe that an
        INSERT of a 'pending' row produces a NOTIFY carrying the
        scan_id as the payload. A commit is required for the notify
        to reach the listener."""
        listener = _make_conn()
        listener.autocommit = True  # LISTEN must be outside a transaction
        listener_cur = listener.cursor()
        listener_cur.execute("LISTEN snapshot_scans_pending")

        writer = _make_conn()
        try:
            scan_id = uuid.uuid4()
            wcur = writer.cursor()
            wcur.execute(
                "INSERT INTO snapshot_scans (scan_id, warehouse_id, status) "
                "VALUES (%s, 1, 'pending')",
                (str(scan_id),),
            )
            writer.commit()  # NOTIFY is delivered at COMMIT

            # Poll up to 2s for the notification to arrive.
            got = None
            for _ in range(20):
                if select.select([listener], [], [], 0.1) == ([], [], []):
                    continue
                listener.poll()
                if listener.notifies:
                    got = listener.notifies.pop(0)
                    break
            assert got is not None, (
                "expected a NOTIFY on snapshot_scans_pending after inserting a pending row"
            )
            assert got.channel == "snapshot_scans_pending"
            assert got.payload == str(scan_id)
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM snapshot_scans WHERE scan_id::text LIKE '%'"
            )
            cleanup.close()
            writer.close()
            listener_cur.close()
            listener.close()

    def test_non_pending_insert_does_not_fire_notify(self):
        """An INSERT with status='active' (or anything other than
        'pending') must not fire the NOTIFY. The keeper only cares
        about pending-row arrivals."""
        listener = _make_conn()
        listener.autocommit = True
        listener_cur = listener.cursor()
        listener_cur.execute("LISTEN snapshot_scans_pending")

        writer = _make_conn()
        try:
            scan_id = uuid.uuid4()
            wcur = writer.cursor()
            wcur.execute(
                "INSERT INTO snapshot_scans (scan_id, warehouse_id, status) "
                "VALUES (%s, 1, 'active')",
                (str(scan_id),),
            )
            writer.commit()

            # Allow time for a rogue NOTIFY to arrive; absence is the assertion.
            for _ in range(3):
                if select.select([listener], [], [], 0.1) != ([], [], []):
                    listener.poll()
            assert not listener.notifies, (
                "NOTIFY must not fire for non-pending status inserts"
            )
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM snapshot_scans WHERE scan_id = %s", (str(scan_id),)
            )
            cleanup.close()
            writer.close()
            listener_cur.close()
            listener.close()
