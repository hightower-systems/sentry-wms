"""Schema tests for migration 085 (distribution, dispatch and delivery notes)."""

import hashlib
import os
import uuid

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "db",
    "migrations",
    "085_shipyard_outbound_documents.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_outbound_document_tables_exist():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name = ANY(%s)
            """,
            (
                [
                    "shipyard_distribution_imports",
                    "shipyard_distribution_files",
                    "shipyard_distribution_lines",
                    "shipyard_dispatch_orders",
                    "shipyard_dispatch_lines",
                    "shipyard_delivery_notes",
                    "shipyard_delivery_lines",
                ],
            ),
        )
        assert {row[0] for row in cur.fetchall()} == {
            "shipyard_distribution_imports",
            "shipyard_distribution_files",
            "shipyard_distribution_lines",
            "shipyard_dispatch_orders",
            "shipyard_dispatch_lines",
            "shipyard_delivery_notes",
            "shipyard_delivery_lines",
        }
    finally:
        conn.rollback()
        conn.close()


def test_distribution_lines_retain_source_row_hash():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT data_type, character_maximum_length
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_distribution_lines'
               AND column_name = 'source_row_hash'
            """
        )
        assert cur.fetchone() == ("character", 64)
    finally:
        conn.rollback()
        conn.close()


def test_delivery_notes_retain_source_and_receiver_fields():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND (
                   (table_name = 'shipyard_delivery_notes'
                    AND column_name IN ('receiver_name', 'received_at'))
                   OR
                   (table_name = 'shipyard_delivery_lines'
                    AND column_name IN (
                        'section_no', 'material_code', 'material_name',
                        'specification', 'demand_import_no',
                        'demand_source_row_no'
                    ))
               )
            """
        )
        assert {tuple(row) for row in cur.fetchall()} == {
            ("shipyard_delivery_notes", "receiver_name"),
            ("shipyard_delivery_notes", "received_at"),
            ("shipyard_delivery_lines", "section_no"),
            ("shipyard_delivery_lines", "material_code"),
            ("shipyard_delivery_lines", "material_name"),
            ("shipyard_delivery_lines", "specification"),
            ("shipyard_delivery_lines", "demand_import_no"),
            ("shipyard_delivery_lines", "demand_source_row_no"),
        }
    finally:
        conn.rollback()
        conn.close()


def test_dispatch_idempotency_and_one_note_per_dispatch():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        warehouse_id = 1
        idempotency_key = f"dispatch-{uuid.uuid4()}"
        dispatch_no = f"PT-{uuid.uuid4().hex[:12]}"
        cur.execute(
            """
            INSERT INTO shipyard_dispatch_orders
                (warehouse_id, dispatch_no, dispatch_date, idempotency_key,
                 created_by)
            VALUES
                (%s, %s, '2026-10-27', %s, 'tester')
            RETURNING dispatch_order_id
            """,
            (warehouse_id, dispatch_no, idempotency_key),
        )
        dispatch_id = cur.fetchone()[0]
        try:
            cur.execute(
                """
                INSERT INTO shipyard_dispatch_orders
                    (warehouse_id, dispatch_no, dispatch_date,
                     idempotency_key, created_by)
                VALUES
                    (%s, %s, '2026-10-27', %s, 'tester')
                """,
                (warehouse_id, f"PT-{uuid.uuid4().hex[:12]}", idempotency_key),
            )
            assert False, "duplicate dispatch idempotency key should fail"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_dispatch_orders
                (warehouse_id, dispatch_no, dispatch_date, idempotency_key,
                 created_by)
            VALUES
                (%s, %s, '2026-10-27', %s, 'tester')
            RETURNING dispatch_order_id
            """,
            (
                warehouse_id,
                f"PT-{uuid.uuid4().hex[:12]}",
                f"dispatch-{uuid.uuid4()}",
            ),
        )
        dispatch_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO shipyard_delivery_notes
                (warehouse_id, delivery_note_no, dispatch_order_id,
                 issue_date, idempotency_key, created_by)
            VALUES
                (%s, %s, %s, '2026-10-27', %s, 'tester')
            """,
            (
                warehouse_id,
                f"DN-{uuid.uuid4().hex[:12]}",
                dispatch_id,
                f"note-{uuid.uuid4()}",
            ),
        )
        try:
            cur.execute(
                """
                INSERT INTO shipyard_delivery_notes
                    (warehouse_id, delivery_note_no, dispatch_order_id,
                     issue_date, idempotency_key, created_by)
                VALUES
                    (%s, %s, %s, '2026-10-27', %s, 'tester')
                """,
                (
                    warehouse_id,
                    f"DN-{uuid.uuid4().hex[:12]}",
                    dispatch_id,
                    f"note-{uuid.uuid4()}",
                ),
            )
            assert False, "one dispatch may only have one delivery note"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
    finally:
        conn.close()


def test_distribution_file_hash_ignores_voided_imports():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        file_hash = _sha(f"file-{uuid.uuid4()}")
        first_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO shipyard_distribution_imports
                (import_id, warehouse_id, import_no, source_file_name,
                 source_file_sha256, source_template_version, created_by)
            VALUES
                (%s, 1, %s, 'distribution.xlsx', %s, 'v1', 'tester')
            """,
            (first_id, f"DIST-{uuid.uuid4().hex[:10]}", file_hash),
        )
        cur.execute(
            """
            UPDATE shipyard_distribution_imports
               SET status = 'VOID'
             WHERE import_id = %s
            """,
            (first_id,),
        )
        cur.execute(
            """
            INSERT INTO shipyard_distribution_imports
                (warehouse_id, import_no, source_file_name,
                 source_file_sha256, source_template_version, created_by)
            VALUES
                (1, %s, 'distribution.xlsx', %s, 'v1', 'tester')
            """,
            (f"DIST-{uuid.uuid4().hex[:10]}", file_hash),
        )
    finally:
        conn.rollback()
        conn.close()


def test_migration_is_idempotent():
    with open(MIGRATION_PATH, encoding="utf-8") as migration:
        sql = migration.read()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()
