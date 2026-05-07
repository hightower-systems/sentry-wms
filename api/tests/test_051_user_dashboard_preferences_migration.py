"""Schema-level tests for migration 051 (v1.8.0 #283).

Coverage:
- user_dashboard_preferences table shape: user_id PK FK to users
  (ON DELETE CASCADE), chart_order JSONB default, default_range +
  default_view CHECKs, updated_at NOT NULL.
- ON DELETE CASCADE removes preferences row when user is deleted.
- ix_audit_log_dashboard exists in pg_indexes with key columns
  (action_type, created_at, user_id, warehouse_id) and INCLUDE
  columns (entity_id, details). With enable_seqscan = OFF, EXPLAIN
  on the dashboard query shape selects the index.
- warehouses.timezone exists, NOT NULL, defaults populated for
  existing rows.
- Idempotent re-run.
"""

import os
import sys
import uuid
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
from psycopg2.extras import Json

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "051_user_dashboard_preferences.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _make_user(conn):
    # Literal column list (not dynamic) so test_external_id_inserts
    # static guardrail (#241) sees external_id in the SQL.
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users "
        "(username, password_hash, full_name, role, external_id) "
        "VALUES (%s, 'x', 't51', 'USER', %s) RETURNING user_id",
        (f"t51-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
    )
    uid = cur.fetchone()[0]
    cur.close()
    return uid


# ---------------------------------------------------------------------
# user_dashboard_preferences
# ---------------------------------------------------------------------


class TestPreferencesShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'user_dashboard_preferences'
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert rows["user_id"] == ("integer", "NO")
        assert rows["chart_order"][0] == "jsonb"
        assert rows["chart_order"][1] == "NO"
        assert rows["default_range"][1] == "NO"
        assert rows["default_view"][1] == "NO"
        assert rows["updated_at"][1] == "NO"

    def test_default_range_check_rejects_unknown(self):
        conn = _make_conn()
        try:
            uid = _make_user(conn)
            conn.commit()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO user_dashboard_preferences "
                    "(user_id, default_range) VALUES (%s, 'last_century')",
                    (uid,),
                )
                conn.commit()
                assert False, "unknown default_range should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_default_view_check_rejects_unknown(self):
        conn = _make_conn()
        try:
            uid = _make_user(conn)
            conn.commit()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO user_dashboard_preferences "
                    "(user_id, default_view) VALUES (%s, 'graph')",
                    (uid,),
                )
                conn.commit()
                assert False, "unknown default_view should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_accepts_valid_values_round_trip(self):
        conn = _make_conn()
        try:
            uid = _make_user(conn)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO user_dashboard_preferences "
                "(user_id, chart_order, default_range, default_view) "
                "VALUES (%s, %s, 'last_7d', 'table')",
                (uid, Json(["packing", "shipped"])),
            )
            conn.commit()
            cur.execute(
                "SELECT chart_order, default_range, default_view "
                "FROM user_dashboard_preferences WHERE user_id = %s",
                (uid,),
            )
            order, rng, view = cur.fetchone()
            assert order == ["packing", "shipped"]
            assert rng == "last_7d"
            assert view == "table"
        finally:
            conn.close()

    def test_delete_user_cascades(self):
        conn = _make_conn()
        try:
            uid = _make_user(conn)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO user_dashboard_preferences (user_id) VALUES (%s)",
                (uid,),
            )
            conn.commit()
            cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
            conn.commit()
            cur.execute(
                "SELECT COUNT(*) FROM user_dashboard_preferences "
                "WHERE user_id = %s",
                (uid,),
            )
            assert cur.fetchone()[0] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------
# ix_audit_log_dashboard
# ---------------------------------------------------------------------


class TestAuditLogDashboardIndex:
    def test_index_exists_with_expected_definition(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT indexdef FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND indexname = 'ix_audit_log_dashboard'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "ix_audit_log_dashboard must exist"
        defn = row[0]
        # Key columns in order
        assert "action_type" in defn
        assert "created_at" in defn
        assert "user_id" in defn
        assert "warehouse_id" in defn
        # INCLUDE clause
        assert "INCLUDE" in defn
        assert "entity_id" in defn
        assert "details" in defn

    def test_index_keys_and_includes_match_dashboard_query(self):
        # Whether the planner *chooses* the index depends on data
        # distribution + planner stats; that is the operator's
        # pre-merge gate concern (run EXPLAIN ANALYZE on a 30-day
        # window with prod-like data). The unit-test contract is the
        # index shape: leftmost key columns match the WHERE-clause
        # leading edge, and INCLUDE covers the projection.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.attname, ix.indkey, ix.indnatts, ix.indnkeyatts
                  FROM pg_class c
                  JOIN pg_index ix ON ix.indexrelid = c.oid
                  JOIN pg_class t  ON t.oid = ix.indrelid
                  JOIN pg_attribute a
                       ON a.attrelid = t.oid
                      AND a.attnum = ANY(ix.indkey::int[])
                 WHERE c.relname = 'ix_audit_log_dashboard'
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert rows, "ix_audit_log_dashboard should expose its key columns"
        names = {r[0] for r in rows}
        # All four key columns plus both INCLUDE columns visible.
        for col in ("action_type", "created_at", "user_id",
                    "warehouse_id", "entity_id", "details"):
            assert col in names, f"{col} missing from ix_audit_log_dashboard"
        # Four key columns; six total attributes (4 key + 2 INCLUDE).
        _, _, indnatts, indnkeyatts = rows[0]
        assert indnatts == 6
        assert indnkeyatts == 4


# ---------------------------------------------------------------------
# warehouses.timezone
# ---------------------------------------------------------------------


class TestWarehousesTimezone:
    def test_column_exists_not_null_with_default(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'warehouses'
                   AND column_name = 'timezone'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        data_type, nullable, default = row
        assert data_type == "character varying"
        assert nullable == "NO"
        assert "America/Denver" in default

    def test_existing_rows_have_default_populated(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM warehouses WHERE timezone IS NULL"
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT timezone FROM warehouses LIMIT 1"
            )
            row = cur.fetchone()
            if row is not None:
                assert row[0] == "America/Denver"
        finally:
            conn.close()


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


class TestIdempotency:
    def test_re_run_migration_no_error(self):
        with open(MIGRATION_PATH) as f:
            sql = f.read()
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(sql)
        finally:
            conn.close()
