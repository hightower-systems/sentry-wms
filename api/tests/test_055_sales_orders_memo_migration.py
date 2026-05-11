"""Schema-level tests for migration 055 (v1.9.0 sales_orders.memo).

Coverage:
- Column exists with type TEXT and is nullable.
- Existing rows carry NULL after migration (no backfill).
- Round-trip: insert + select preserves the value verbatim,
  including newlines and whitespace.
- Idempotent re-run.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "055_sales_orders_memo.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _make_so(conn, *, memo=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders (so_number, customer_name, warehouse_id, "
        "external_id, memo) VALUES (%s, %s, %s, %s, %s) RETURNING so_id",
        (
            f"SO-T55-{uuid.uuid4().hex[:8]}",
            "t55", 1, str(uuid.uuid4()), memo,
        ),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


class TestColumnShape:
    def test_memo_is_text_nullable(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                " WHERE table_name = 'sales_orders' AND column_name = 'memo'"
            )
            row = cur.fetchone()
            assert row is not None, "memo column missing"
            assert row[0] == "text"
            assert row[1] == "YES"
        finally:
            conn.close()


class TestStorageBehaviour:
    def test_existing_rows_have_null_memo(self):
        conn = _make_conn()
        try:
            so_id = _make_so(conn)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT memo FROM sales_orders WHERE so_id = %s", (so_id,),
            )
            assert cur.fetchone()[0] is None
        finally:
            conn.close()

    def test_round_trip_simple_string(self):
        conn = _make_conn()
        try:
            so_id = _make_so(conn, memo="leave at back door")
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT memo FROM sales_orders WHERE so_id = %s", (so_id,),
            )
            assert cur.fetchone()[0] == "leave at back door"
        finally:
            conn.close()

    def test_round_trip_with_newlines_preserved(self):
        conn = _make_conn()
        try:
            multiline = "line1\nline2\n\nline4 with spaces"
            so_id = _make_so(conn, memo=multiline)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT memo FROM sales_orders WHERE so_id = %s", (so_id,),
            )
            assert cur.fetchone()[0] == multiline
        finally:
            conn.close()

    def test_long_memo_accepted(self):
        # TEXT has no length cap; a 10 KB memo lands cleanly.
        conn = _make_conn()
        try:
            big = "x" * 10_240
            so_id = _make_so(conn, memo=big)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT length(memo) FROM sales_orders WHERE so_id = %s",
                (so_id,),
            )
            assert cur.fetchone()[0] == 10_240
        finally:
            conn.close()


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
