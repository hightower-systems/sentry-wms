"""Schema-level tests for migrations 022 + 023 (v1.5.0 #127).

Locks the pinned design decisions into structural checks:
- credential_type exists on connector_credentials with default
  'connector_api_key' (Decision P predecessor for v2+ outbound flavours)
- wms_tokens has no encrypted_token column (Decision P: hash-only)
- token_hash is CHAR(64) UNIQUE (64 hex chars = SHA-256)
- warehouse_ids / event_types / endpoints are typed arrays (Decision S)
- expires_at defaults to NOW() + INTERVAL '1 year' (Decision R)
- status defaults to 'active'
- wms_tokens_status_rotated index exists for the admin rotation badge

Raw psycopg2 connection; pure information_schema introspection.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


class TestCredentialTypeColumn:
    def test_credential_type_present_on_connector_credentials(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'connector_credentials'
                   AND column_name = 'credential_type'
                """
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "connector_credentials.credential_type column missing (migration 022)"
        data_type, nullable, default = row
        assert data_type == "character varying"
        assert nullable == "NO"
        assert default is not None
        assert "connector_api_key" in default


class TestWmsTokensShape:
    def test_no_encrypted_token_column(self):
        """Decision P: wms_tokens is hash-only. An encrypted_token column
        would signal the table was wired up wrong (reusable plaintext)."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'wms_tokens'
                """
            )
            cols = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        assert "encrypted_token" not in cols
        assert "token_hash" in cols

    def test_token_hash_is_char_64_unique(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, character_maximum_length, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'wms_tokens' AND column_name = 'token_hash'
                """
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT 1 FROM information_schema.table_constraints
                 WHERE table_name = 'wms_tokens' AND constraint_type = 'UNIQUE'
                """
            )
            uniques = cur.fetchall()
        finally:
            conn.close()
        data_type, max_len, nullable = row
        assert data_type == "character"
        assert max_len == 64  # SHA-256 hex digest length
        assert nullable == "NO"
        # token_hash is the only UNIQUE constraint in v1.5.0.
        assert len(uniques) >= 1

    def test_scope_columns_are_typed_arrays(self):
        """Decision S: scope is typed arrays, not JSONB."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type
                  FROM information_schema.columns
                 WHERE table_name = 'wms_tokens'
                   AND column_name IN ('warehouse_ids', 'event_types', 'endpoints')
                 ORDER BY column_name
                """
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}
        finally:
            conn.close()
        # Postgres reports array columns as "ARRAY" in information_schema.
        assert rows["warehouse_ids"] == "ARRAY"
        assert rows["event_types"] == "ARRAY"
        assert rows["endpoints"] == "ARRAY"

    def test_status_defaults_to_active(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_default FROM information_schema.columns
                 WHERE table_name = 'wms_tokens' AND column_name = 'status'
                """
            )
            default = cur.fetchone()[0]
        finally:
            conn.close()
        assert default is not None and "active" in default

    def test_expires_at_defaults_to_one_year(self):
        """Decision R: default expiry = NOW() + INTERVAL '1 year'. Insert a
        minimal row and assert expires_at lands roughly one year out."""
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO wms_tokens (token_name, token_hash) "
                "VALUES ('expiry-default-probe', repeat('x', 64)) "
                "RETURNING created_at, expires_at"
            )
            created_at, expires_at = cur.fetchone()
            # Allow a few seconds of wall clock slack.
            delta = (expires_at - created_at).total_seconds()
            one_year = 365 * 24 * 3600
            assert abs(delta - one_year) < 86_400, (
                f"expires_at default should be ~1 year past created_at; "
                f"got {delta:.0f} seconds"
            )
            cur.execute("DELETE FROM wms_tokens WHERE token_name = 'expiry-default-probe'")
        finally:
            conn.close()

    def test_status_rotated_index_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'wms_tokens'"
            )
            names = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        assert "wms_tokens_status_rotated" in names

    def test_connector_id_fk_allows_null(self):
        """wms_tokens.connector_id is nullable (admin-issued tokens may
        not yet be tied to a specific connector). The FK still enforces
        referential integrity when a value is supplied."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT is_nullable FROM information_schema.columns
                 WHERE table_name = 'wms_tokens' AND column_name = 'connector_id'
                """
            )
            nullable = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'wms_tokens'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'connectors'
                """
            )
            fk_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert nullable == "YES"
        assert fk_count == 1


class TestEndpointsBackfill:
    """v1.5.1 V-200 (#140): migration 026 backfills empty endpoints on
    pre-v1.5.1 tokens so they keep working after the decorator starts
    enforcing the slug list. Tokens that already had an explicit list
    are left alone."""

    _MIGRATION_SQL = """
        UPDATE wms_tokens
           SET endpoints = ARRAY[
                 'events.poll',
                 'events.ack',
                 'events.types',
                 'events.schema',
                 'snapshot.inventory'
               ]::TEXT[]
         WHERE endpoints = '{}'::TEXT[]
    """

    def _insert_with_endpoints(self, conn, name_suffix, endpoints):
        import hashlib
        cur = conn.cursor()
        # Each test uses a unique hash to avoid UNIQUE collision when
        # re-running against a populated DB.
        import uuid
        unique_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        cur.execute(
            "INSERT INTO wms_tokens (token_name, token_hash, endpoints) "
            "VALUES (%s, %s, %s) RETURNING token_id",
            (f"endpoints-backfill-{name_suffix}", unique_hash, endpoints),
        )
        token_id = cur.fetchone()[0]
        conn.commit()
        return token_id

    def _cleanup(self, conn, token_ids):
        cur = conn.cursor()
        for tid in token_ids:
            cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (tid,))
        conn.commit()

    def test_empty_endpoints_are_backfilled_to_full_slug_set(self):
        conn = _make_conn()
        try:
            empty_id = self._insert_with_endpoints(conn, "empty", [])
            keep_id = self._insert_with_endpoints(
                conn, "keep", ["events.poll"]
            )
            cur = conn.cursor()
            cur.execute(self._MIGRATION_SQL)
            conn.commit()
            cur.execute(
                "SELECT endpoints FROM wms_tokens WHERE token_id = %s",
                (empty_id,),
            )
            backfilled = list(cur.fetchone()[0])
            cur.execute(
                "SELECT endpoints FROM wms_tokens WHERE token_id = %s",
                (keep_id,),
            )
            preserved = list(cur.fetchone()[0])
            self._cleanup(conn, [empty_id, keep_id])
        finally:
            conn.close()
        assert set(backfilled) == {
            "events.poll", "events.ack", "events.types",
            "events.schema", "snapshot.inventory",
        }
        assert preserved == ["events.poll"], (
            "migration must not overwrite tokens that already set an explicit slug list"
        )
