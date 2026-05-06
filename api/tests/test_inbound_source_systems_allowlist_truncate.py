"""v1.7.0 #275: inbound_source_systems_allowlist TRUNCATE forensic
trigger reachability.

The AFTER TRUNCATE trigger writes one forensic row to
inbound_source_systems_allowlist_audit, but it only fires on
TRUNCATE ... CASCADE. A plain TRUNCATE on the allowlist raises
ForeignKeyViolation before the trigger fires because the v1.7
inbound tables and cross_system_mappings declare FKs into
source_system. These tests pin both paths so a future schema change
that adds or removes an FK referencer doesn't silently flip the
forensic shape.

Tests use direct psycopg2 connections so the FK error and trigger
both observe a real transaction. Each test cleans up the synthetic
row it inserts so other tests' allowlist baseline is preserved.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.errors
import pytest


DATABASE_URL = os.environ["TEST_DATABASE_URL"]


def _baseline_audit_count(cur) -> int:
    cur.execute(
        "SELECT COUNT(*) FROM inbound_source_systems_allowlist_audit "
        " WHERE event_type = 'TRUNCATE'"
    )
    return cur.fetchone()[0]


class TestAllowlistTruncateReachability:
    def test_plain_truncate_raises_fk_violation(self):
        """Plain TRUNCATE on the allowlist raises ForeignKeyViolation
        because the v1.7 inbound tables and cross_system_mappings
        carry NOT NULL FKs into source_system. The trigger never fires;
        no forensic row is written. The error itself is the only
        signal."""
        conn = psycopg2.connect(DATABASE_URL)
        try:
            conn.autocommit = False
            cur = conn.cursor()
            baseline = _baseline_audit_count(cur)
            with pytest.raises(psycopg2.errors.FeatureNotSupported) as excinfo:
                cur.execute("TRUNCATE inbound_source_systems_allowlist")
            # Postgres raises with class 0A "feature not supported" and
            # message "cannot truncate a table referenced in a foreign
            # key constraint".
            assert "foreign key constraint" in str(excinfo.value)
            conn.rollback()
            # Audit table count is unchanged.
            assert _baseline_audit_count(cur) == baseline
        finally:
            conn.close()

    def test_truncate_cascade_writes_forensic_audit_row(self):
        """TRUNCATE ... CASCADE wipes the allowlist plus every NOT NULL
        referencing table; the AFTER TRUNCATE trigger fires and writes
        one forensic row carrying SESSION_USER, CURRENT_USER,
        backend_pid, application_name. The trigger writes
        rows_affected = NULL because TRUNCATE doesn't expose the row
        count to the trigger surface."""
        # Use a savepoint so the CASCADE wipe doesn't actually destroy
        # operator state. The conftest's session TRUNCATE wipes these
        # tables anyway; here we wrap in a transaction we ROLLBACK.
        conn = psycopg2.connect(DATABASE_URL)
        try:
            conn.autocommit = False
            cur = conn.cursor()
            baseline = _baseline_audit_count(cur)
            # Insert a synthetic row so the table is non-empty for the
            # CASCADE path.
            label = f"truncate-test-{uuid.uuid4().hex[:8]}"
            cur.execute(
                "INSERT INTO inbound_source_systems_allowlist "
                " (source_system, kind) VALUES (%s, 'internal_tool')",
                (label,),
            )
            cur.execute(
                "TRUNCATE inbound_source_systems_allowlist CASCADE"
            )
            # Trigger fires synchronously inside the same transaction;
            # the audit row is visible before COMMIT.
            cur.execute(
                "SELECT event_type, rows_affected, sess_user, "
                "       backend_pid, application_name "
                "  FROM inbound_source_systems_allowlist_audit "
                " ORDER BY audit_id DESC LIMIT 1"
            )
            row = cur.fetchone()
            assert row is not None
            event_type, rows_affected, sess_user, backend_pid, app_name = row
            assert event_type == "TRUNCATE"
            # rows_affected is NULL on TRUNCATE (no OLD TABLE binding).
            assert rows_affected is None
            assert sess_user is not None
            assert backend_pid is not None
            # The audit count went up by exactly one.
            assert _baseline_audit_count(cur) == baseline + 1
            # Roll back the CASCADE so the test doesn't destroy
            # operator state. This also rolls back the audit INSERT,
            # which is the correct behavior for a test session: the
            # audit row only persists for committed CASCADE writes.
            conn.rollback()
            # After rollback, audit count returns to baseline.
            assert _baseline_audit_count(cur) == baseline
        finally:
            conn.close()

    def test_delete_path_independently_writes_audit_row(self):
        """The DELETE forensic path is unconditional. Pin it alongside
        the TRUNCATE path so a future schema change that removes the
        DELETE trigger doesn't silently degrade forensic coverage."""
        conn = psycopg2.connect(DATABASE_URL)
        try:
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM inbound_source_systems_allowlist_audit "
                " WHERE event_type = 'DELETE'"
            )
            baseline_delete = cur.fetchone()[0]
            label = f"delete-test-{uuid.uuid4().hex[:8]}"
            cur.execute(
                "INSERT INTO inbound_source_systems_allowlist "
                " (source_system, kind) VALUES (%s, 'internal_tool')",
                (label,),
            )
            cur.execute(
                "DELETE FROM inbound_source_systems_allowlist "
                " WHERE source_system = %s",
                (label,),
            )
            cur.execute(
                "SELECT event_type, rows_affected "
                "  FROM inbound_source_systems_allowlist_audit "
                " ORDER BY audit_id DESC LIMIT 1"
            )
            event_type, rows_affected = cur.fetchone()
            assert event_type == "DELETE"
            assert rows_affected == 1
            cur.execute(
                "SELECT COUNT(*) FROM inbound_source_systems_allowlist_audit "
                " WHERE event_type = 'DELETE'"
            )
            assert cur.fetchone()[0] == baseline_delete + 1
            conn.rollback()
        finally:
            conn.close()
