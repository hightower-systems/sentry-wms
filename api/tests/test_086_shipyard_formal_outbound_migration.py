"""Schema tests for migration 086 (formal outbound and loading)."""

import os

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "db",
    "migrations",
    "086_shipyard_formal_outbound.sql",
)


def _connection():
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = False
    return connection


def test_formal_outbound_tables_and_columns_exist():
    connection = _connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name = ANY(%s)
            """,
            (
                [
                    "shipyard_outbound_manifests",
                    "shipyard_outbound_manifest_files",
                    "shipyard_outbound_manifest_lines",
                    "shipyard_outbound_orders",
                    "shipyard_outbound_order_lines",
                    "shipyard_outbound_tasks",
                    "shipyard_outbound_confirmations",
                    "shipyard_outbound_confirmation_pallets",
                    "shipyard_outbound_loading_confirmations",
                ],
            ),
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "shipyard_outbound_manifests",
            "shipyard_outbound_manifest_files",
            "shipyard_outbound_manifest_lines",
            "shipyard_outbound_orders",
            "shipyard_outbound_order_lines",
            "shipyard_outbound_tasks",
            "shipyard_outbound_confirmations",
            "shipyard_outbound_confirmation_pallets",
            "shipyard_outbound_loading_confirmations",
        }
        cursor.execute(
            """
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND (
                   (table_name = 'shipyard_inbound_lines'
                    AND column_name = 'material_code')
                   OR
                   (table_name = 'shipyard_photos'
                    AND column_name IN (
                        'outbound_task_id', 'outbound_confirmation_id'
                    ))
               )
            """
        )
        assert {tuple(row) for row in cursor.fetchall()} == {
            ("shipyard_inbound_lines", "material_code"),
            ("shipyard_photos", "outbound_task_id"),
            ("shipyard_photos", "outbound_confirmation_id"),
        }
    finally:
        connection.rollback()
        connection.close()


def test_formal_outbound_constraints_guard_whole_ticket_tasks():
    connection = _connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT conname
              FROM pg_constraint
             WHERE conrelid = 'shipyard_outbound_tasks'::regclass
                OR conrelid = 'shipyard_outbound_confirmations'::regclass
                OR conrelid = 'shipyard_outbound_loading_confirmations'::regclass
            """
        )
        names = {row[0] for row in cursor.fetchall()}
        assert "uq_shipyard_outbound_task_ticket" in names
        assert "shipyard_outbound_task_pallets" in names
        assert "uq_shipyard_outbound_confirmation_task" in names
        assert "uq_shipyard_outbound_loading_order" in names
    finally:
        connection.rollback()
        connection.close()
