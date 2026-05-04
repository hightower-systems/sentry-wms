"""Schema-level tests for migration 038 (v1.7.0 cross-system mappings).

Locks the bidirectional-mapping shape into structural checks:
- table exists with the expected columns
- UNIQUE (source_system, source_type, source_id) enforced
- canonical-side index exists (covers (canonical_type, canonical_id))
- source_type / canonical_type CHECK rejects bogus values
- FK to inbound_source_systems_allowlist rejects orphan source_system
- DELETE / TRUNCATE on cross_system_mappings fire forensic audit rows

Same shape as test_wms_tokens_migration.py and
test_inbound_source_systems_allowlist_migration.py.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _make_allowlist_row(conn, source_system):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (source_system,),
    )
    cur.close()


def _drop_allowlist_row(conn, source_system):
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM inbound_source_systems_allowlist WHERE source_system = %s",
        (source_system,),
    )
    cur.close()


class TestCrossSystemMappingsShape:
    def test_table_exists_with_expected_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'cross_system_mappings'
                 ORDER BY column_name
                """
            )
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert rows["mapping_id"] == ("bigint", "NO")
        assert rows["source_system"] == ("character varying", "NO")
        assert rows["source_type"] == ("character varying", "NO")
        assert rows["source_id"] == ("character varying", "NO")
        assert rows["canonical_type"] == ("character varying", "NO")
        assert rows["canonical_id"] == ("uuid", "NO")
        assert rows["first_seen_at"][0].startswith("timestamp")
        assert rows["last_updated_at"][0].startswith("timestamp")

    def test_source_unique_index_present(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT indexname, indexdef
                  FROM pg_indexes
                 WHERE tablename = 'cross_system_mappings'
                """
            )
            indexes = {name: defn for name, defn in cur.fetchall()}
        finally:
            conn.close()
        assert "cross_system_mappings_source_unique" in indexes
        defn = indexes["cross_system_mappings_source_unique"]
        assert "UNIQUE" in defn.upper()
        for col in ("source_system", "source_type", "source_id"):
            assert col in defn
        assert "cross_system_mappings_canonical" in indexes
        canon_defn = indexes["cross_system_mappings_canonical"]
        assert "canonical_type" in canon_defn and "canonical_id" in canon_defn

    def test_source_unique_constraint_rejects_duplicate(self):
        ss = f"csm-unique-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _make_allowlist_row(conn, ss)
            cur = conn.cursor()
            canon = uuid.uuid4()
            cur.execute(
                "INSERT INTO cross_system_mappings "
                "(source_system, source_type, source_id, canonical_type, canonical_id) "
                "VALUES (%s, 'customer', 'C-1', 'customer', %s)",
                (ss, str(canon)),
            )
            try:
                cur.execute(
                    "INSERT INTO cross_system_mappings "
                    "(source_system, source_type, source_id, canonical_type, canonical_id) "
                    "VALUES (%s, 'customer', 'C-1', 'customer', %s)",
                    (ss, str(uuid.uuid4())),
                )
            except psycopg2.errors.UniqueViolation:
                cur.execute(
                    "DELETE FROM cross_system_mappings WHERE source_system = %s",
                    (ss,),
                )
                _drop_allowlist_row(conn, ss)
                return
            cur.execute("DELETE FROM cross_system_mappings WHERE source_system = %s", (ss,))
            _drop_allowlist_row(conn, ss)
            raise AssertionError("duplicate (source_system, source_type, source_id) should have raised")
        finally:
            conn.close()

    def test_source_type_check_rejects_bogus(self):
        ss = f"csm-check-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _make_allowlist_row(conn, ss)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO cross_system_mappings "
                    "(source_system, source_type, source_id, canonical_type, canonical_id) "
                    "VALUES (%s, 'order', 'O-1', 'sales_order', %s)",
                    (ss, str(uuid.uuid4())),
                )
            except psycopg2.errors.CheckViolation:
                _drop_allowlist_row(conn, ss)
                return
            cur.execute("DELETE FROM cross_system_mappings WHERE source_system = %s", (ss,))
            _drop_allowlist_row(conn, ss)
            raise AssertionError("bogus source_type should have raised CheckViolation")
        finally:
            conn.close()

    def test_canonical_type_check_rejects_bogus(self):
        ss = f"csm-canon-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _make_allowlist_row(conn, ss)
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO cross_system_mappings "
                    "(source_system, source_type, source_id, canonical_type, canonical_id) "
                    "VALUES (%s, 'sales_order', 'O-1', 'order', %s)",
                    (ss, str(uuid.uuid4())),
                )
            except psycopg2.errors.CheckViolation:
                _drop_allowlist_row(conn, ss)
                return
            cur.execute("DELETE FROM cross_system_mappings WHERE source_system = %s", (ss,))
            _drop_allowlist_row(conn, ss)
            raise AssertionError("bogus canonical_type should have raised CheckViolation")
        finally:
            conn.close()

    def test_fk_rejects_unallowlisted_source_system(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO cross_system_mappings "
                    "(source_system, source_type, source_id, canonical_type, canonical_id) "
                    "VALUES ('nope-not-allowlisted', 'customer', 'C-1', 'customer', %s)",
                    (str(uuid.uuid4()),),
                )
            except psycopg2.errors.ForeignKeyViolation:
                return
            raise AssertionError("FK violation expected for unallowlisted source_system")
        finally:
            conn.close()


class TestCrossSystemMappingsAuditTriggers:
    def _clean_audit(self, conn):
        cur = conn.cursor()
        cur.execute("DELETE FROM cross_system_mappings_audit")
        cur.close()

    def test_delete_fires_statement_level_audit_row(self):
        ss = f"csm-audit-{uuid.uuid4().hex[:8]}"
        conn = _make_conn()
        conn.autocommit = True
        try:
            _make_allowlist_row(conn, ss)
            self._clean_audit(conn)
            cur = conn.cursor()
            for i in range(3):
                cur.execute(
                    "INSERT INTO cross_system_mappings "
                    "(source_system, source_type, source_id, canonical_type, canonical_id) "
                    "VALUES (%s, 'customer', %s, 'customer', %s)",
                    (ss, f"C-audit-{i}", str(uuid.uuid4())),
                )
            cur.execute(
                "DELETE FROM cross_system_mappings WHERE source_system = %s",
                (ss,),
            )
            cur.execute(
                "SELECT event_type, rows_affected, sess_user, curr_user, backend_pid "
                "  FROM cross_system_mappings_audit "
                " ORDER BY audit_id DESC LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            self._clean_audit(conn)
            _drop_allowlist_row(conn, ss)
            conn.close()
        assert row is not None
        event_type, rows_affected, sess_user, curr_user, pid = row
        assert event_type == "DELETE"
        assert rows_affected == 3
        assert sess_user
        assert curr_user
        assert isinstance(pid, int) and pid > 0

    def test_truncate_trigger_is_registered(self):
        """Same defensible structural check used elsewhere: TRUNCATE on a
        mapping-truth table is risky to exercise live."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tgname, pg_get_triggerdef(oid) AS def
                  FROM pg_trigger
                 WHERE tgrelid = 'cross_system_mappings'::regclass
                   AND NOT tgisinternal
                """
            )
            triggers = {name: definition for name, definition in cur.fetchall()}
        finally:
            conn.close()
        assert "tr_cross_system_mappings_audit_truncate" in triggers
        assert "TRUNCATE" in triggers["tr_cross_system_mappings_audit_truncate"].upper()
