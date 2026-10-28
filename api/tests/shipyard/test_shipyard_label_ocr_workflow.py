"""End-to-end OCR workflow for receiving and whole-ticket outbound."""

import uuid
from io import BytesIO

from db_test_context import get_raw_connection
from shipyard.helpers import (
    PNG_BYTES,
    confirm_manifest,
    create_binding,
    create_zone_mapping,
    outbound_manifest_bytes,
    single_line_manifest_bytes,
    upload_manifest,
    upload_outbound_manifest,
)
from sqlalchemy.exc import OperationalError

from services.shipyard import (
    outbound_turnover_service,
    receipt_service,
)
from services.shipyard.errors import OcrProviderError
from services.shipyard.label_ocr import LabelOcrResult


def _ocr_result(fields):
    return LabelOcrResult(
        provider="baidu",
        raw_text="\n".join(f"{key}: {value}" for key, value in fields.items()),
        raw_lines=[f"{key}: {value}" for key, value in fields.items()],
        fields=fields,
        confidence=0.98,
    )


def _upload_label(client, headers, path):
    return client.post(
        path,
        data={"file": (BytesIO(PNG_BYTES), "pallet-label.png")},
        content_type="multipart/form-data",
        headers=headers,
    )


def _confirmed_ticket(client, headers, ship_no):
    create_binding(client, headers, ship_no, drawing="D-001")
    create_zone_mapping(client, headers, ship_no)
    preview = upload_manifest(
        client,
        headers,
        single_line_manifest_bytes(
            ship_no,
            quantity=4,
            drawing="D-001",
            material_code="MAT-OCR",
            item_name="船用支架",
            item_spec="S-OCR",
        ),
        manifest_no=f"IN-OCR-{uuid.uuid4().hex[:10]}",
    )
    assert preview.status_code == 201, preview.get_json()
    confirmed = confirm_manifest(
        client,
        headers,
        preview.get_json()["manifest"]["manifest_id"],
    )
    ticket = confirmed["tickets"][0]
    connection = get_raw_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE shipyard_cargo_tickets
           SET expected_pallet_count = 2
         WHERE ticket_id = %s
        """,
        (ticket["ticket_id"],),
    )
    cursor.close()
    return ticket


def _preview_outbound(client, headers, ship_no):
    preview = upload_outbound_manifest(
        client,
        headers,
        outbound_manifest_bytes(
            ship_no=ship_no,
            material_code="MAT-OCR",
            item_name="船用支架",
            item_spec="S-OCR",
            demand_quantity=4,
        ),
        file_name="ocr-outbound.xlsx",
    )
    assert preview.status_code == 201, preview.get_json()
    manifest_id = preview.get_json()["manifest"]["manifest_id"]
    confirmed = client.post(
        f"/api/shipyard/outbound/formal-manifests/{manifest_id}/confirm",
        json={},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    return confirmed.get_json()


def test_photo_ocr_receipt_and_outbound_need_no_qr_code(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)
    suffixes = ("1-1", "1-2")
    inbound_results = [
        _ocr_result(
            {
                "ship_no": ship_no,
                "engineering_no": ship_no,
                "production_drawing_no": "D-001",
                "material_code": "MAT-OCR",
                "item_name": "船用支架",
                "item_spec": "S-OCR",
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffix,
                "label_quantity": "4",
            }
        )
        for suffix in suffixes
    ]
    monkeypatch.setattr(
        receipt_service,
        "recognize_label_image",
        lambda _content: inbound_results.pop(0),
    )

    pallets = []
    inbound_photos = []
    for suffix in suffixes:
        response = _upload_label(
            client,
            auth_headers,
            f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/label-recognition",
        )
        assert response.status_code == 201, response.get_json()
        data = response.get_json()
        assert data["matched"] is True
        assert data["recognized"]["tag_suffix"] == suffix
        assert data["recognition"]["status"] == "MATCHED"
        assert data["photo"]["file_sha256"]
        inbound_photos.append(data["photo"]["photo_id"])
        pallets.append(
            {
                "tag_suffix": suffix,
                "label_recognition_id": data["recognition"]["recognition_id"],
                "photo_id": data["photo"]["photo_id"],
            }
        )

    receipt = client.post(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/receipts",
        json={
            "idempotency_key": f"receipt-ocr-{uuid.uuid4()}",
            "client_device_id": "WX-OCR",
            "received_quantity": 4,
            "pallets": pallets,
        },
        headers=auth_headers,
    )
    assert receipt.status_code == 200, receipt.get_json()
    assert receipt.get_json()["ticket"]["status"] == "IN_STOCK"
    assert receipt.get_json()["ticket"]["on_hand_quantity"] == "4.000"
    assert {
        pallet["label_recognition"]["status"]
        for pallet in receipt.get_json()["pallets"]
    } == {"MATCHED"}
    assert all(
        pallet["label_fields"]["material_code"] == "MAT-OCR"
        for pallet in receipt.get_json()["pallets"]
    )

    order = _preview_outbound(client, auth_headers, ship_no)
    task = order["tasks"][0]
    outbound_results = [
        _ocr_result(
            {
                "material_code": "MAT-OCR",
                "item_name": "船用支架",
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffix,
                "label_quantity": "4",
            }
        )
        for suffix in suffixes
    ]
    monkeypatch.setattr(
        outbound_turnover_service,
        "recognize_label_image",
        lambda _content: outbound_results.pop(0),
    )

    evidence = []
    for suffix in suffixes:
        response = _upload_label(
            client,
            auth_headers,
            f"/api/shipyard/outbound/tasks/{task['task_id']}/label-recognition",
        )
        assert response.status_code == 201, response.get_json()
        data = response.get_json()
        assert data["matched"] is True
        assert data["recognition"]["task_id"] == task["task_id"]
        evidence.append(
            {
                "tag_suffix": suffix,
                "label_recognition_id": data["recognition"]["recognition_id"],
                "photo_id": data["photo"]["photo_id"],
            }
        )

    confirmed = client.post(
        f"/api/shipyard/outbound/tasks/{task['task_id']}/confirm",
        json={
            "idempotency_key": f"outbound-ocr-{uuid.uuid4()}",
            "client_device_id": "WX-OCR",
            "pallets": evidence,
        },
        headers=auth_headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    confirmed_data = confirmed.get_json()
    assert confirmed_data["confirmation"]["balance_after"] == "0.000"
    assert confirmed_data["order"]["status"] == "OUTBOUND"
    assert {
        pallet["label_recognition"]["status"] for pallet in confirmed_data["pallets"]
    } == {"MATCHED"}

    loading = client.post(
        f"/api/shipyard/outbound/orders/{order['order']['outbound_order_id']}/loading",
        json={
            "idempotency_key": f"loading-ocr-{uuid.uuid4()}",
            "client_device_id": "WX-OCR",
        },
        headers=auth_headers,
    )
    assert loading.status_code == 200, loading.get_json()
    assert loading.get_json()["order"]["status"] == "LOADED"

    connection = get_raw_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT t.on_hand_quantity, t.status,
               COUNT(DISTINCT p.pallet_id),
               COUNT(DISTINCT p.label_recognition_id),
               COUNT(DISTINCT r.recognition_id)
          FROM shipyard_cargo_tickets t
          JOIN shipyard_outbound_confirmation_pallets p
            ON p.confirmation_id = %s
          JOIN shipyard_label_recognitions r
            ON r.recognition_id = p.label_recognition_id
         WHERE t.ticket_id = %s
         GROUP BY t.ticket_id
        """,
        (
            confirmed_data["confirmation"]["confirmation_id"],
            ticket["ticket_id"],
        ),
    )
    assert cursor.fetchone() == (0, "OUTBOUND", 2, 2, 2)
    cursor.execute(
        """
        SELECT COUNT(*)
          FROM shipyard_pallet_units
         WHERE ticket_id = %s
           AND qr_payload IS NULL
           AND qr_payload_sha256 IS NULL
           AND label_fields ? 'material_code'
           AND label_fields ? 'item_name'
        """,
        (ticket["ticket_id"],),
    )
    assert cursor.fetchone()[0] == 2
    cursor.close()
    assert len(inbound_photos) == 2


def test_missing_required_ocr_field_cannot_be_confirmed(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)
    monkeypatch.setattr(
        receipt_service,
        "recognize_label_image",
        lambda _content: _ocr_result(
            {
                "ship_no": ship_no,
                "item_name": "船用支架",
                "tag_suffix": "1-1",
            }
        ),
    )

    response = _upload_label(
        client,
        auth_headers,
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/label-recognition",
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["matched"] is False
    assert {
        item["field"] for item in data["mismatches"] if item["reason"] == "REQUIRED"
    } == {"material_code"}

    confirmation = client.post(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/receipts",
        json={
            "idempotency_key": f"receipt-missing-ocr-{uuid.uuid4()}",
            "received_quantity": 4,
            "pallets": [
                {
                    "tag_suffix": "1-1",
                    "label_recognition_id": data["recognition"]["recognition_id"],
                    "photo_id": data["photo"]["photo_id"],
                }
            ],
        },
        headers=auth_headers,
    )
    assert confirmation.status_code == 422
    assert confirmation.get_json()["error"] == "pallet_validation_failed"


def test_missing_suffix_is_selected_without_retyping_the_whole_label(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)
    monkeypatch.setattr(
        receipt_service,
        "recognize_label_image",
        lambda _content: _ocr_result(
            {
                "ship_no": ship_no,
                "engineering_no": ship_no,
                "production_drawing_no": "D-001",
                "material_code": "MAT-OCR",
                "item_name": "船用支架",
                "item_spec": "S-OCR",
                "label_quantity": "4",
            }
        ),
    )

    response = _upload_label(
        client,
        auth_headers,
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/label-recognition",
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["matched"] is False
    assert data["needs_suffix"] is True

    confirmation = client.post(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/receipts",
        json={
            "idempotency_key": f"receipt-suffix-ocr-{uuid.uuid4()}",
            "received_quantity": 2,
            "difference_reason": "先到一托，余货次日补到",
            "pallets": [
                {
                    "tag_suffix": "1-1",
                    "label_recognition_id": data["recognition"]["recognition_id"],
                    "photo_id": data["photo"]["photo_id"],
                }
            ],
        },
        headers=auth_headers,
    )
    assert confirmation.status_code == 200, confirmation.get_json()
    assert confirmation.get_json()["ticket"]["status"] == "PARTIAL"


def test_ocr_provider_failure_keeps_photo_and_failed_recognition(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)

    def fail(_content):
        raise OcrProviderError("吊牌 OCR 服务暂时不可用")

    monkeypatch.setattr(receipt_service, "recognize_label_image", fail)

    response = _upload_label(
        client,
        auth_headers,
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/label-recognition",
    )
    assert response.status_code == 502
    data = response.get_json()
    assert data["error"] == "ocr_provider_error"
    assert data["recognition"]["status"] == "FAILED"
    assert data["photo"]["photo_id"]
    assert data["recognition"]["photo_id"] == data["photo"]["photo_id"]


def test_unexpected_database_failure_returns_503(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)

    def fail(_db, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("database down"))

    monkeypatch.setattr(receipt_service, "recognize_ticket_label_photo", fail)
    from routes.shipyard import receipts as receipt_routes

    monkeypatch.setattr(receipt_routes, "recognize_ticket_label_photo", fail)
    response = _upload_label(
        client,
        auth_headers,
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/label-recognition",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "shipyard_database_unavailable"}
