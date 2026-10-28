"""Schema tests for migration 083 (shipyard temporal and import integrity)."""

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
    "083_shipyard_integrity.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bin_id(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT bin_id FROM bins ORDER BY bin_id LIMIT 1")
    bin_id = cur.fetchone()[0]
    cur.close()
    return bin_id


def test_temporal_and_qr_constraints_exist():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT conname
              FROM pg_constraint
             WHERE conname = ANY(%s)
            """,
            (
                [
                    "shipyard_project_binding_no_overlap",
                    "shipyard_zone_ship_primary_no_overlap",
                    "shipyard_zone_ship_bin_no_overlap",
                ],
            ),
        )
        constraints = {row[0] for row in cur.fetchall()}
        assert constraints == {
            "shipyard_project_binding_no_overlap",
            "shipyard_zone_ship_primary_no_overlap",
            "shipyard_zone_ship_bin_no_overlap",
        }

        cur.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE indexname = 'uq_shipyard_pallet_qr_payload_sha256'
            """
        )
        assert cur.fetchone() is not None
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                   AND table_name = 'shipyard_manifest_files'
            )
            """
        )
        assert cur.fetchone()[0] is True
    finally:
        conn.rollback()
        conn.close()


def test_project_binding_windows_must_not_overlap():
    conn = _make_conn()
    try:
        ship_no = f"H83-{uuid.uuid4().hex[:8]}"
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_project_bindings
                (ship_no, engineering_no, production_drawing_no,
                 manufacturer_name, effective_from, effective_to)
            VALUES
                (%s, 'E-1', 'D-1', 'Factory',
                 '2030-01-01T00:00:00Z', '2030-02-01T00:00:00Z'),
                (%s, 'E-1', 'D-1', 'Factory',
                 '2030-02-01T00:00:00Z', '2030-03-01T00:00:00Z')
            """,
            (ship_no, ship_no),
        )

        try:
            cur.execute(
                """
                INSERT INTO shipyard_project_bindings
                    (ship_no, engineering_no, production_drawing_no,
                     manufacturer_name, effective_from, effective_to)
                VALUES
                    (%s, 'E-1', 'D-1', 'Factory',
                     '2030-01-15T00:00:00Z', '2030-02-15T00:00:00Z')
                """,
                (ship_no,),
            )
            assert False, "overlapping binding window should be rejected"
        except psycopg2.errors.ExclusionViolation:
            conn.rollback()
            assert True
    finally:
        conn.close()


def test_primary_zone_can_be_scheduled_in_adjacent_windows():
    conn = _make_conn()
    try:
        ship_no = f"H83-Z-{uuid.uuid4().hex[:8]}"
        cur = conn.cursor()
        cur.execute("SELECT bin_id FROM bins ORDER BY bin_id LIMIT 2")
        first_bin, second_bin = [row[0] for row in cur.fetchall()]
        cur.execute(
            """
            INSERT INTO shipyard_zone_ship_mappings
                (bin_id, ship_no, is_primary, effective_from, effective_to)
            VALUES
                (%s, %s, TRUE,
                 '2030-01-01T00:00:00Z', '2030-02-01T00:00:00Z'),
                (%s, %s, TRUE,
                 '2030-02-01T00:00:00Z', '2030-03-01T00:00:00Z')
            """,
            (first_bin, ship_no, second_bin, ship_no),
        )
        cur.execute(
            """
            SELECT COUNT(*)
              FROM shipyard_zone_ship_mappings
             WHERE ship_no = %s
            """,
            (ship_no,),
        )
        assert cur.fetchone()[0] == 2
    finally:
        conn.rollback()
        conn.close()


def test_duplicate_tag_qr_hash_is_rejected_across_tickets():
    conn = _make_conn()
    try:
        bin_id = _bin_id(conn)
        manifest_id = uuid.uuid4()
        line_a = uuid.uuid4()
        line_b = uuid.uuid4()
        ticket_a = uuid.uuid4()
        ticket_b = uuid.uuid4()
        qr_hash = _sha(f"QR-{uuid.uuid4()}")
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_inbound_manifests
                (manifest_id, warehouse_id, manifest_no, created_by)
            VALUES (%s, 1, %s, 'tester')
            """,
            (manifest_id, f"IN-83-{uuid.uuid4().hex[:8]}"),
        )
        for line_id, row_no in ((line_a, "1"), (line_b, "2")):
            cur.execute(
                """
                INSERT INTO shipyard_inbound_lines
                    (line_id, manifest_id, source_row_no, ship_no,
                     production_drawing_no, item_name, declared_quantity,
                     assigned_bin_id)
                VALUES (%s, %s, %s, 'H8300', 'D-83', 'Test', 1, %s)
                """,
                (line_id, manifest_id, row_no, bin_id),
            )
        for ticket_id, line_id, suffix in (
            (ticket_a, line_a, "a"),
            (ticket_b, line_b, "b"),
        ):
            cur.execute(
                """
                INSERT INTO shipyard_cargo_tickets
                    (ticket_id, ticket_no, inbound_line_id,
                     declared_quantity, assigned_bin_id,
                     location_path_snapshot, stock_lot_number, created_by)
                VALUES (%s, %s, %s, 1, %s, 'A / A1', %s, 'tester')
                """,
                (
                    ticket_id,
                    f"TK-83-{suffix}-{uuid.uuid4().hex[:8]}",
                    line_id,
                    bin_id,
                    str(ticket_id),
                ),
            )
        cur.execute(
            """
            INSERT INTO shipyard_pallet_units
                (ticket_id, qr_payload, qr_payload_sha256)
            VALUES (%s, 'same-qr', %s)
            """,
            (ticket_a, qr_hash),
        )
        try:
            cur.execute(
                """
                INSERT INTO shipyard_pallet_units
                    (ticket_id, qr_payload, qr_payload_sha256)
                VALUES (%s, 'same-qr', %s)
                """,
                (ticket_b, qr_hash),
            )
            assert False, "duplicate QR payload hash should be rejected"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            assert True
    finally:
        conn.close()


def test_voided_manifest_file_hash_can_be_imported_again():
    conn = _make_conn()
    try:
        file_hash = _sha(f"FILE-{uuid.uuid4()}")
        first_manifest = uuid.uuid4()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_inbound_manifests
                (manifest_id, warehouse_id, manifest_no,
                 source_file_sha256, created_by)
            VALUES (%s, 1, %s, %s, 'tester')
            """,
            (first_manifest, f"IN-V-{uuid.uuid4().hex[:8]}", file_hash),
        )
        cur.execute(
            """
            UPDATE shipyard_inbound_manifests
               SET status = 'VOID'
             WHERE manifest_id = %s
            """,
            (first_manifest,),
        )
        cur.execute(
            """
            INSERT INTO shipyard_inbound_manifests
                (warehouse_id, manifest_no, source_file_sha256, created_by)
            VALUES (1, %s, %s, 'tester')
            """,
            (f"IN-V2-{uuid.uuid4().hex[:8]}", file_hash),
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
