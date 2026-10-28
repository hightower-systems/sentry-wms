"""Schema-level tests for migration 082 (shipyard domain core)."""

import hashlib
import os
import uuid

import psycopg2
from psycopg2.extras import Json

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "db",
    "migrations",
    "082_shipyard_core.sql",
)


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bin_ids(conn, count=2):
    cur = conn.cursor()
    cur.execute("SELECT bin_id FROM bins ORDER BY bin_id LIMIT %s", (count,))
    ids = [row[0] for row in cur.fetchall()]
    cur.close()
    assert len(ids) == count
    return ids


def _make_manifest(conn, warehouse_id=1):
    cur = conn.cursor()
    manifest_no = f"IN-82-{uuid.uuid4().hex[:10]}"
    cur.execute(
        """
        INSERT INTO shipyard_inbound_manifests
            (warehouse_id, manifest_no, source_type, source_file_name,
             source_file_sha256, source_template_version, status,
             metadata, created_by)
        VALUES (%s, %s, 'EXCEL', 'inbound.xlsx', %s, 'v1',
                'PARSED', %s, 'tester')
        RETURNING manifest_id
        """,
        (
            warehouse_id,
            manifest_no,
            _sha(manifest_no),
            Json({"sheet": "外高桥"}),
        ),
    )
    manifest_id = cur.fetchone()[0]
    cur.close()
    return manifest_id


def _make_line(conn, manifest_id, bin_id, source_row_no="1", ship_no="H1633"):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO shipyard_inbound_lines
            (manifest_id, source_row_no, source_row_hash, ship_no,
             engineering_no, production_drawing_no, manufacturer_name,
             item_name, item_spec, declared_quantity, assigned_bin_id,
             raw_data, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 45, %s, %s, 'PENDING')
        RETURNING line_id
        """,
        (
            manifest_id,
            source_row_no,
            _sha(f"{manifest_id}:{source_row_no}"),
            ship_no,
            ship_no,
            "132ME201EM",
            "上海杰宇",
            "舾装件",
            "132分段电舾装件制作图（B）",
            bin_id,
            Json({"raw_quantity": "45"}),
        ),
    )
    line_id = cur.fetchone()[0]
    cur.close()
    return line_id


def _make_ticket(conn, line_id, bin_id, quantity=45, suffix="82"):
    cur = conn.cursor()
    ticket_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO shipyard_cargo_tickets
            (ticket_id, ticket_no, inbound_line_id, item_id,
             declared_quantity, received_quantity, expected_pallet_count,
             assigned_bin_id, location_path_snapshot, stock_lot_number,
             status, created_by)
        VALUES (%s, %s, %s, 1, %s, 0, 1, %s, 'A / A1', %s,
                'EXPECTED', 'tester')
        RETURNING ticket_id
        """,
        (
            str(ticket_id),
            f"TICKET-82-{suffix}-{uuid.uuid4().hex[:8]}",
            line_id,
            quantity,
            bin_id,
            str(ticket_id),
        ),
    )
    returned_ticket_id = cur.fetchone()[0]
    cur.close()
    return returned_ticket_id


def _make_pallet(conn, ticket_id, suffix="1-1", payload=None):
    payload = payload or f"QR-{uuid.uuid4().hex}"
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO shipyard_pallet_units
            (ticket_id, tag_suffix, qr_payload, qr_payload_sha256,
             factory_pallet_no, installation_pallet_no, batch_no, status)
        VALUES (%s, %s, %s, %s, '132ME201EM', '132FE120121',
                'H1633杰宇-0', 'EXPECTED')
        RETURNING pallet_id
        """,
        (ticket_id, suffix, payload, _sha(payload)),
    )
    pallet_id = cur.fetchone()[0]
    cur.close()
    return pallet_id


def test_expected_tables_and_columns_exist():
    expected = {
        "shipyard_project_bindings": {"ship_no", "engineering_no", "status"},
        "shipyard_zone_ship_mappings": {"bin_id", "ship_no", "is_primary"},
        "shipyard_inbound_manifests": {"manifest_no", "source_file_sha256"},
        "shipyard_inbound_lines": {
            "source_row_no",
            "declared_quantity",
            "drawing_name",
            "delivery_note_no",
            "po_no",
        },
        "shipyard_cargo_tickets": {"inbound_line_id", "stock_lot_number"},
        "shipyard_pallet_units": {"ticket_id", "tag_suffix", "qr_payload_sha256"},
    }
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = ANY(%s)
            """,
            (list(expected),),
        )
        actual = {}
        for table_name, column_name in cur.fetchall():
            actual.setdefault(table_name, set()).add(column_name)
    finally:
        conn.close()

    for table_name, columns in expected.items():
        assert table_name in actual
        assert columns <= actual[table_name]


def test_inbound_line_keeps_fixed_template_context():
    conn = _make_conn()
    try:
        bin_id = _bin_ids(conn, 1)[0]
        manifest_id = _make_manifest(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_inbound_lines
                (manifest_id, source_row_no, ship_no, engineering_no,
                 production_drawing_no, drawing_name, manufacturer_name,
                 delivery_note_no, po_no, item_name, item_spec,
                 declared_quantity, assigned_bin_id, raw_data)
            VALUES (%s, '3', 'H1633', 'H1633', '132ME201EM',
                    '132分段电舾装件制作图（B）', '上海杰宇', 'SHIP-20261026',
                    'PO-9001', '舾装件', '一托', 45, %s, %s)
            RETURNING drawing_name, delivery_note_no, po_no
            """,
            (manifest_id, bin_id, Json({"source_row": 3})),
        )
        assert cur.fetchone() == (
            "132分段电舾装件制作图（B）",
            "SHIP-20261026",
            "PO-9001",
        )
    finally:
        conn.close()


def test_one_active_primary_bin_per_ship():
    conn = _make_conn()
    try:
        bin_a, bin_b = _bin_ids(conn, 2)
        ship_no = f"H82-{uuid.uuid4().hex[:8]}"
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_zone_ship_mappings
                (bin_id, ship_no, is_primary, status)
            VALUES (%s, %s, TRUE, 'ACTIVE')
            """,
            (bin_a, ship_no),
        )
        conn.commit()
        try:
            cur.execute(
                """
                INSERT INTO shipyard_zone_ship_mappings
                    (bin_id, ship_no, is_primary, status)
                VALUES (%s, %s, TRUE, 'ACTIVE')
                """,
                (bin_b, ship_no),
            )
            conn.commit()
            assert False, "second active primary bin should violate UNIQUE"
        except (
            psycopg2.errors.UniqueViolation,
            psycopg2.errors.ExclusionViolation,
        ):
            conn.rollback()
    finally:
        conn.close()


def test_one_ticket_per_inbound_line():
    conn = _make_conn()
    try:
        bin_id = _bin_ids(conn, 1)[0]
        manifest_id = _make_manifest(conn)
        line_id = _make_line(conn, manifest_id, bin_id)
        _make_ticket(conn, line_id, bin_id, suffix="a")
        conn.commit()

        try:
            _make_ticket(conn, line_id, bin_id, suffix="b")
            conn.commit()
            assert False, "second ticket for one inbound line should violate UNIQUE"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
    finally:
        conn.close()


def test_duplicate_pallet_suffix_rejected_within_ticket():
    conn = _make_conn()
    try:
        bin_id = _bin_ids(conn, 1)[0]
        manifest_id = _make_manifest(conn)
        line_id = _make_line(conn, manifest_id, bin_id)
        ticket_id = _make_ticket(conn, line_id, bin_id)
        _make_pallet(conn, ticket_id, suffix="1-1")
        conn.commit()

        try:
            _make_pallet(conn, ticket_id, suffix="1-1")
            conn.commit()
            assert False, "duplicate pallet suffix should violate UNIQUE"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
    finally:
        conn.close()


def test_duplicate_manifest_file_rejected_in_same_warehouse():
    conn = _make_conn()
    try:
        first_manifest = _make_manifest(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT source_file_sha256 FROM shipyard_inbound_manifests "
            "WHERE manifest_id = %s",
            (first_manifest,),
        )
        file_hash = cur.fetchone()[0]
        conn.commit()

        try:
            cur.execute(
                """
                INSERT INTO shipyard_inbound_manifests
                    (warehouse_id, manifest_no, source_type,
                     source_file_sha256, status, created_by)
                VALUES (1, %s, 'EXCEL', %s, 'PARSED', 'tester')
                """,
                (f"IN-DUP-{uuid.uuid4().hex[:8]}", file_hash),
            )
            conn.commit()
            assert False, "duplicate source file hash should violate UNIQUE"
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
    finally:
        conn.close()


def test_quantity_and_status_checks_reject_invalid_values():
    conn = _make_conn()
    try:
        bin_id = _bin_ids(conn, 1)[0]
        manifest_id = _make_manifest(conn)
        conn.commit()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO shipyard_inbound_lines
                    (manifest_id, source_row_no, ship_no, item_name,
                     declared_quantity, assigned_bin_id, status)
                VALUES (%s, %s, 'H1633', '舾装件', 0, %s, 'PENDING')
                """,
                (manifest_id, f"bad-{uuid.uuid4().hex[:8]}", bin_id),
            )
            conn.commit()
            assert False, "zero declared quantity should violate CHECK"
        except psycopg2.errors.CheckViolation:
            conn.rollback()

        line_id = _make_line(conn, manifest_id, bin_id, source_row_no="ok-2")
        conn.commit()
        try:
            cur.execute(
                """
                INSERT INTO shipyard_cargo_tickets
                    (ticket_no, inbound_line_id, declared_quantity,
                     received_quantity, assigned_bin_id,
                     location_path_snapshot, stock_lot_number, status,
                     created_by)
                VALUES (%s, %s, 10, -1, %s, 'A / A1', %s,
                        'EXPECTED', 'tester')
                """,
                (
                    f"TICKET-BAD-{uuid.uuid4().hex[:8]}",
                    line_id,
                    bin_id,
                    str(uuid.uuid4()),
                ),
            )
            conn.commit()
            assert False, "negative received quantity should violate CHECK"
        except psycopg2.errors.CheckViolation:
            conn.rollback()
    finally:
        conn.close()


def test_invalid_pallet_suffix_rejected():
    conn = _make_conn()
    try:
        bin_id = _bin_ids(conn, 1)[0]
        manifest_id = _make_manifest(conn)
        line_id = _make_line(conn, manifest_id, bin_id)
        ticket_id = _make_ticket(conn, line_id, bin_id)
        conn.commit()
        try:
            _make_pallet(conn, ticket_id, suffix="pallet-one")
            conn.commit()
            assert False, "non 1-1 style suffix should violate CHECK"
        except psycopg2.errors.CheckViolation:
            conn.rollback()
    finally:
        conn.close()


def test_deleting_ticket_cascades_to_pallets():
    conn = _make_conn()
    try:
        bin_id = _bin_ids(conn, 1)[0]
        manifest_id = _make_manifest(conn)
        line_id = _make_line(conn, manifest_id, bin_id)
        ticket_id = _make_ticket(conn, line_id, bin_id)
        pallet_id = _make_pallet(conn, ticket_id)
        conn.commit()

        cur = conn.cursor()
        cur.execute(
            "DELETE FROM shipyard_cargo_tickets WHERE ticket_id = %s",
            (ticket_id,),
        )
        conn.commit()
        cur.execute(
            "SELECT COUNT(*) FROM shipyard_pallet_units WHERE pallet_id = %s",
            (pallet_id,),
        )
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_migration_is_idempotent():
    with open(MIGRATION_PATH, encoding="utf-8") as migration:
        sql = migration.read()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(sql)
        later_migration = os.path.join(
            os.path.dirname(MIGRATION_PATH),
            "083_shipyard_integrity.sql",
        )
        if os.path.exists(later_migration):
            with open(later_migration, encoding="utf-8") as migration:
                cur.execute(migration.read())
    finally:
        conn.close()
