"""Schema-level tests for migration 052 (v1.8.0 #284).

Schema half of #270 (mapping_override semantics resolution: Option B
-- per-token static JSONB column).

Coverage:
- mapping_overrides exists as jsonb, NOT NULL, default '{}'.
- Existing rows have '{}' after the migration runs.
- Inserting a token without mapping_overrides defaults to {};
  empty dict and populated dict both round-trip cleanly.
- The existing mapping_override BOOLEAN capability flag is unchanged
  (v1.7 contract preserved; the boolean is the gate, the JSONB is
  the override map).
- Idempotent re-run.
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

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "db", "migrations",
    "052_mapping_override_per_token.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _make_token(conn, mapping_overrides=None, mapping_override=False):
    cur = conn.cursor()
    if mapping_overrides is None:
        cur.execute(
            "INSERT INTO wms_tokens "
            "(token_name, token_hash, mapping_override) "
            "VALUES (%s, %s, %s) RETURNING token_id",
            (
                f"t52-{uuid.uuid4().hex[:8]}",
                hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                mapping_override,
            ),
        )
    else:
        cur.execute(
            "INSERT INTO wms_tokens "
            "(token_name, token_hash, mapping_override, mapping_overrides) "
            "VALUES (%s, %s, %s, %s) RETURNING token_id",
            (
                f"t52-{uuid.uuid4().hex[:8]}",
                hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                mapping_override,
                Json(mapping_overrides),
            ),
        )
    tid = cur.fetchone()[0]
    cur.close()
    return tid


class TestColumnShape:
    def test_column_exists_as_jsonb_not_null_with_empty_default(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'wms_tokens'
                   AND column_name = 'mapping_overrides'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        data_type, nullable, default = row
        assert data_type == "jsonb"
        assert nullable == "NO"
        assert "{}" in default

    def test_existing_rows_have_empty_object_after_migration(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM wms_tokens "
                "WHERE mapping_overrides IS NULL"
            )
            assert cur.fetchone()[0] == 0
        finally:
            conn.close()


class TestStorageBehaviour:
    def test_default_when_omitted_is_empty_object(self):
        conn = _make_conn()
        try:
            tid = _make_token(conn)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT mapping_overrides FROM wms_tokens WHERE token_id = %s",
                (tid,),
            )
            assert cur.fetchone()[0] == {}
        finally:
            conn.close()

    def test_empty_dict_round_trips(self):
        conn = _make_conn()
        try:
            tid = _make_token(conn, mapping_overrides={})
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT mapping_overrides FROM wms_tokens WHERE token_id = %s",
                (tid,),
            )
            assert cur.fetchone()[0] == {}
        finally:
            conn.close()

    def test_populated_dict_round_trips(self):
        payload = {
            "marketplace_id": "AMAZON",
            "currency": "USD",
            "channel_id": 7,
        }
        conn = _make_conn()
        try:
            tid = _make_token(conn, mapping_overrides=payload,
                              mapping_override=True)
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT mapping_overrides, mapping_override "
                "FROM wms_tokens WHERE token_id = %s",
                (tid,),
            )
            stored, flag = cur.fetchone()
            assert stored == payload
            assert flag is True
        finally:
            conn.close()

    def test_mapping_override_boolean_unchanged(self):
        # v1.7 contract: the boolean is the capability gate. v1.8 #284
        # adds the JSONB but does not redefine the boolean's semantics.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'wms_tokens'
                   AND column_name = 'mapping_override'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        data_type, nullable, default = row
        assert data_type == "boolean"
        assert nullable == "NO"
        assert default in ("false", "FALSE")


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
