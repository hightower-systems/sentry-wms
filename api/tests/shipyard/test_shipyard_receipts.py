import uuid

from db_test_context import get_raw_connection
from shipyard.helpers import (
    PNG_BYTES,
    confirm_manifest,
    create_binding,
    create_zone_mapping,
    manifest_bytes,
    pallet_payload,
    receive_ticket,
    single_line_manifest_bytes,
    upload_manifest,
    upload_ticket_photo,
)
from sqlalchemy.exc import OperationalError


def _confirmed_ticket(
    client,
    headers,
    *,
    ship_no,
    quantity=4,
    drawing="D-001",
):
    create_binding(client, headers, ship_no, drawing=drawing)
    create_zone_mapping(client, headers, ship_no)
    manifest_no = f"IN-{uuid.uuid4().hex[:10]}"
    preview = upload_manifest(
        client,
        headers,
        single_line_manifest_bytes(
            ship_no,
            quantity=quantity,
            drawing=drawing,
        ),
        manifest_no=manifest_no,
    )
    assert preview.status_code == 201, preview.get_json()
    manifest_id = preview.get_json()["manifest"]["manifest_id"]
    confirmed = confirm_manifest(client, headers, manifest_id)
    return confirmed["tickets"][0], manifest_id


def _photo_id(client, headers, ticket_id, suffix, **kwargs):
    response = upload_ticket_photo(
        client,
        headers,
        ticket_id,
        tag_suffix=suffix,
        **kwargs,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["photo"]["photo_id"]


def test_scan_validates_ticket_without_persisting_stock(client, auth_headers):
    ticket, _manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9201",
    )
    payload = pallet_payload(
        photo_id=str(uuid.uuid4()),
        ship_no="H9201",
        tag_suffix="1-1",
    )
    payload.pop("photo_id")
    scan = client.post(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/scan",
        json=payload,
        headers=auth_headers,
    )
    assert scan.status_code == 200, scan.get_json()
    assert scan.get_json()["matched"] is True
    assert scan.get_json()["pallet"]["tag_suffix"] == "1-1"
    assert scan.get_json()["ticket"]["received_quantity"] == "0.000"

    wrong_ship = client.post(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/scan",
        json={**payload, "ship_no": "H9999"},
        headers=auth_headers,
    )
    assert wrong_ship.status_code == 422
    assert wrong_ship.get_json()["error"] == "pallet_validation_failed"


def test_full_receipt_books_stock_photos_and_is_idempotent(client, auth_headers):
    ticket, manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9202",
    )
    photo_id = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-1",
    )
    pallet = pallet_payload(
        photo_id=photo_id,
        ship_no="H9202",
        tag_suffix="1-1",
    )
    group_photo_id = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        None,
        photo_kind="GROUP",
    )
    area_photo_id = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        None,
        photo_kind="AREA",
    )
    idempotency_key = f"receipt-{uuid.uuid4()}"
    received = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[pallet],
        idempotency_key=idempotency_key,
        group_photo_id=group_photo_id,
        area_photo_id=area_photo_id,
    )
    assert received.status_code == 200, received.get_json()
    data = received.get_json()
    assert data["ticket"]["status"] == "IN_STOCK"
    assert data["ticket"]["received_quantity"] == "4.000"
    assert data["ticket"]["on_hand_quantity"] == "4.000"
    assert data["ticket"]["shortage_status"] == "NONE"
    assert data["manifest"]["status"] == "CLOSED"
    assert data["pallets"][0]["tag_suffix"] == "1-1"
    assert data["pallets"][0]["photo"]["photo_id"] == photo_id
    assert {photo["photo_kind"] for photo in data["evidence"]} == {
        "GROUP",
        "AREA",
    }

    repeated = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[pallet],
        idempotency_key=idempotency_key,
        group_photo_id=group_photo_id,
        area_photo_id=area_photo_id,
    )
    assert repeated.status_code == 200, repeated.get_json()
    assert (
        repeated.get_json()["receipt"]["receipt_id"] == (data["receipt"]["receipt_id"])
    )

    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM shipyard_receipts
              WHERE ticket_id = %s),
            (SELECT COUNT(*) FROM shipyard_pallet_units
              WHERE ticket_id = %s),
            (SELECT COUNT(*) FROM shipyard_stock_ledger
              WHERE ticket_id = %s)
        """,
        (ticket["ticket_id"], ticket["ticket_id"], ticket["ticket_id"]),
    )
    assert cur.fetchone() == (1, 1, 1)
    cur.close()

    manifest = client.get(
        f"/api/shipyard/inbound/manifests/{manifest_id}",
        headers=auth_headers,
    )
    assert manifest.status_code == 200
    summary = manifest.get_json()["validation"]["summary"]
    assert summary["received_ticket_count"] == 1
    assert summary["pending_ticket_count"] == 0
    assert summary["total_received_quantity"] == "4.000"
    assert summary["total_short_quantity"] == "0.000"

    photo = client.get(
        f"/api/shipyard/photos/{photo_id}",
        headers=auth_headers,
    )
    assert photo.status_code == 200
    assert photo.data == PNG_BYTES

    already_received = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[pallet],
        idempotency_key=f"second-{uuid.uuid4()}",
    )
    assert already_received.status_code == 409
    assert already_received.get_json()["error"] == "ticket_state_conflict"

    no_shortage = client.put(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/shortage",
        json={"status": "REPORTED", "note": "不应允许"},
        headers=auth_headers,
    )
    assert no_shortage.status_code == 409
    assert no_shortage.get_json()["error"] == "shortage_state_conflict"


def test_partial_receipt_tracks_shortage_and_replenishment(client, auth_headers):
    ticket, manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9203",
    )
    first_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-1",
    )
    difference_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        None,
        photo_kind="DIFFERENCE",
    )
    first = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=3,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9203",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"partial-{uuid.uuid4()}",
        difference_reason="少 1 件，已联系工厂补货",
        difference_photo_id=difference_photo,
    )
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["ticket"]["status"] == "PARTIAL"
    assert first.get_json()["ticket"]["short_quantity"] == "1.000"
    assert first.get_json()["ticket"]["shortage_status"] == "PENDING_REPORT"
    assert first.get_json()["manifest"]["status"] == "PARTIAL"

    reported = client.put(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/shortage",
        json={"status": "REPORTED", "note": "已微信通知工厂补 1 件"},
        headers=auth_headers,
    )
    assert reported.status_code == 200, reported.get_json()
    assert reported.get_json()["ticket"]["shortage_status"] == "REPORTED"

    second_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-2",
    )
    second = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=1,
        pallets=[
            pallet_payload(
                photo_id=second_photo,
                ship_no="H9203",
                tag_suffix="1-2",
            )
        ],
        idempotency_key=f"replenish-{uuid.uuid4()}",
    )
    assert second.status_code == 200, second.get_json()
    assert second.get_json()["ticket"]["status"] == "IN_STOCK"
    assert second.get_json()["ticket"]["received_quantity"] == "4.000"
    assert second.get_json()["ticket"]["on_hand_quantity"] == "4.000"
    assert second.get_json()["ticket"]["shortage_status"] == "REPLENISHED"
    assert second.get_json()["manifest"]["status"] == "CLOSED"

    manifest = client.get(
        f"/api/shipyard/inbound/manifests/{manifest_id}",
        headers=auth_headers,
    )
    assert (
        manifest.get_json()["validation"]["summary"]["total_short_quantity"] == "0.000"
    )


def test_receipt_rejects_mixed_suffix_group_and_duplicate_suffix(client, auth_headers):
    ticket, _manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9204",
    )
    first_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-1",
    )
    second_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "2-1",
    )
    mixed = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9204",
                tag_suffix="1-1",
            ),
            pallet_payload(
                photo_id=second_photo,
                ship_no="H9204",
                tag_suffix="2-1",
                qr_payload="QR-H9204-SECOND",
            ),
        ],
        idempotency_key=f"mixed-{uuid.uuid4()}",
    )
    assert mixed.status_code == 422
    assert mixed.get_json()["error"] == "pallet_validation_failed"

    duplicate_suffix = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9204",
                tag_suffix="1-1",
            ),
            pallet_payload(
                photo_id=second_photo,
                ship_no="H9204",
                tag_suffix="1-1",
                qr_payload="QR-H9204-SECOND",
            ),
        ],
        idempotency_key=f"duplicate-suffix-{uuid.uuid4()}",
    )
    assert duplicate_suffix.status_code == 422
    assert duplicate_suffix.get_json()["error"] == "pallet_validation_failed"

    duplicate_qr = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9204",
                tag_suffix="1-1",
                qr_payload="QR-H9204-DUPLICATE",
            ),
            pallet_payload(
                photo_id=second_photo,
                ship_no="H9204",
                tag_suffix="1-2",
                qr_payload="QR-H9204-DUPLICATE",
            ),
        ],
        idempotency_key=f"duplicate-qr-{uuid.uuid4()}",
    )
    assert duplicate_qr.status_code == 409
    assert duplicate_qr.get_json()["error"] == "pallet_duplicate"


def test_receipt_rejects_wrong_or_reused_photo(client, auth_headers):
    ticket, _manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9205",
    )
    wrong_suffix_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-2",
    )
    wrong_suffix = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=wrong_suffix_photo,
                ship_no="H9205",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"wrong-photo-{uuid.uuid4()}",
    )
    assert wrong_suffix.status_code == 422
    assert wrong_suffix.get_json()["error"] == "photo_validation_failed"

    group_photo_id = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        None,
        photo_kind="GROUP",
    )
    wrong_kind = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=group_photo_id,
                ship_no="H9205",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"wrong-kind-{uuid.uuid4()}",
    )
    assert wrong_kind.status_code == 422
    assert wrong_kind.get_json()["error"] == "photo_validation_failed"

    missing_photo = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=str(uuid.uuid4()),
                ship_no="H9205",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"missing-photo-{uuid.uuid4()}",
    )
    assert missing_photo.status_code == 422
    assert missing_photo.get_json()["error"] == "photo_validation_failed"

    invalid_kind = upload_ticket_photo(
        client,
        auth_headers,
        ticket["ticket_id"],
        photo_kind="UNKNOWN",
        content=PNG_BYTES,
        file_name="invalid.png",
    )
    assert invalid_kind.status_code == 422
    assert invalid_kind.get_json()["error"] == "photo_validation_failed"


def test_receipt_rejects_over_receipt_and_duplicate_qr(client, auth_headers):
    first_ticket, _manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9206",
        drawing="D-001",
    )
    photo_id = _photo_id(
        client,
        auth_headers,
        first_ticket["ticket_id"],
        "1-1",
    )
    over = receive_ticket(
        client,
        auth_headers,
        first_ticket["ticket_id"],
        quantity=5,
        pallets=[
            pallet_payload(
                photo_id=photo_id,
                ship_no="H9206",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"over-{uuid.uuid4()}",
    )
    assert over.status_code == 422
    assert over.get_json()["error"] == "pallet_validation_failed"

    create_binding(client, auth_headers, "H9207", drawing="D-001")
    create_binding(client, auth_headers, "H9207", drawing="D-002")
    create_zone_mapping(client, auth_headers, "H9207")
    preview = upload_manifest(
        client,
        auth_headers,
        manifest_bytes("H9207"),
        manifest_no=f"IN-{uuid.uuid4().hex[:10]}",
    )
    assert preview.status_code == 201, preview.get_json()
    preview_data = preview.get_json()
    confirmed = confirm_manifest(
        client,
        auth_headers,
        preview_data["manifest"]["manifest_id"],
    )
    tickets_by_line = {
        ticket["inbound_line_id"]: ticket for ticket in confirmed["tickets"]
    }
    first = tickets_by_line[preview_data["lines"][0]["line_id"]]
    second = tickets_by_line[preview_data["lines"][1]["line_id"]]
    shared_qr = f"QR-DUPLICATE-{uuid.uuid4()}"
    first_photo = _photo_id(
        client,
        auth_headers,
        first["ticket_id"],
        "1-1",
    )
    first_receipt = receive_ticket(
        client,
        auth_headers,
        first["ticket_id"],
        quantity=15,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9207",
                tag_suffix="1-1",
                drawing="D-001",
                qr_payload=shared_qr,
                label_quantity=15,
            )
        ],
        idempotency_key=f"first-{uuid.uuid4()}",
    )
    assert first_receipt.status_code == 200, first_receipt.get_json()

    second_photo = _photo_id(
        client,
        auth_headers,
        second["ticket_id"],
        "2-1",
    )
    duplicate_qr = receive_ticket(
        client,
        auth_headers,
        second["ticket_id"],
        quantity=5,
        pallets=[
            pallet_payload(
                photo_id=second_photo,
                ship_no="H9207",
                tag_suffix="2-1",
                drawing="D-002",
                qr_payload=shared_qr,
                label_quantity=5,
            )
        ],
        idempotency_key=f"second-{uuid.uuid4()}",
    )
    assert duplicate_qr.status_code == 409
    assert duplicate_qr.get_json()["error"] == "pallet_duplicate"


def test_photo_endpoint_rejects_non_image_payload(client, auth_headers):
    ticket, _manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9208",
    )
    response = upload_ticket_photo(
        client,
        auth_headers,
        ticket["ticket_id"],
        tag_suffix="1-1",
        content=b"not-an-image",
        file_name="fake.png",
    )
    assert response.status_code == 415
    assert response.get_json()["error"] == "unsupported_file_type"


def test_expected_pallet_count_and_group_prefix_are_enforced(client, auth_headers):
    ticket, _manifest_id = _confirmed_ticket(
        client,
        auth_headers,
        ship_no="H9209",
    )
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE shipyard_cargo_tickets
           SET expected_pallet_count = 2
         WHERE ticket_id = %s
        """,
        (ticket["ticket_id"],),
    )
    cur.close()

    first_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-1",
    )
    incomplete_group = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9209",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"incomplete-{uuid.uuid4()}",
    )
    assert incomplete_group.status_code == 422
    assert incomplete_group.get_json()["error"] == "pallet_validation_failed"

    partial = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=3,
        pallets=[
            pallet_payload(
                photo_id=first_photo,
                ship_no="H9209",
                tag_suffix="1-1",
            )
        ],
        idempotency_key=f"partial-count-{uuid.uuid4()}",
    )
    assert partial.status_code == 200, partial.get_json()

    wrong_prefix_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "2-2",
    )
    wrong_prefix = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=1,
        pallets=[
            pallet_payload(
                photo_id=wrong_prefix_photo,
                ship_no="H9209",
                tag_suffix="2-2",
            )
        ],
        idempotency_key=f"wrong-prefix-{uuid.uuid4()}",
    )
    assert wrong_prefix.status_code == 422
    assert wrong_prefix.get_json()["error"] == "pallet_validation_failed"

    second_photo = _photo_id(
        client,
        auth_headers,
        ticket["ticket_id"],
        "1-2",
    )
    completed = receive_ticket(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=1,
        pallets=[
            pallet_payload(
                photo_id=second_photo,
                ship_no="H9209",
                tag_suffix="1-2",
            )
        ],
        idempotency_key=f"completed-{uuid.uuid4()}",
    )
    assert completed.status_code == 200, completed.get_json()
    assert completed.get_json()["ticket"]["pallet_count"] == 2
    assert completed.get_json()["ticket"]["status"] == "IN_STOCK"


def test_unexpected_database_failure_returns_503(
    client,
    auth_headers,
    monkeypatch,
):
    from routes.shipyard import inbound as inbound_routes

    def fail(_db, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("database down"))

    monkeypatch.setattr(inbound_routes, "list_manifests", fail)
    response = client.get(
        "/api/shipyard/inbound/manifests?warehouse_id=1",
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "shipyard_database_unavailable"}
