"""Schema-level tests for migration 039 (v1.7.0 inbound_sales_orders).

Locks the per-resource staging shape into structural checks:
- table exists with the expected columns
- idempotency UNIQUE on (source_system, external_id, external_version)
- partial index on (..., received_at DESC) WHERE status='applied'
- index on canonical_id
- ingested_via_token_id is BIGINT FK ON DELETE RESTRICT to wms_tokens
- status CHECK enforces applied / superseded
- sales_orders.latest_inbound_id added (no FK, no index)
- DELETE on a wms_tokens row referenced by inbound_sales_orders raises FK
"""

import os
import sys
import uuid
import json

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hashlib
import psycopg2
from psycopg2.extras import Json


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_allowlist(conn, source_system):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (source_system,),
    )
    cur.close()


def _drop_allowlist(conn, source_system):
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM inbound_source_systems_allowlist WHERE source_system = %s",
        (source_system,),
    )
    cur.close()


def _make_token(conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO wms_tokens (token_name, token_hash) VALUES (%s, %s) "
        "RETURNING token_id",
        (
            f"inbound-so-test-{uuid.uuid4().hex[:8]}",
            hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        ),
    )
    token_id = cur.fetchone()[0]
    cur.close()
    return token_id


def _drop_token(conn, token_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
    cur.close()


class TestInboundSalesOrdersShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'inbound_sales_orders'
                 ORDER BY column_name
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert rows["inbound_id"] == ("bigint", "NO")
        assert rows["source_system"] == ("character varying", "NO")
        assert rows["external_id"] == ("character varying", "NO")
        assert rows["external_version"] == ("character varying", "NO")
        assert rows["canonical_id"] == ("uuid", "NO")
        assert rows["canonical_payload"] == ("jsonb", "NO")
        # mig 045 dropped NOT NULL on source_payload so the v1.7.0 R6
        # retention task can null the column past the configured window.
        assert rows["source_payload"] == ("jsonb", "YES")
        assert rows["received_at"][0].startswith("timestamp")
        assert rows["status"] == ("character varying", "NO")
        assert rows["superseded_at"][0].startswith("timestamp")
        assert rows["superseded_at"][1] == "YES"
        assert rows["ingested_via_token_id"] == ("bigint", "NO")

    def test_idempotency_unique_index_and_partial_current_index(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                " WHERE tablename = 'inbound_sales_orders'"
            )
            indexes = {name: defn for name, defn in cur.fetchall()}
        finally:
            conn.close()
        assert "inbound_sales_orders_idempotency" in indexes
        idem = indexes["inbound_sales_orders_idempotency"]
        assert "UNIQUE" in idem.upper()
        for col in ("source_system", "external_id", "external_version"):
            assert col in idem

        assert "inbound_sales_orders_current" in indexes
        current = indexes["inbound_sales_orders_current"]
        assert "received_at" in current
        assert "WHERE" in current.upper()
        assert "applied" in current

        assert "inbound_sales_orders_canonical" in indexes

    def test_ingested_via_token_fk_to_wms_tokens(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tc.constraint_name, rc.delete_rule, ccu.table_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.referential_constraints rc
                    ON tc.constraint_name = rc.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'inbound_sales_orders'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'wms_tokens'
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, "expected one FK to wms_tokens"
        _, delete_rule, target = rows[0]
        assert delete_rule == "RESTRICT"
        assert target == "wms_tokens"


class TestInboundSalesOrdersBehavior:
    def _insert_row(self, conn, source_system, token_id, ext_id, ext_ver):
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO inbound_sales_orders "
            "(source_system, external_id, external_version, canonical_id, "
            " canonical_payload, source_payload, ingested_via_token_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING inbound_id",
            (
                source_system,
                ext_id,
                ext_ver,
                str(uuid.uuid4()),
                Json({"so_number": ext_id}),
                Json({"orderNumber": ext_id}),
                token_id,
            ),
        )
        inbound_id = cur.fetchone()[0]
        cur.close()
        return inbound_id

    def test_idempotency_unique_rejects_duplicate(self):
        ss = f"so-uniq-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            self._insert_row(conn, ss, token_id, "SO-1", "v1")
            try:
                self._insert_row(conn, ss, token_id, "SO-1", "v1")
            except psycopg2.errors.UniqueViolation:
                cur = conn.cursor()
                cur.execute("DELETE FROM inbound_sales_orders WHERE source_system = %s", (ss,))
                cur.close()
                _drop_token(conn, token_id)
                _drop_allowlist(conn, ss)
                return
            raise AssertionError("duplicate (source_system, external_id, external_version) should have raised")
        finally:
            conn.close()

    def test_status_check_rejects_bogus(self):
        ss = f"so-status-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO inbound_sales_orders "
                    "(source_system, external_id, external_version, canonical_id, "
                    " canonical_payload, source_payload, ingested_via_token_id, status) "
                    "VALUES (%s, 'SO-x', 'v1', %s, '{}'::jsonb, '{}'::jsonb, %s, 'pending')",
                    (ss, str(uuid.uuid4()), token_id),
                )
            except psycopg2.errors.CheckViolation:
                _drop_token(conn, token_id)
                _drop_allowlist(conn, ss)
                return
            raise AssertionError("status='pending' should have raised CheckViolation")
        finally:
            conn.close()

    def test_token_delete_with_inbound_row_raises_restrict(self):
        ss = f"so-tokfk-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            inbound_id = self._insert_row(conn, ss, token_id, "SO-tokfk", "v1")
            cur = conn.cursor()
            try:
                cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
            except psycopg2.errors.ForeignKeyViolation:
                cur.execute(
                    "DELETE FROM inbound_sales_orders WHERE inbound_id = %s",
                    (inbound_id,),
                )
                _drop_token(conn, token_id)
                _drop_allowlist(conn, ss)
                return
            raise AssertionError("DELETE on referenced wms_tokens row should have raised FK violation")
        finally:
            conn.close()

    def test_token_revoked_at_update_does_not_disturb_inbound(self):
        ss = f"so-rev-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            inbound_id = self._insert_row(conn, ss, token_id, "SO-rev", "v1")
            cur = conn.cursor()
            cur.execute(
                "UPDATE wms_tokens SET revoked_at = NOW() WHERE token_id = %s",
                (token_id,),
            )
            cur.execute(
                "SELECT inbound_id FROM inbound_sales_orders WHERE inbound_id = %s",
                (inbound_id,),
            )
            still_there = cur.fetchone()
            cur.execute("DELETE FROM inbound_sales_orders WHERE inbound_id = %s", (inbound_id,))
            _drop_token(conn, token_id)
            _drop_allowlist(conn, ss)
        finally:
            conn.close()
        assert still_there is not None and still_there[0] == inbound_id


class TestSalesOrdersLatestInboundId:
    def test_column_added_unindexed_no_fk(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable FROM information_schema.columns
                 WHERE table_name = 'sales_orders'
                   AND column_name = 'latest_inbound_id'
                """
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.constraint_column_usage ccu
                  JOIN information_schema.referential_constraints rc
                    ON ccu.constraint_name = rc.constraint_name
                 WHERE ccu.table_name = 'sales_orders'
                   AND ccu.column_name = 'latest_inbound_id'
                """
            )
            fk_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert row is not None, "sales_orders.latest_inbound_id missing"
        data_type, nullable = row
        assert data_type == "bigint"
        assert nullable == "YES"
        assert fk_count == 0, "latest_inbound_id must NOT have an FK (chicken-and-egg on first-time-receipt)"
