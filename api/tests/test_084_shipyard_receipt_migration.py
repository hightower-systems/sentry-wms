"""Schema tests for migration 084 (pallet receipt and photo evidence)."""

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
    "084_shipyard_receipt.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ticket_fixture(conn):
    bin_id = conn.cursor()
    bin_id.execute(
        "SELECT bin_id FROM bins WHERE warehouse_id = 1 ORDER BY bin_id LIMIT 1"
    )
    storage_bin_id = bin_id.fetchone()[0]
    bin_id.close()
    manifest_id = uuid.uuid4()
    line_id = uuid.uuid4()
    ticket_id = uuid.uuid4()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO shipyard_inbound_manifests
            (manifest_id, warehouse_id, manifest_no, status, created_by)
        VALUES (%s, 1, %s, 'CONFIRMED', 'tester')
        """,
        (manifest_id, f"IN-84-{uuid.uuid4().hex[:10]}"),
    )
    cur.execute(
        """
        INSERT INTO shipyard_inbound_lines
            (line_id, manifest_id, source_row_no, ship_no,
             production_drawing_no, item_name, declared_quantity,
             assigned_bin_id)
        VALUES
            (%s, %s, '1', 'H8400', 'D-84', 'Test', 4, %s)
        """,
        (line_id, manifest_id, storage_bin_id),
    )
    cur.execute(
        """
        INSERT INTO shipyard_cargo_tickets
            (ticket_id, ticket_no, inbound_line_id, declared_quantity,
             assigned_bin_id, location_path_snapshot,
             stock_lot_number, created_by)
        VALUES
            (%s, %s, %s, 4, %s, 'A / A1', %s, 'tester')
        """,
        (
            ticket_id,
            f"SY-84-{uuid.uuid4().hex[:10]}",
            line_id,
            storage_bin_id,
            str(ticket_id),
        ),
    )
    cur.close()
    return storage_bin_id, manifest_id, line_id, ticket_id


def test_receipt_schema_objects_and_columns_exist():
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
                    "shipyard_receipts",
                    "shipyard_receipt_pallets",
                    "shipyard_stock_ledger",
                    "shipyard_photos",
                ],
            ),
        )
        assert {row[0] for row in cur.fetchall()} == {
            "shipyard_receipts",
            "shipyard_receipt_pallets",
            "shipyard_stock_ledger",
            "shipyard_photos",
        }
        cur.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name = 'shipyard_cargo_tickets'
               AND column_name = ANY(%s)
            """,
            (["on_hand_quantity", "shortage_status"],),
        )
        assert {row[0] for row in cur.fetchall()} == {
            "on_hand_quantity",
            "shortage_status",
        }
        cur.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE indexname = 'uq_shipyard_pallet_ticket_suffix_normalized'
            """
        )
        assert cur.fetchone() is not None
    finally:
        conn.rollback()
        conn.close()


def test_receipt_idempotency_and_ticket_quantity_constraints():
    conn = _make_conn()
    try:
        storage_bin_id, _manifest_id, _line_id, ticket_id = _ticket_fixture(conn)
        idempotency_key = f"receipt-{uuid.uuid4()}"
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_receipts
                (ticket_id, idempotency_key,
                 previous_received_quantity, received_quantity,
                 balance_after, resulting_ticket_status, bin_id,
                 location_path_snapshot, received_by)
            VALUES
                (%s, %s, 0, 1, 1, 'PARTIAL', %s, 'A / A1', 'tester')
            """,
            (ticket_id, idempotency_key, storage_bin_id),
        )
        try:
            cur.execute(
                """
                INSERT INTO shipyard_receipts
                    (ticket_id, idempotency_key,
                     previous_received_quantity, received_quantity,
                     balance_after, resulting_ticket_status, bin_id,
                     location_path_snapshot, received_by)
                VALUES
                    (%s, %s, 0, 1, 1, 'PARTIAL', %s, 'A / A1', 'tester')
                """,
                (ticket_id, idempotency_key, storage_bin_id),
            )
            assert False, "duplicate receipt idempotency key should be rejected"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()

        storage_bin_id, _manifest_id, _line_id, ticket_id = _ticket_fixture(conn)
        cur = conn.cursor()
        try:
            cur.execute(
                """
                UPDATE shipyard_cargo_tickets
                   SET received_quantity = 1,
                       on_hand_quantity = 2
                 WHERE ticket_id = %s
                """,
                (ticket_id,),
            )
            assert False, "on-hand quantity cannot exceed received quantity"
        except psycopg2.errors.CheckViolation:
            conn.rollback()
    finally:
        conn.close()


def test_null_pallet_suffix_is_unique_within_ticket():
    conn = _make_conn()
    try:
        _storage_bin_id, _manifest_id, _line_id, ticket_id = _ticket_fixture(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_pallet_units
                (ticket_id, qr_payload, qr_payload_sha256)
            VALUES
                (%s, 'first', %s)
            """,
            (ticket_id, _sha("first")),
        )
        try:
            cur.execute(
                """
                INSERT INTO shipyard_pallet_units
                    (ticket_id, qr_payload, qr_payload_sha256)
                VALUES
                    (%s, 'second', %s)
                """,
                (ticket_id, _sha("second")),
            )
            assert False, "a ticket cannot have two pallets with a null suffix"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
    finally:
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
