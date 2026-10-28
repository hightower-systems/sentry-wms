"""Schema tests for migration 087 (photo-based label OCR)."""

import os

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "db",
    "migrations",
    "087_shipyard_label_ocr.sql",
)


def _connection():
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = False
    return connection


def test_ocr_recognition_table_and_evidence_columns_exist():
    connection = _connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_pallet_units'
               AND column_name IN (
                   'qr_payload', 'qr_payload_sha256', 'label_fields'
               )
             ORDER BY column_name
            """
        )
        assert cursor.fetchall() == [
            ("label_fields", "jsonb", "NO"),
            ("qr_payload", "text", "YES"),
            ("qr_payload_sha256", "character", "YES"),
        ]

        cursor.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_label_recognitions'
            """
        )
        columns = dict(cursor.fetchall())
        assert columns["photo_id"] == "uuid"
        assert columns["ticket_id"] == "uuid"
        assert columns["task_id"] == "uuid"
        assert columns["fields"] == "jsonb"
        assert columns["status"] == "character varying"

        cursor.execute(
            """
            SELECT is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_outbound_confirmation_pallets'
               AND column_name = 'qr_payload'
            """
        )
        assert cursor.fetchone() == ("YES",)

        cursor.execute(
            """
            SELECT data_type
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_outbound_confirmation_pallets'
               AND column_name = 'label_recognition_id'
            """
        )
        assert cursor.fetchone() == ("uuid",)
    finally:
        connection.rollback()
        connection.close()


def test_migration_is_idempotent():
    with open(MIGRATION_PATH, encoding="utf-8") as migration:
        sql = migration.read()
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = True
    try:
        connection.cursor().execute(sql)
        connection.cursor().execute(sql)
    finally:
        connection.close()
