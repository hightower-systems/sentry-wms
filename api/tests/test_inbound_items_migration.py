"""Schema-level tests for migration 040 (v1.7.0 inbound_items).

Same coverage shape as test_inbound_sales_orders_migration.py:
- table exists with the expected columns
- idempotency UNIQUE on (source_system, external_id, external_version)
- partial 'applied' index, canonical_id index
- ingested_via_token_id is BIGINT FK ON DELETE RESTRICT to wms_tokens
- status CHECK enforces applied / superseded
- items.latest_inbound_id added (no FK, no index)
"""

import os
import sys
import uuid
import hashlib

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
            f"inbound-items-test-{uuid.uuid4().hex[:8]}",
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


class TestInboundItemsShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'inbound_items'
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
        # mig 045 dropped NOT NULL on source_payload (R6 retention task).
        assert rows["source_payload"] == ("jsonb", "YES")
        assert rows["status"] == ("character varying", "NO")
        assert rows["ingested_via_token_id"] == ("bigint", "NO")

    def test_indexes_present(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                " WHERE tablename = 'inbound_items'"
            )
            indexes = {name: defn for name, defn in cur.fetchall()}
        finally:
            conn.close()
        assert "inbound_items_idempotency" in indexes
        idem = indexes["inbound_items_idempotency"]
        assert "UNIQUE" in idem.upper()
        for col in ("source_system", "external_id", "external_version"):
            assert col in idem
        assert "inbound_items_current" in indexes
        cur_def = indexes["inbound_items_current"]
        assert "received_at" in cur_def
        assert "WHERE" in cur_def.upper() and "applied" in cur_def
        assert "inbound_items_canonical" in indexes

    def test_token_fk_is_restrict(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT rc.delete_rule, ccu.table_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.referential_constraints rc
                    ON tc.constraint_name = rc.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'inbound_items'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'wms_tokens'
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        delete_rule, target = rows[0]
        assert delete_rule == "RESTRICT"
        assert target == "wms_tokens"


class TestInboundItemsBehavior:
    def _insert_row(self, conn, ss, token_id, ext_id, ext_ver):
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO inbound_items "
            "(source_system, external_id, external_version, canonical_id, "
            " canonical_payload, source_payload, ingested_via_token_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING inbound_id",
            (
                ss,
                ext_id,
                ext_ver,
                str(uuid.uuid4()),
                Json({"sku": ext_id}),
                Json({"sku": ext_id}),
                token_id,
            ),
        )
        inbound_id = cur.fetchone()[0]
        cur.close()
        return inbound_id

    def test_idempotency_unique_rejects_duplicate(self):
        ss = f"items-uniq-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            self._insert_row(conn, ss, token_id, "SKU-1", "v1")
            try:
                self._insert_row(conn, ss, token_id, "SKU-1", "v1")
            except psycopg2.errors.UniqueViolation:
                cur = conn.cursor()
                cur.execute("DELETE FROM inbound_items WHERE source_system = %s", (ss,))
                cur.close()
                _drop_token(conn, token_id)
                _drop_allowlist(conn, ss)
                return
            raise AssertionError("duplicate idempotency triple should have raised")
        finally:
            conn.close()

    def test_status_check_rejects_bogus(self):
        ss = f"items-status-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO inbound_items "
                    "(source_system, external_id, external_version, canonical_id, "
                    " canonical_payload, source_payload, ingested_via_token_id, status) "
                    "VALUES (%s, 'SKU-x', 'v1', %s, '{}'::jsonb, '{}'::jsonb, %s, 'pending')",
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
        ss = f"items-tokfk-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            inbound_id = self._insert_row(conn, ss, token_id, "SKU-tokfk", "v1")
            cur = conn.cursor()
            try:
                cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
            except psycopg2.errors.ForeignKeyViolation:
                cur.execute("DELETE FROM inbound_items WHERE inbound_id = %s", (inbound_id,))
                _drop_token(conn, token_id)
                _drop_allowlist(conn, ss)
                return
            raise AssertionError("DELETE on referenced wms_tokens row should have raised FK violation")
        finally:
            conn.close()


class TestItemsLatestInboundId:
    def test_column_added_unindexed_no_fk(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable FROM information_schema.columns
                 WHERE table_name = 'items'
                   AND column_name = 'latest_inbound_id'
                """
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.constraint_column_usage ccu
                  JOIN information_schema.referential_constraints rc
                    ON ccu.constraint_name = rc.constraint_name
                 WHERE ccu.table_name = 'items'
                   AND ccu.column_name = 'latest_inbound_id'
                """
            )
            fk_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert row is not None, "items.latest_inbound_id missing"
        assert row == ("bigint", "YES")
        assert fk_count == 0
