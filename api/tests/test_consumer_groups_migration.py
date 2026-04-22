"""Schema-level tests for migration 021 (v1.5.0).

Asserts that ``connectors`` and ``consumer_groups`` exist with the
columns, defaults, and FK the polling endpoint in #122 will rely on.
Uses a raw psycopg2 connection (not the Flask fixture) because these
tests are pure schema introspection.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


class TestConnectorsTable:
    def test_columns_shape(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'connectors'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
        assert cols["connector_id"] == ("character varying", "NO")
        assert cols["display_name"] == ("character varying", "NO")
        assert cols["created_at"] == ("timestamp with time zone", "NO")
        assert cols["updated_at"] == ("timestamp with time zone", "NO")


class TestConsumerGroupsTable:
    def test_columns_shape(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'consumer_groups'
                 ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        finally:
            conn.close()

        assert cols["consumer_group_id"][:2] == ("character varying", "NO")
        assert cols["connector_id"][:2] == ("character varying", "NO")
        assert cols["last_cursor"][:2] == ("bigint", "NO")
        assert cols["last_cursor"][2].strip() == "0"
        assert cols["last_heartbeat"][:2] == ("timestamp with time zone", "NO")
        assert cols["subscription"][:2] == ("jsonb", "NO")
        # Default is the JSONB empty object; Postgres renders the
        # literal as `'{}'::jsonb`.
        assert cols["subscription"][2] is not None and "jsonb" in cols["subscription"][2]
        assert cols["created_at"][:2] == ("timestamp with time zone", "NO")
        assert cols["updated_at"][:2] == ("timestamp with time zone", "NO")

    def test_connector_fk_exists(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tc.constraint_name, kcu.column_name, ccu.table_name, ccu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'consumer_groups'
                   AND tc.constraint_type = 'FOREIGN KEY'
                """
            )
            fks = cur.fetchall()
        finally:
            conn.close()
        # Exactly one FK: consumer_groups.connector_id -> connectors.connector_id
        assert len(fks) == 1
        _, local_col, parent_table, parent_col = fks[0]
        assert local_col == "connector_id"
        assert parent_table == "connectors"
        assert parent_col == "connector_id"

    def test_inserting_group_without_connector_fails(self):
        conn = _make_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute(
                    "INSERT INTO consumer_groups (consumer_group_id, connector_id) "
                    "VALUES ('orphan', 'does-not-exist')"
                )
        finally:
            conn.close()

    def test_connector_id_is_indexed(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'consumer_groups'"
            )
            names = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        assert "ix_consumer_groups_connector" in names
