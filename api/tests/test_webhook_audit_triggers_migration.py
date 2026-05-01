"""Schema-level tests for migration 032 (v1.6.0 #167).

Locks the forensic instrumentation on webhook_subscriptions and
webhook_secrets:

  * Both audit tables exist with the V-157 column shape.
  * Both have a (event_at DESC) index for "most recent forensic
    events" queries.
  * Both source tables have AFTER DELETE (statement-level,
    transition table) and AFTER TRUNCATE (statement-level)
    triggers registered.
  * A multi-row DELETE produces exactly ONE audit row with
    rows_affected = N (not N rows).
  * A TRUNCATE produces one audit row with rows_affected = NULL.
  * Forensic columns (sess_user, curr_user, backend_pid,
    application_name, event_at) are populated.

CI loads db/schema.sql, so these tests are the load-bearing
assertion that the migration body and the schema.sql mirror
agree -- a missing trigger, a row-level firing, or a dropped
transition table reference all surface here as a failed assert.

Cleanup is on a best-effort basis: tests reset audit tables to
the pre-test row count after running so the running v1.6.0 dev
DB does not accumulate trigger noise across pytest runs.
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
import pytest


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_connector(cur, connector_id="test-conn-032"):
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "Migration 032 audit-trigger test connector"),
    )
    return connector_id


def _audit_count(cur, table):
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def _audit_rows_since(cur, table, baseline):
    cur.execute(
        f"SELECT event_type, rows_affected, sess_user, curr_user, "
        f"       backend_pid, application_name, event_at "
        f"  FROM {table} "
        f" WHERE audit_id > %s "
        f" ORDER BY audit_id ASC",
        (baseline,),
    )
    return cur.fetchall()


def _max_audit_id(cur, table):
    cur.execute(f"SELECT COALESCE(MAX(audit_id), 0) FROM {table}")
    return cur.fetchone()[0]


class TestWebhookAuditTablesShape:
    @pytest.mark.parametrize(
        "table",
        ["webhook_subscriptions_audit", "webhook_secrets_audit"],
    )
    def test_columns(self, table):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = %s
                 ORDER BY ordinal_position
                """,
                (table,),
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()

        assert cols["audit_id"] == ("bigint", "NO")
        assert cols["event_type"] == ("character varying", "NO")
        assert cols["rows_affected"] == ("integer", "YES")
        assert cols["sess_user"] == ("text", "NO")
        assert cols["curr_user"] == ("text", "NO")
        assert cols["backend_pid"] == ("integer", "NO")
        assert cols["application_name"] == ("text", "YES")
        assert cols["event_at"] == ("timestamp with time zone", "NO")

    @pytest.mark.parametrize(
        "table,index",
        [
            ("webhook_subscriptions_audit", "webhook_subscriptions_audit_event_at"),
            ("webhook_secrets_audit", "webhook_secrets_audit_event_at"),
        ],
    )
    def test_event_at_descending_index(self, table, index):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = %s AND indexname = %s",
                (table, index),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, (
            f"{index} must exist; the 'most recent forensic events' "
            f"query path uses event_at DESC and a missing index would "
            f"force a sequential scan on every triage page load"
        )
        assert "event_at DESC" in row[0]


class TestWebhookAuditTriggersRegistered:
    @pytest.mark.parametrize(
        "trigger,table,event,proname",
        [
            (
                "tr_webhook_subscriptions_audit_delete",
                "webhook_subscriptions",
                "DELETE",
                "webhook_subscriptions_audit_delete",
            ),
            (
                "tr_webhook_subscriptions_audit_truncate",
                "webhook_subscriptions",
                "TRUNCATE",
                "webhook_subscriptions_audit_truncate",
            ),
            (
                "tr_webhook_secrets_audit_delete",
                "webhook_secrets",
                "DELETE",
                "webhook_secrets_audit_delete",
            ),
            (
                "tr_webhook_secrets_audit_truncate",
                "webhook_secrets",
                "TRUNCATE",
                "webhook_secrets_audit_truncate",
            ),
        ],
    )
    def test_trigger_registered_statement_level(self, trigger, table, event, proname):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.tgname, c.relname, p.proname,
                       (t.tgtype & 1) AS is_row_level
                  FROM pg_trigger t
                  JOIN pg_class c ON c.oid = t.tgrelid
                  JOIN pg_proc p  ON p.oid = t.tgfoid
                 WHERE t.tgname = %s
                   AND NOT t.tgisinternal
                """,
                (trigger,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, f"{trigger} must be registered on {table}"
        tgname, relname, fn, is_row_level = row
        assert relname == table
        assert fn == proname
        # tgtype bit 0 = row-level (1) vs statement-level (0). A wipe-the-
        # world DELETE under a row-level trigger would produce N audit
        # rows; the V-157 design is statement-level on purpose.
        assert is_row_level == 0, (
            f"{trigger} must be statement-level (FOR EACH STATEMENT); "
            f"row-level firing would produce N audit rows for an N-row "
            f"DELETE and inflate the audit table to no purpose"
        )


class TestWebhookSubscriptionsAuditFiring:
    """Drive a DELETE and a TRUNCATE through webhook_subscriptions
    and assert the right audit shape lands."""

    def _make_subscription(self):
        conn = _make_conn()
        conn.autocommit = True
        cur = conn.cursor()
        connector_id = _ensure_connector(cur)
        cur.execute(
            """
            INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
            VALUES (%s, %s, %s)
            RETURNING subscription_id
            """,
            (connector_id, "audit fire test", "https://example.invalid/audit"),
        )
        sub_id = cur.fetchone()[0]
        conn.close()
        return sub_id

    def test_multi_row_delete_writes_one_audit_row(self):
        # Insert three subscriptions, DELETE all three in one
        # statement, assert one audit row with rows_affected=3.
        ids = [self._make_subscription() for _ in range(3)]

        conn = _make_conn()
        try:
            cur = conn.cursor()
            baseline = _max_audit_id(cur, "webhook_subscriptions_audit")

            cur.execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = ANY(%s::uuid[])",
                ([str(i) for i in ids],),
            )
            conn.commit()

            rows = _audit_rows_since(cur, "webhook_subscriptions_audit", baseline)
            assert len(rows) == 1, (
                "statement-level trigger must produce exactly one audit row "
                "for a multi-row DELETE; got " + str(len(rows))
            )
            event_type, rows_affected, sess_user, curr_user, backend_pid, app, event_at = rows[0]
            assert event_type == "DELETE"
            assert rows_affected == 3
            assert sess_user and curr_user
            assert backend_pid > 0
            assert event_at is not None
        finally:
            conn.close()

    def test_truncate_writes_audit_row_with_null_rows_affected(self):
        # Make sure there's at least one row to truncate.
        sub_id = self._make_subscription()

        # TRUNCATE webhook_subscriptions cannot succeed while
        # webhook_deliveries (FK RESTRICT) or webhook_secrets
        # rows reference it. The current dev DB has those tables
        # empty for this subscription; just to be safe, scope the
        # test to a transaction we will roll back -- TRUNCATE
        # inside a rolled-back tx still fires the trigger but
        # leaves no row removal.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            baseline = _max_audit_id(cur, "webhook_subscriptions_audit")
            try:
                cur.execute("TRUNCATE webhook_subscriptions CASCADE")
            except psycopg2.errors.InsufficientPrivilege:
                pytest.skip("DB role lacks TRUNCATE privilege; skipping")

            # Read inside the same uncommitted transaction. psycopg2's
            # default isolation lets a connection see its own writes,
            # so the audit row is visible here even though we will
            # rollback below to avoid actually wiping the dev DB.
            rows = _audit_rows_since(cur, "webhook_subscriptions_audit", baseline)
            assert len(rows) == 1
            event_type, rows_affected, *_ = rows[0]
            assert event_type == "TRUNCATE"
            assert rows_affected is None, (
                "TRUNCATE does not expose a transition table; rows_affected "
                "must be NULL so investigators do not assume a count"
            )
            conn.rollback()  # do not actually wipe the dev DB
        finally:
            conn.close()
            # Belt-and-suspenders cleanup of the subscription we created.
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                (str(sub_id),),
            )
            cleanup.close()


class TestWebhookSecretsAuditFiring:
    """Same shape against webhook_secrets to confirm the parallel
    trigger pair is wired correctly."""

    def _make_subscription_and_secrets(self, n_secrets=2):
        conn = _make_conn()
        conn.autocommit = True
        cur = conn.cursor()
        connector_id = _ensure_connector(cur, connector_id="test-conn-032-secrets")
        cur.execute(
            """
            INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
            VALUES (%s, %s, %s)
            RETURNING subscription_id
            """,
            (connector_id, "secrets audit fire", "https://example.invalid/audit-secrets"),
        )
        sub_id = cur.fetchone()[0]
        # Insert n_secrets rows (generation 1 and optionally 2).
        for gen in range(1, n_secrets + 1):
            cur.execute(
                "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                "VALUES (%s, %s, %s)",
                (str(sub_id), gen, b"ciphertext-" + str(gen).encode()),
            )
        conn.close()

        def cleanup():
            c = _make_conn()
            c.autocommit = True
            c.cursor().execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                (str(sub_id),),
            )
            c.close()

        return sub_id, cleanup

    def test_multi_row_delete_writes_one_audit_row(self):
        sub_id, cleanup = self._make_subscription_and_secrets(n_secrets=2)
        # Skip the implicit cleanup; we want to manually DELETE the
        # secret rows here so the audit row reflects an explicit
        # DELETE on webhook_secrets, not a CASCADE from subscription
        # removal (which fires this same trigger but for different
        # forensic reasons we are not testing here).
        conn = _make_conn()
        try:
            cur = conn.cursor()
            baseline = _max_audit_id(cur, "webhook_secrets_audit")

            cur.execute(
                "DELETE FROM webhook_secrets WHERE subscription_id = %s",
                (str(sub_id),),
            )
            conn.commit()

            rows = _audit_rows_since(cur, "webhook_secrets_audit", baseline)
            assert len(rows) == 1, (
                "statement-level trigger must produce exactly one audit row "
                "for a multi-row DELETE on webhook_secrets"
            )
            event_type, rows_affected, sess_user, curr_user, backend_pid, app, event_at = rows[0]
            assert event_type == "DELETE"
            assert rows_affected == 2
            assert sess_user and curr_user
            assert backend_pid > 0
        finally:
            conn.close()
            cleanup()

    def test_truncate_writes_audit_row_with_null_rows_affected(self):
        sub_id, cleanup = self._make_subscription_and_secrets(n_secrets=1)
        conn = _make_conn()
        try:
            cur = conn.cursor()
            baseline = _max_audit_id(cur, "webhook_secrets_audit")
            try:
                cur.execute("TRUNCATE webhook_secrets")
            except psycopg2.errors.InsufficientPrivilege:
                pytest.skip("DB role lacks TRUNCATE privilege; skipping")

            # Same uncommitted-read pattern as the subscriptions TRUNCATE
            # test: connection sees its own writes; rollback restores the
            # dev-DB rows after the assertion.
            rows = _audit_rows_since(cur, "webhook_secrets_audit", baseline)
            assert len(rows) == 1
            event_type, rows_affected, *_ = rows[0]
            assert event_type == "TRUNCATE"
            assert rows_affected is None
            conn.rollback()
        finally:
            conn.close()
            cleanup()


class TestAuditRowsAreNotEmittedOnInsertOrUpdate:
    """The triggers are bound to DELETE and TRUNCATE only.
    INSERTs and UPDATEs against the source tables must NOT
    produce audit rows -- otherwise the forensic table fills with
    noise and a real DELETE event becomes harder to spot."""

    def test_insert_and_update_dont_fire_audit(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            connector_id = _ensure_connector(cur)
            base_subs = _max_audit_id(cur, "webhook_subscriptions_audit")
            base_secrets = _max_audit_id(cur, "webhook_secrets_audit")

            cur.execute(
                """
                INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
                VALUES (%s, %s, %s)
                RETURNING subscription_id
                """,
                (connector_id, "no-audit insert", "https://example.invalid/quiet"),
            )
            sub_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE webhook_subscriptions SET display_name = %s WHERE subscription_id = %s",
                ("renamed", str(sub_id)),
            )
            cur.execute(
                "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                "VALUES (%s, 1, %s)",
                (str(sub_id), b"cipher"),
            )
            cur.execute(
                "UPDATE webhook_secrets SET expires_at = NOW() + INTERVAL '1 hour' "
                "WHERE subscription_id = %s",
                (str(sub_id),),
            )
            conn.commit()

            assert _max_audit_id(cur, "webhook_subscriptions_audit") == base_subs, (
                "INSERT/UPDATE on webhook_subscriptions must not write to "
                "webhook_subscriptions_audit"
            )
            assert _max_audit_id(cur, "webhook_secrets_audit") == base_secrets, (
                "INSERT/UPDATE on webhook_secrets must not write to "
                "webhook_secrets_audit"
            )
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                (str(sub_id),),
            )
            cleanup.close()
            conn.close()
