"""Schema-level tests for migration 054 (v1.9.0 dockd integration).

Coverage:
- five new item_fulfillments columns (pre_ship_status, voided_at,
  voided_by, void_reason, shipping_cost) exist with the right types
  and are nullable.
- dockd_idempotency table exists with the right column shape, the
  composite PK on (token_id, idempotency_key), the prune index on
  created_at, and the FK to wms_tokens with ON DELETE CASCADE.
- backfill runs to completion: existing SHIPPED rows carry
  pre_ship_status='PICKED' (idempotent because the migration's
  WHERE pre_ship_status IS NULL avoids double-writes).
- cleanup_dockd_idempotency deletes rows past the 72h retention
  window and leaves fresher rows alone.
- idempotent re-run via ADD COLUMN IF NOT EXISTS.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "054_dockd_integration.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _make_so(conn, status="PICKED"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, warehouse_id, external_id, status) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING so_id",
        (
            f"SO-T54-{uuid.uuid4().hex[:8]}",
            "t54",
            1,
            str(uuid.uuid4()),
            status,
        ),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _make_fulfillment(conn, so_id, status="SHIPPED"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO item_fulfillments "
        "(so_id, warehouse_id, tracking_number, carrier, ship_method, "
        " shipped_by, status, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING fulfillment_id",
        (so_id, 1, "T-54", "UPS", "GROUND", "t54", status, str(uuid.uuid4())),
    )
    fid = cur.fetchone()[0]
    cur.close()
    return fid


def _make_token(conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO wms_tokens (token_name, token_hash) "
        "VALUES (%s, %s) RETURNING token_id",
        (f"t54-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex + uuid.uuid4().hex),
    )
    token_id = cur.fetchone()[0]
    cur.close()
    return token_id


class TestItemFulfillmentsColumns:
    def test_five_columns_exist_with_expected_types(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, character_maximum_length,
                       numeric_precision, numeric_scale, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'item_fulfillments'
                   AND column_name IN ('pre_ship_status','voided_at',
                                       'voided_by','void_reason',
                                       'shipping_cost')
                 ORDER BY column_name
                """
            )
            rows = {r[0]: r for r in cur.fetchall()}
        finally:
            conn.close()
        assert set(rows) == {
            "pre_ship_status", "voided_at", "voided_by",
            "void_reason", "shipping_cost",
        }
        assert rows["pre_ship_status"][1] == "character varying"
        assert rows["pre_ship_status"][2] == 20
        assert rows["voided_at"][1] == "timestamp with time zone"
        assert rows["voided_by"][1] == "character varying"
        assert rows["voided_by"][2] == 100
        assert rows["void_reason"][1] == "character varying"
        assert rows["void_reason"][2] == 500
        assert rows["shipping_cost"][1] == "numeric"
        assert rows["shipping_cost"][3] == 12
        assert rows["shipping_cost"][4] == 2
        for name, row in rows.items():
            assert row[5] == "YES", f"{name} should be nullable"


class TestBackfill:
    def test_shipped_rows_carry_pre_ship_status_picked(self):
        conn = _make_conn()
        try:
            so_id = _make_so(conn)
            fid = _make_fulfillment(conn, so_id, status="SHIPPED")
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "UPDATE item_fulfillments SET pre_ship_status = NULL "
                "WHERE fulfillment_id = %s",
                (fid,),
            )
            conn.commit()
            with open(MIGRATION_PATH) as f:
                sql = f.read()
            cur.execute(sql)
            conn.commit()
            cur.execute(
                "SELECT pre_ship_status FROM item_fulfillments "
                "WHERE fulfillment_id = %s",
                (fid,),
            )
            assert cur.fetchone()[0] == "PICKED"
        finally:
            conn.close()


class TestDockdIdempotencyTable:
    def test_columns_and_pk(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, character_maximum_length,
                       is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'dockd_idempotency'
                 ORDER BY ordinal_position
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        names = [r[0] for r in rows]
        assert names == [
            "token_id", "idempotency_key", "endpoint", "so_number",
            "request_body_sha256", "response_body", "response_status",
            "created_at",
        ]
        cols = {r[0]: r for r in rows}
        assert cols["token_id"][1] == "bigint"
        assert cols["idempotency_key"][1] == "character varying"
        assert cols["idempotency_key"][2] == 64
        assert cols["endpoint"][2] == 50
        assert cols["so_number"][2] == 128
        assert cols["request_body_sha256"][1] == "character"
        assert cols["request_body_sha256"][2] == 64
        assert cols["response_body"][1] == "jsonb"
        assert cols["response_status"][1] == "smallint"
        assert cols["created_at"][1] == "timestamp with time zone"

    def test_pk_and_prune_index(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT indexname, indexdef
                  FROM pg_indexes
                 WHERE tablename = 'dockd_idempotency'
                 ORDER BY indexname
                """
            )
            idxs = {r[0]: r[1] for r in cur.fetchall()}
        finally:
            conn.close()
        assert "dockd_idempotency_pkey" in idxs
        assert "token_id, idempotency_key" in idxs["dockd_idempotency_pkey"]
        assert "dockd_idempotency_prune" in idxs
        assert "created_at" in idxs["dockd_idempotency_prune"]

    def test_fk_cascade_to_wms_tokens(self):
        conn = _make_conn()
        try:
            token_id = _make_token(conn)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO dockd_idempotency "
                "(token_id, idempotency_key, endpoint, so_number, "
                " request_body_sha256) "
                "VALUES (%s, %s, %s, %s, %s)",
                (token_id, "k1", "ship", "SO-X", "0" * 64),
            )
            conn.commit()
            cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
            conn.commit()
            cur.execute(
                "SELECT COUNT(*) FROM dockd_idempotency WHERE token_id = %s",
                (token_id,),
            )
            assert cur.fetchone()[0] == 0
        finally:
            conn.close()


class TestCleanupTask:
    def test_prunes_rows_older_than_72h(self, _db_transaction):
        # Run the cleanup impl through the per-test transactional
        # connection so inserts, DELETE, and re-query all see the
        # same in-flight state. Mirrors test_login_attempts_cleanup.
        from sqlalchemy import text as sa_text
        from jobs.cleanup_tasks import _cleanup_dockd_idempotency_impl
        db = _db_transaction
        db.execute(
            sa_text(
                "INSERT INTO wms_tokens (token_name, token_hash) "
                "VALUES (:n, :h) RETURNING token_id"
            ),
            {"n": "t54-cleanup", "h": "c" * 64},
        )
        token_id = db.execute(
            sa_text("SELECT token_id FROM wms_tokens WHERE token_name = :n"),
            {"n": "t54-cleanup"},
        ).fetchone()[0]
        stale = datetime.now(timezone.utc) - timedelta(hours=80)
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        db.execute(
            sa_text(
                "INSERT INTO dockd_idempotency "
                "(token_id, idempotency_key, endpoint, so_number, "
                " request_body_sha256, created_at) "
                "VALUES (:t,'stale','ship','SO-A',:h1,:s),"
                "       (:t,'fresh','ship','SO-B',:h2,:f)"
            ),
            {"t": token_id, "h1": "1" * 64, "h2": "2" * 64,
             "s": stale, "f": fresh},
        )

        deleted = _cleanup_dockd_idempotency_impl(db)
        assert deleted == 1

        keys = [
            r.idempotency_key for r in db.execute(
                sa_text(
                    "SELECT idempotency_key FROM dockd_idempotency "
                    "WHERE token_id = :t ORDER BY idempotency_key"
                ),
                {"t": token_id},
            ).fetchall()
        ]
        assert keys == ["fresh"]


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
