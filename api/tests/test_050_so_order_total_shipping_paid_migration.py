"""Schema-level tests for migration 050 (v1.8.0 #282 sales_orders
order_total + customer_shipping_paid).

Coverage:
- both columns exist as NUMERIC(12,2) and nullable.
- existing rows have NULL after the migration runs (the migration is
  pure ALTER TABLE ADD COLUMN; nothing backfills).
- precision/scale behaviour: Postgres rounds excess scale silently
  (12.345 -> 12.35), and rejects excess precision with
  numeric_value_out_of_range. Wire-level decimal_places enforcement
  is a Pydantic concern that lands in Pass 3.
- zero is storable and distinguishable from NULL (0.00 vs NULL).
- idempotent re-run via ADD COLUMN IF NOT EXISTS.
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

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "050_so_order_total_shipping_paid.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _make_so(conn, order_total=None, customer_shipping_paid=None):
    # Explicit column list (not dynamic) so the test_external_id_inserts
    # static guardrail (#241) can see external_id in the literal SQL.
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, warehouse_id, external_id, "
        " order_total, customer_shipping_paid) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING so_id",
        (
            f"SO-T50-{uuid.uuid4().hex[:8]}",
            "t50",
            1,
            str(uuid.uuid4()),
            order_total,
            customer_shipping_paid,
        ),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


class TestColumnShape:
    def test_both_columns_exist_with_numeric_12_2(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, numeric_precision,
                       numeric_scale, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'sales_orders'
                   AND column_name IN ('order_total', 'customer_shipping_paid')
                 ORDER BY column_name
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert len(rows) == 2
        for row in rows:
            name, data_type, precision, scale, nullable = row
            assert data_type == "numeric", f"{name} should be numeric"
            assert precision == 12, f"{name} should have precision 12"
            assert scale == 2, f"{name} should have scale 2"
            assert nullable == "YES", f"{name} should be nullable"


class TestStorageBehaviour:
    def test_existing_rows_have_null_for_both(self):
        conn = _make_conn()
        try:
            so_id = _make_so(conn)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT order_total, customer_shipping_paid "
                "FROM sales_orders WHERE so_id = %s",
                (so_id,),
            )
            ot, csp = cur.fetchone()
            assert ot is None
            assert csp is None
        finally:
            conn.close()

    def test_zero_distinguishable_from_null(self):
        conn = _make_conn()
        try:
            so_id = _make_so(conn, order_total=Decimal("0.00"))
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT order_total, customer_shipping_paid "
                "FROM sales_orders WHERE so_id = %s",
                (so_id,),
            )
            ot, csp = cur.fetchone()
            assert ot == Decimal("0.00")
            assert csp is None
        finally:
            conn.close()

    def test_round_trip_typical_value(self):
        conn = _make_conn()
        try:
            so_id = _make_so(
                conn,
                order_total=Decimal("123.45"),
                customer_shipping_paid=Decimal("9.99"),
            )
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT order_total, customer_shipping_paid "
                "FROM sales_orders WHERE so_id = %s",
                (so_id,),
            )
            ot, csp = cur.fetchone()
            assert ot == Decimal("123.45")
            assert csp == Decimal("9.99")
        finally:
            conn.close()

    def test_excess_scale_is_rounded_silently(self):
        # Postgres NUMERIC(p,s) rounds to s decimals. Wire-level
        # decimal_places=2 rejection is the Pydantic layer's job;
        # the column itself is permissive.
        conn = _make_conn()
        try:
            so_id = _make_so(conn, order_total=Decimal("12.345"))
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT order_total FROM sales_orders WHERE so_id = %s",
                (so_id,),
            )
            stored = cur.fetchone()[0]
            assert stored in (Decimal("12.35"), Decimal("12.34")), (
                f"expected banker-rounded value, got {stored}"
            )
        finally:
            conn.close()

    def test_excess_precision_raises(self):
        # NUMERIC(12,2) caps the integer part at 10 digits
        # (9,999,999,999.99). 11 digits to the left should raise
        # numeric_value_out_of_range.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            try:
                _make_so(conn, order_total=Decimal("99999999999.99"))
                conn.commit()
                assert False, "excess precision should raise"
            except psycopg2.errors.NumericValueOutOfRange:
                conn.rollback()
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
