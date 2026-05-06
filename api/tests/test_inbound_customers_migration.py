"""Schema-level tests for migration 041 (v1.7.0 inbound_customers + new canonical customers).

Locks the new-canonical-table shape into structural checks:
- customers exists with canonical_id UUID PK + V-216 external_id UUID UNIQUE
- conservative NOT NULL posture: only canonical_id, external_id,
  created_at, updated_at, latest_inbound_id NOT NULL
- latest_inbound_id is BIGINT, no FK
- inbound_customers shape mirrors mig 039 / 040
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
            f"inbound-customers-test-{uuid.uuid4().hex[:8]}",
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


class TestCanonicalCustomersShape:
    def test_table_exists_with_conservative_not_null_posture(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'customers'
                 ORDER BY column_name
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        # NOT NULL set per plan §1.4.
        assert rows["canonical_id"] == ("uuid", "NO")
        assert rows["external_id"] == ("uuid", "NO")
        assert rows["created_at"][1] == "NO"
        assert rows["updated_at"][1] == "NO"
        assert rows["latest_inbound_id"] == ("bigint", "NO")
        # Everything else nullable until v2.0.
        for col in ("customer_name", "email", "phone", "billing_address",
                    "shipping_address", "tax_id", "is_active"):
            assert rows[col][1] == "YES", f"{col} should be nullable until v2.0"

    def test_canonical_id_is_primary_key_with_uuid_default(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_attribute a
                    ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                 WHERE i.indrelid = 'customers'::regclass
                   AND i.indisprimary
                """
            )
            pk_cols = [r[0] for r in cur.fetchall()]
            cur.execute(
                """
                SELECT column_default FROM information_schema.columns
                 WHERE table_name = 'customers' AND column_name = 'canonical_id'
                """
            )
            default = cur.fetchone()[0]
        finally:
            conn.close()
        assert pk_cols == ["canonical_id"]
        assert "gen_random_uuid" in (default or "")

    def test_external_id_is_unique_with_uuid_default(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_default FROM information_schema.columns
                 WHERE table_name = 'customers' AND column_name = 'external_id'
                """
            )
            default = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'customers'
                   AND tc.constraint_type = 'UNIQUE'
                   AND ccu.column_name = 'external_id'
                """
            )
            uniq_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert "gen_random_uuid" in (default or "")
        assert uniq_count == 1

    def test_latest_inbound_id_has_no_fk(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.constraint_column_usage ccu
                  JOIN information_schema.referential_constraints rc
                    ON ccu.constraint_name = rc.constraint_name
                 WHERE ccu.table_name = 'customers'
                   AND ccu.column_name = 'latest_inbound_id'
                """
            )
            fk_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert fk_count == 0

    def test_minimal_insert_satisfies_conservative_not_null_set(self):
        """A row with only the defaulted columns + NOT NULL set must insert
        cleanly. All optional fields stay NULL."""
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO customers DEFAULT VALUES RETURNING canonical_id, external_id, "
                "       customer_name, email, phone, latest_inbound_id"
            )
            row = cur.fetchone()
            cid = row[0]
            cur.execute("DELETE FROM customers WHERE canonical_id = %s", (cid,))
        finally:
            conn.close()
        canonical_id, external_id, name, email, phone, latest_inbound_id = row
        assert canonical_id is not None
        assert external_id is not None
        assert name is None and email is None and phone is None
        assert latest_inbound_id == 0


class TestInboundCustomersShape:
    def test_table_and_indexes_present(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'inbound_customers'
                """
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'inbound_customers'"
            )
            indexes = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        assert cols["inbound_id"] == ("bigint", "NO")
        assert cols["canonical_id"] == ("uuid", "NO")
        assert cols["ingested_via_token_id"] == ("bigint", "NO")
        assert "inbound_customers_idempotency" in indexes
        assert "inbound_customers_current" in indexes
        assert "inbound_customers_canonical" in indexes

    def test_idempotency_unique_rejects_duplicate(self):
        ss = f"customers-uniq-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _ensure_allowlist(conn, ss)
            token_id = _make_token(conn)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO inbound_customers "
                "(source_system, external_id, external_version, canonical_id, "
                " canonical_payload, source_payload, ingested_via_token_id) "
                "VALUES (%s, 'C-1', 'v1', %s, %s, %s, %s)",
                (ss, str(uuid.uuid4()), Json({"name": "x"}), Json({"name": "x"}), token_id),
            )
            try:
                cur.execute(
                    "INSERT INTO inbound_customers "
                    "(source_system, external_id, external_version, canonical_id, "
                    " canonical_payload, source_payload, ingested_via_token_id) "
                    "VALUES (%s, 'C-1', 'v1', %s, %s, %s, %s)",
                    (ss, str(uuid.uuid4()), Json({"name": "x"}), Json({"name": "x"}), token_id),
                )
            except psycopg2.errors.UniqueViolation:
                cur.execute("DELETE FROM inbound_customers WHERE source_system = %s", (ss,))
                _drop_token(conn, token_id)
                _drop_allowlist(conn, ss)
                return
            raise AssertionError("duplicate idempotency triple should have raised")
        finally:
            conn.close()

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
                 WHERE tc.table_name = 'inbound_customers'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'wms_tokens'
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0] == ("RESTRICT", "wms_tokens")
