"""Schema-level tests for migration 029's webhook_secrets table.

Locks: column shapes, the composite primary key (subscription_id,
generation), the CHECK on generation IN (1, 2), and the
ON DELETE CASCADE behavior so secret rows do not outlive their
subscription.
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


def _ensure_connector(cur, connector_id="test-conn-029-secrets"):
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "Migration 029 secrets test connector"),
    )
    return connector_id


def _make_subscription():
    """Return (subscription_id, cleanup_fn). Cleanup deletes the
    subscription, which CASCADEs to webhook_secrets rows."""
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
        (connector_id, "secrets test", "https://example.invalid/hook"),
    )
    sub_id = cur.fetchone()[0]
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


class TestWebhookSecretsShape:
    def test_columns(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'webhook_secrets'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert cols["subscription_id"] == ("uuid", "NO")
        assert cols["generation"] == ("smallint", "NO")
        assert cols["secret_ciphertext"] == ("bytea", "NO")
        assert cols["created_at"] == ("timestamp with time zone", "NO")
        assert cols["expires_at"] == ("timestamp with time zone", "YES")

    def test_composite_primary_key(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                 WHERE i.indrelid = 'webhook_secrets'::regclass
                   AND i.indisprimary
                 ORDER BY array_position(i.indkey::int[], a.attnum::int)
                """
            )
            cols = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        assert cols == ["subscription_id", "generation"], (
            "PK must be (subscription_id, generation) so two slots per "
            "subscription are addressable without a separate uniqueness "
            "constraint"
        )


class TestWebhookSecretsGenerationCheck:
    def test_generation_zero_rejected(self):
        sub_id, cleanup = _make_subscription()
        try:
            conn = _make_conn()
            try:
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                        "VALUES (%s, 0, %s)",
                        (str(sub_id), b"ciphertext"),
                    )
                conn.rollback()
            finally:
                conn.close()
        finally:
            cleanup()

    def test_generation_three_rejected(self):
        sub_id, cleanup = _make_subscription()
        try:
            conn = _make_conn()
            try:
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                        "VALUES (%s, 3, %s)",
                        (str(sub_id), b"ciphertext"),
                    )
                conn.rollback()
            finally:
                conn.close()
        finally:
            cleanup()

    def test_generation_one_and_two_accepted(self):
        sub_id, cleanup = _make_subscription()
        try:
            conn = _make_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                    "VALUES (%s, 1, %s), (%s, 2, %s)",
                    (str(sub_id), b"primary-cipher", str(sub_id), b"previous-cipher"),
                )
                conn.commit()
                cur.execute(
                    "SELECT COUNT(*) FROM webhook_secrets WHERE subscription_id = %s",
                    (str(sub_id),),
                )
                assert cur.fetchone()[0] == 2
            finally:
                conn.close()
        finally:
            cleanup()


class TestWebhookSecretsForeignKeyCascade:
    def test_subscription_delete_cascades_to_secrets(self):
        sub_id, _ = _make_subscription()
        # Insert two secret rows.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                "VALUES (%s, 1, %s), (%s, 2, %s)",
                (str(sub_id), b"c1", str(sub_id), b"c2"),
            )
            conn.commit()
            cur.execute(
                "SELECT COUNT(*) FROM webhook_secrets WHERE subscription_id = %s",
                (str(sub_id),),
            )
            assert cur.fetchone()[0] == 2
        finally:
            conn.close()

        # Delete the subscription; CASCADE must drop both secret rows.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                (str(sub_id),),
            )
            conn.commit()
            cur.execute(
                "SELECT COUNT(*) FROM webhook_secrets WHERE subscription_id = %s",
                (str(sub_id),),
            )
            assert cur.fetchone()[0] == 0, (
                "ON DELETE CASCADE on webhook_secrets.subscription_id is the "
                "invariant that keeps secret material from outliving the "
                "subscription it belongs to"
            )
        finally:
            conn.close()


class TestWebhookSecretsForeignKeyReject:
    def test_orphan_subscription_id_rejected(self):
        bogus = uuid.uuid4()
        conn = _make_conn()
        try:
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
                    "VALUES (%s, 1, %s)",
                    (str(bogus), b"orphan-cipher"),
                )
            conn.rollback()
        finally:
            conn.close()
