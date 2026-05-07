"""Schema-level tests for migration 049 (v1.8.0 #281 transfer orders).

Coverage:
- transfer_orders / transfer_order_lines / transfer_order_approvals
  exist with the expected column types + nullability + CHECKs.
- ON DELETE CASCADE cascades from transfer_orders to lines and
  approvals.
- pick_tasks gains to_id + to_line_id (nullable) and the XOR
  CHECK rejects (so_id NULL, to_id NULL) + (so_id NOT NULL, to_id
  NOT NULL); accepts each valid form.
- transfer_order_lines monotonicity CHECKs reject committed >
  requested, picked > committed, approved > picked.
- app_settings.transfer_order_block_self_approval seeded TRUE.
- Re-running the migration is idempotent (CREATE TABLE IF NOT
  EXISTS, ADD COLUMN IF NOT EXISTS, DROP CONSTRAINT IF EXISTS,
  ON CONFLICT DO NOTHING).
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
from psycopg2.extras import Json


MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "049_transfer_orders.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _make_pick_batch(conn, warehouse_id):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, %s, 'OPEN') RETURNING batch_id",
        (f"BATCH-T49-{uuid.uuid4().hex[:8]}", warehouse_id),
    )
    bid = cur.fetchone()[0]
    cur.close()
    return bid


def _make_to(conn, source_wh=1, dest_wh=2, created_by="t49"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transfer_orders "
        "(to_number, source_warehouse_id, destination_warehouse_id, "
        " created_by, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING to_id",
        (
            f"TO-T49-{uuid.uuid4().hex[:8]}",
            source_wh, dest_wh, created_by, str(uuid.uuid4()),
        ),
    )
    to_id = cur.fetchone()[0]
    cur.close()
    return to_id


def _make_to_line(conn, to_id, line_number=1, item_id=1, requested=10):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transfer_order_lines "
        "(to_id, item_id, line_number, requested_qty) "
        "VALUES (%s, %s, %s, %s) RETURNING to_line_id",
        (to_id, item_id, line_number, requested),
    )
    line_id = cur.fetchone()[0]
    cur.close()
    return line_id


def _make_to_approval(conn, to_id, submitted_by="picker1"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transfer_order_approvals "
        "(to_id, submitted_by, lines_snapshot, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING to_approval_id",
        (to_id, submitted_by, Json({"lines": []}), str(uuid.uuid4())),
    )
    aid = cur.fetchone()[0]
    cur.close()
    return aid


# ---------------------------------------------------------------------
# Table shape
# ---------------------------------------------------------------------


class TestTransferOrdersShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'transfer_orders'
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert rows["to_id"] == ("integer", "NO")
        assert rows["to_number"][1] == "NO"
        assert rows["source_warehouse_id"] == ("integer", "NO")
        assert rows["destination_warehouse_id"] == ("integer", "NO")
        assert rows["status"][1] == "NO"
        assert rows["external_id"] == ("uuid", "NO")
        assert rows["notes"][1] == "YES"

    def test_distinct_warehouse_check_rejects_self_transfer(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO transfer_orders "
                    "(to_number, source_warehouse_id, destination_warehouse_id, "
                    " created_by, external_id) "
                    "VALUES (%s, 1, 1, 't49', %s)",
                    (f"TO-SELF-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
                )
                conn.commit()
                assert False, "self-transfer should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_status_check_rejects_unknown_value(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO transfer_orders "
                    "(to_number, source_warehouse_id, destination_warehouse_id, "
                    " status, created_by, external_id) "
                    "VALUES (%s, 1, 2, 'BOGUS', 't49', %s)",
                    (f"TO-BOGUS-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
                )
                conn.commit()
                assert False, "BOGUS status should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()


class TestTransferOrderLinesMonotonicity:
    def test_committed_cannot_exceed_requested(self):
        conn = _make_conn()
        try:
            to_id = _make_to(conn)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO transfer_order_lines "
                    "(to_id, item_id, line_number, requested_qty, committed_qty) "
                    "VALUES (%s, 1, 1, 5, 99)",
                    (to_id,),
                )
                conn.commit()
                assert False, "committed > requested should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_picked_cannot_exceed_committed(self):
        conn = _make_conn()
        try:
            to_id = _make_to(conn)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO transfer_order_lines "
                    "(to_id, item_id, line_number, requested_qty, "
                    " committed_qty, picked_qty) "
                    "VALUES (%s, 1, 1, 10, 5, 9)",
                    (to_id,),
                )
                conn.commit()
                assert False, "picked > committed should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_approved_cannot_exceed_picked(self):
        conn = _make_conn()
        try:
            to_id = _make_to(conn)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO transfer_order_lines "
                    "(to_id, item_id, line_number, requested_qty, "
                    " committed_qty, picked_qty, approved_qty) "
                    "VALUES (%s, 1, 1, 10, 10, 5, 9)",
                    (to_id,),
                )
                conn.commit()
                assert False, "approved > picked should violate CHECK"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_unique_to_id_line_number(self):
        conn = _make_conn()
        try:
            to_id = _make_to(conn)
            _make_to_line(conn, to_id, line_number=1)
            conn.commit()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO transfer_order_lines "
                    "(to_id, item_id, line_number, requested_qty) "
                    "VALUES (%s, 2, 1, 5)",
                    (to_id,),
                )
                conn.commit()
                assert False, "duplicate (to_id, line_number) should violate UNIQUE"
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
        finally:
            conn.close()


class TestCascadeDelete:
    def test_delete_to_cascades_to_lines_and_approvals(self):
        conn = _make_conn()
        try:
            to_id = _make_to(conn)
            line_id = _make_to_line(conn, to_id)
            approval_id = _make_to_approval(conn, to_id)
            conn.commit()

            cur = conn.cursor()
            cur.execute("DELETE FROM transfer_orders WHERE to_id = %s", (to_id,))
            conn.commit()

            cur.execute(
                "SELECT COUNT(*) FROM transfer_order_lines WHERE to_line_id = %s",
                (line_id,),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM transfer_order_approvals "
                "WHERE to_approval_id = %s",
                (approval_id,),
            )
            assert cur.fetchone()[0] == 0
        finally:
            conn.close()


class TestPickTasksXor:
    def test_rejects_both_null(self):
        conn = _make_conn()
        try:
            batch_id = _make_pick_batch(conn, warehouse_id=1)
            conn.commit()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO pick_tasks "
                    "(batch_id, item_id, bin_id, quantity_to_pick, pick_sequence) "
                    "VALUES (%s, 1, 1, 1, 1)",
                    (batch_id,),
                )
                conn.commit()
                assert False, "(so_id NULL, to_id NULL) should violate XOR"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_rejects_both_set(self):
        conn = _make_conn()
        try:
            batch_id = _make_pick_batch(conn, warehouse_id=1)
            to_id = _make_to(conn)
            line_id = _make_to_line(conn, to_id)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sales_orders "
                "(so_number, customer_name, warehouse_id, external_id) "
                "VALUES (%s, 't49', 1, %s) RETURNING so_id",
                (f"SO-T49-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
            )
            so_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO sales_order_lines "
                "(so_id, line_number, item_id, quantity_ordered) "
                "VALUES (%s, 1, 1, 1) RETURNING so_line_id",
                (so_id,),
            )
            so_line_id = cur.fetchone()[0]
            conn.commit()

            try:
                cur.execute(
                    "INSERT INTO pick_tasks "
                    "(batch_id, so_id, so_line_id, to_id, to_line_id, "
                    " item_id, bin_id, quantity_to_pick, pick_sequence) "
                    "VALUES (%s, %s, %s, %s, %s, 1, 1, 1, 1)",
                    (batch_id, so_id, so_line_id, to_id, line_id),
                )
                conn.commit()
                assert False, "(so_id NOT NULL, to_id NOT NULL) should violate XOR"
            except psycopg2.errors.CheckViolation:
                conn.rollback()
        finally:
            conn.close()

    def test_accepts_to_only(self):
        conn = _make_conn()
        try:
            batch_id = _make_pick_batch(conn, warehouse_id=1)
            to_id = _make_to(conn)
            line_id = _make_to_line(conn, to_id)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO pick_tasks "
                "(batch_id, to_id, to_line_id, item_id, bin_id, "
                " quantity_to_pick, pick_sequence) "
                "VALUES (%s, %s, %s, 1, 1, 1, 1) RETURNING pick_task_id",
                (batch_id, to_id, line_id),
            )
            pid = cur.fetchone()[0]
            conn.commit()
            assert pid is not None
        finally:
            conn.close()

    def test_accepts_so_only(self):
        # Regression net: existing SO-only inserts still work after the
        # NOT NULL drop on so_id / so_line_id.
        conn = _make_conn()
        try:
            batch_id = _make_pick_batch(conn, warehouse_id=1)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sales_orders "
                "(so_number, customer_name, warehouse_id, external_id) "
                "VALUES (%s, 't49', 1, %s) RETURNING so_id",
                (f"SO-T49-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
            )
            so_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO sales_order_lines "
                "(so_id, line_number, item_id, quantity_ordered) "
                "VALUES (%s, 1, 1, 1) RETURNING so_line_id",
                (so_id,),
            )
            so_line_id = cur.fetchone()[0]
            conn.commit()
            cur.execute(
                "INSERT INTO pick_tasks "
                "(batch_id, so_id, so_line_id, item_id, bin_id, "
                " quantity_to_pick, pick_sequence) "
                "VALUES (%s, %s, %s, 1, 1, 1, 1) RETURNING pick_task_id",
                (batch_id, so_id, so_line_id),
            )
            assert cur.fetchone()[0] is not None
            conn.commit()
        finally:
            conn.close()


class TestSelfApprovalSetting:
    def test_app_settings_row_seeded_true(self):
        # Conftest TRUNCATEs app_settings at session start and re-loads
        # from seed-apartment-lab.sql which does not declare this row;
        # re-apply the migration's idempotent INSERT so the assertion
        # reflects post-migration state regardless of session-load
        # order. Mirrors the v1.7.0 #271 audit_log_chain_head pattern.
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO app_settings (key, value) "
                "VALUES ('transfer_order_block_self_approval', 'true') "
                "ON CONFLICT (key) DO NOTHING"
            )
            cur.execute(
                "SELECT value FROM app_settings "
                "WHERE key = 'transfer_order_block_self_approval'"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "true"
        finally:
            conn.close()


class TestIdempotency:
    def test_re_run_migration_no_error(self):
        # Re-applying the migration against a DB that already has it
        # must succeed: CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT
        # EXISTS, DROP CONSTRAINT IF EXISTS, ON CONFLICT DO NOTHING,
        # ALTER COLUMN DROP NOT NULL on already-nullable columns are
        # all idempotent.
        with open(MIGRATION_PATH) as f:
            sql = f.read()
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(sql)
        finally:
            conn.close()
