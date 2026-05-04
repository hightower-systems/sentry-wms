"""Schema-level tests for migration 044 (v1.7.0 inbound_cleanup_runs).

Locks the retention-beat log shape into structural checks:
- table exists with the expected columns + defaults
- resource CHECK rejects bogus values
- status CHECK rejects bogus values
- (resource, started_at DESC) index exists
- partial (status, started_at DESC) WHERE status='failed' index exists
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


class TestInboundCleanupRunsShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'inbound_cleanup_runs'
                 ORDER BY column_name
                """
            )
            rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert rows["run_id"][0] == "bigint"
        assert rows["resource"] == ("character varying", "NO", None)
        assert rows["started_at"][1] == "NO"
        assert rows["finished_at"][1] == "YES"
        assert rows["rows_nullified"][0] == "integer"
        assert rows["rows_nullified"][1] == "NO"
        assert rows["retention_days"][1] == "NO"
        assert rows["status"][1] == "NO"
        assert "running" in (rows["status"][2] or "")

    def test_indexes_present(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                " WHERE tablename = 'inbound_cleanup_runs'"
            )
            indexes = {name: defn for name, defn in cur.fetchall()}
        finally:
            conn.close()
        assert "inbound_cleanup_runs_resource_started" in indexes
        rs = indexes["inbound_cleanup_runs_resource_started"]
        assert "resource" in rs and "started_at" in rs
        assert "inbound_cleanup_runs_status_started" in indexes
        ss = indexes["inbound_cleanup_runs_status_started"]
        assert "WHERE" in ss.upper() and "failed" in ss


class TestInboundCleanupRunsChecks:
    def test_resource_check_rejects_bogus(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO inbound_cleanup_runs (resource, retention_days) "
                    "VALUES ('orders', 30)"
                )
            except psycopg2.errors.CheckViolation:
                return
            raise AssertionError("resource='orders' should have raised CheckViolation")
        finally:
            conn.close()

    def test_status_check_rejects_bogus(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO inbound_cleanup_runs "
                    "(resource, retention_days, status) "
                    "VALUES ('items', 30, 'pending')"
                )
            except psycopg2.errors.CheckViolation:
                return
            raise AssertionError("status='pending' should have raised CheckViolation")
        finally:
            conn.close()

    def test_minimal_insert_path_uses_defaults(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO inbound_cleanup_runs (resource, retention_days) "
                "VALUES ('items', 30) RETURNING run_id, status, rows_nullified, "
                "       finished_at, error_message"
            )
            run_id, status, rows_nullified, finished_at, error_message = cur.fetchone()
            cur.execute("DELETE FROM inbound_cleanup_runs WHERE run_id = %s", (run_id,))
        finally:
            conn.close()
        assert status == "running"
        assert rows_nullified == 0
        assert finished_at is None
        assert error_message is None

    def test_full_lifecycle_insert_then_succeed(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO inbound_cleanup_runs (resource, retention_days) "
                "VALUES ('sales_orders', 90) RETURNING run_id"
            )
            run_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE inbound_cleanup_runs SET status='succeeded', finished_at=NOW(), "
                "       rows_nullified=42 WHERE run_id=%s",
                (run_id,),
            )
            cur.execute(
                "SELECT status, rows_nullified, finished_at IS NOT NULL "
                "  FROM inbound_cleanup_runs WHERE run_id=%s",
                (run_id,),
            )
            status, rows_nullified, has_finished = cur.fetchone()
            cur.execute("DELETE FROM inbound_cleanup_runs WHERE run_id=%s", (run_id,))
        finally:
            conn.close()
        assert status == "succeeded"
        assert rows_nullified == 42
        assert has_finished is True
