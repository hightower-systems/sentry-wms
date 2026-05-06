"""Schema-level tests for migration 037 (v1.7.0 Pipe B columns).

Locks the v1.7 inbound additions into structural checks:
- inbound_source_systems_allowlist exists with the right shape
- kind CHECK rejects bogus values
- wms_tokens.source_system FKs to the allowlist (nullable)
- wms_tokens.inbound_resources is TEXT[] default '{}'
- wms_tokens.mapping_override is BOOLEAN default FALSE
- DELETE / TRUNCATE on the allowlist fire forensic audit rows

Raw psycopg2 connection; same pattern as test_wms_tokens_migration.py.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid

import psycopg2


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


class TestInboundSourceSystemsAllowlistShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'inbound_source_systems_allowlist'
                 ORDER BY column_name
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert rows["source_system"] == ("character varying", "NO")
        assert rows["kind"] == ("character varying", "NO")
        assert rows["notes"][0] == "text"
        assert rows["created_at"][0].startswith("timestamp")

    def test_kind_check_rejects_bogus_value(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            ss = f"check-probe-{uuid.uuid4().hex[:8]}"
            try:
                cur.execute(
                    "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
                    "VALUES (%s, 'bogus')",
                    (ss,),
                )
            except psycopg2.errors.CheckViolation:
                return
            cur.execute(
                "DELETE FROM inbound_source_systems_allowlist WHERE source_system = %s",
                (ss,),
            )
            raise AssertionError("kind='bogus' should have raised CheckViolation")
        finally:
            conn.close()

    def test_kind_accepts_three_known_values(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            inserted = []
            for kind in ("connector", "internal_tool", "manual_import"):
                ss = f"kind-probe-{kind}-{uuid.uuid4().hex[:8]}"
                cur.execute(
                    "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
                    "VALUES (%s, %s)",
                    (ss, kind),
                )
                inserted.append(ss)
            for ss in inserted:
                cur.execute(
                    "DELETE FROM inbound_source_systems_allowlist WHERE source_system = %s",
                    (ss,),
                )
        finally:
            conn.close()


class TestWmsTokensInboundColumns:
    def test_source_system_is_nullable_fk(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable FROM information_schema.columns
                 WHERE table_name = 'wms_tokens' AND column_name = 'source_system'
                """
            )
            data_type, nullable = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'wms_tokens'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'inbound_source_systems_allowlist'
                """
            )
            fk_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert data_type == "character varying"
        assert nullable == "YES", "source_system must be nullable so outbound-only tokens stay valid"
        assert fk_count == 1, "source_system must FK to inbound_source_systems_allowlist"

    def test_inbound_resources_is_text_array_default_empty(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'wms_tokens' AND column_name = 'inbound_resources'
                """
            )
            data_type, nullable, default = cur.fetchone()
        finally:
            conn.close()
        assert data_type == "ARRAY"
        assert nullable == "NO"
        assert default is not None and "{}" in default

    def test_mapping_override_is_boolean_default_false(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'wms_tokens' AND column_name = 'mapping_override'
                """
            )
            data_type, nullable, default = cur.fetchone()
        finally:
            conn.close()
        assert data_type == "boolean"
        assert nullable == "NO"
        assert default is not None and "false" in default.lower()

    def test_fk_rejects_unallowlisted_source_system(self):
        """Insert a wms_tokens row with source_system='nope-not-allowlisted'.
        Should fail with ForeignKeyViolation. Verifies the FK is the correct
        shape (PostgreSQL forbids subqueries in CHECK constraints, hence FK)."""
        import hashlib
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            unique_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            try:
                cur.execute(
                    "INSERT INTO wms_tokens (token_name, token_hash, source_system) "
                    "VALUES (%s, %s, %s)",
                    (
                        f"fk-probe-{uuid.uuid4().hex[:8]}",
                        unique_hash,
                        "nope-not-allowlisted",
                    ),
                )
            except psycopg2.errors.ForeignKeyViolation:
                return
            raise AssertionError("FK violation expected for unallowlisted source_system")
        finally:
            conn.close()

    def test_outbound_only_token_inserts_with_null_source_system(self):
        """Existing outbound-only tokens must keep working: NULL source_system
        is valid; the FK is exempt by the nullable shape."""
        import hashlib
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            unique_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            cur.execute(
                "INSERT INTO wms_tokens (token_name, token_hash) "
                "VALUES (%s, %s) RETURNING token_id, source_system, "
                "       inbound_resources, mapping_override",
                (f"outbound-only-{uuid.uuid4().hex[:8]}", unique_hash),
            )
            token_id, source_system, inbound_resources, mapping_override = cur.fetchone()
            cur.execute("DELETE FROM wms_tokens WHERE token_id = %s", (token_id,))
        finally:
            conn.close()
        assert source_system is None
        assert list(inbound_resources) == []
        assert mapping_override is False


class TestAllowlistAuditTriggers:
    def _clean_audit(self, conn):
        cur = conn.cursor()
        cur.execute("DELETE FROM inbound_source_systems_allowlist_audit")
        cur.close()

    def test_delete_fires_statement_level_audit_row(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            self._clean_audit(conn)
            cur = conn.cursor()
            inserted = []
            for i in range(3):
                ss = f"audit-delete-{uuid.uuid4().hex[:8]}-{i}"
                cur.execute(
                    "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
                    "VALUES (%s, 'internal_tool')",
                    (ss,),
                )
                inserted.append(ss)
            cur.execute(
                "DELETE FROM inbound_source_systems_allowlist "
                " WHERE source_system = ANY(%s)",
                (inserted,),
            )
            cur.execute(
                "SELECT event_type, rows_affected, sess_user, curr_user, backend_pid "
                "  FROM inbound_source_systems_allowlist_audit "
                " ORDER BY audit_id DESC LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            self._clean_audit(conn)
            conn.close()
        assert row is not None
        event_type, rows_affected, sess_user, curr_user, pid = row
        assert event_type == "DELETE"
        assert rows_affected == 3
        assert sess_user
        assert curr_user
        assert isinstance(pid, int) and pid > 0

    def test_truncate_trigger_is_registered(self):
        """Same defensible structural check used in test_wms_tokens_migration.py:
        TRUNCATE on a privilege table is risky to exercise live."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tgname, pg_get_triggerdef(oid) AS def
                  FROM pg_trigger
                 WHERE tgrelid = 'inbound_source_systems_allowlist'::regclass
                   AND NOT tgisinternal
                """
            )
            triggers = {name: definition for name, definition in cur.fetchall()}
        finally:
            conn.close()
        assert "tr_inbound_source_systems_allowlist_audit_truncate" in triggers
        assert "TRUNCATE" in triggers["tr_inbound_source_systems_allowlist_audit_truncate"].upper()
