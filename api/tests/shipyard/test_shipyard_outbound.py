import uuid

from db_test_context import get_raw_connection
from shipyard.helpers import (
    dispatch_line_payload,
    distribution_demand_bytes,
    upload_distribution_import,
    workbook_bytes,
)


def _confirm_distribution(client, headers, content, *, file_name):
    preview = upload_distribution_import(
        client,
        headers,
        content,
        file_name=file_name,
    )
    assert preview.status_code == 201, preview.get_json()
    preview_data = preview.get_json()
    import_id = preview_data["import"]["import_id"]
    confirmed = client.post(
        f"/api/shipyard/outbound/distribution-imports/{import_id}/confirm",
        json={},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    return preview_data, confirmed.get_json()


def _duplicate_pallet_import():
    return workbook_bytes(
        [
            [
                "工程号",
                "分段号",
                "托盘号",
                "托盘名称",
                "工单状态",
                None,
                "申请人",
                "部门",
                "配送地址",
                "联系人",
                "手机号",
                "需求时间",
                "配送资源1",
                "配送员1",
                "完成时间",
                "确认状态",
            ],
            [
                "H1632",
                "106",
                "106FP120141",
                "第二版托盘名称",
                "已完成",
                "未到",
                "测试申请人(10001)",
                "总装一部>分段舾装作业区",
                "5号风雨棚",
                "测试申请人",
                "13800000001",
                "2026-10-27 10:30:00~2026-10-27 11:00:00",
                "5T叉车",
                "测试配送员A",
                "2026-10-27 09:00:00",
                None,
            ],
        ]
    )


def _create_dispatch(
    client,
    headers,
    lines,
    dispatch_no=None,
    *,
    warehouse_id=1,
    idempotency_key=None,
):
    return client.post(
        "/api/shipyard/outbound/dispatches",
        json={
            "warehouse_id": warehouse_id,
            "dispatch_no": dispatch_no or f"PT-{uuid.uuid4().hex[:12]}",
            "dispatch_date": "2026-10-27",
            "source_type": "IMAGE_OCR",
            "source_file_name": "dispatch.jpg",
            "idempotency_key": idempotency_key or f"dispatch-{uuid.uuid4()}",
            "lines": lines,
        },
        headers=headers,
    )


def _create_warehouse():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO warehouses
            (warehouse_code, warehouse_name, address)
        VALUES
            (%s, %s, %s)
        RETURNING warehouse_id
        """,
        (
            f"T{uuid.uuid4().hex[:12]}",
            "船厂第二仓库",
            "测试地址",
        ),
    )
    warehouse_id = cur.fetchone()[0]
    cur.close()
    return warehouse_id


def test_distribution_import_preview_file_confirm_and_duplicate(client, auth_headers):
    content = distribution_demand_bytes()
    preview = upload_distribution_import(client, auth_headers, content)
    assert preview.status_code == 201, preview.get_json()
    data = preview.get_json()
    assert data["import"]["status"] == "PARSED"
    assert len(data["lines"]) == 3
    assert data["lines"][0]["delivery_status"] == "未到"
    assert data["warnings"][0]["code"] == "UNNAMED_HEADER_MAPPED"

    import_id = data["import"]["import_id"]
    download = client.get(
        f"/api/shipyard/outbound/distribution-imports/{import_id}/file",
        headers=auth_headers,
    )
    assert download.status_code == 200
    assert download.data == content

    duplicate = upload_distribution_import(client, auth_headers, content)
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"] == "duplicate_distribution_file"
    assert duplicate.get_json()["import_id"] == import_id

    confirmed = client.post(
        f"/api/shipyard/outbound/distribution-imports/{import_id}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["import"]["status"] == "CONFIRMED"

    repeated = client.post(
        f"/api/shipyard/outbound/distribution-imports/{import_id}/confirm",
        json={},
        headers=auth_headers,
    )
    assert repeated.status_code == 409
    assert repeated.get_json()["error"] == "distribution_import_state_conflict"


def test_dispatch_matching_manual_resolution_and_delivery_note_print(
    client, auth_headers
):
    _first_preview, _first_confirmed = _confirm_distribution(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-v1.xlsx",
    )
    second_preview, _second_confirmed = _confirm_distribution(
        client,
        auth_headers,
        _duplicate_pallet_import(),
        file_name="distribution-v2.xlsx",
    )

    dispatch = _create_dispatch(
        client,
        auth_headers,
        [
            dispatch_line_payload(
                line_no="1",
                engineering_no="H1632",
                pallet_no="106FE120121",
            ),
            dispatch_line_payload(
                line_no="2",
                engineering_no="H1632",
                pallet_no="106FP120141",
            ),
            dispatch_line_payload(
                line_no="3",
                engineering_no="H1404",
                pallet_no="E063-4002(1008)",
            ),
        ],
        dispatch_no="PT202610278A00910008",
    )
    assert dispatch.status_code == 201, dispatch.get_json()
    data = dispatch.get_json()
    assert data["match_summary"] == {
        "matched": 1,
        "ambiguous": 1,
        "unmatched": 1,
    }
    assert [line["match_status"] for line in data["lines"]] == [
        "MATCHED",
        "AMBIGUOUS",
        "UNMATCHED",
    ]

    second_demand_line_id = second_preview["lines"][0]["line_id"]
    ambiguous_line = data["lines"][1]
    matched = client.put(
        f"/api/shipyard/outbound/dispatch-lines/{ambiguous_line['dispatch_line_id']}/demand-match",
        json={"demand_line_id": second_demand_line_id},
        headers=auth_headers,
    )
    assert matched.status_code == 200, matched.get_json()
    assert matched.get_json()["lines"][1]["match_status"] == "MANUAL"
    assert matched.get_json()["lines"][1]["demand_line_id"] == (second_demand_line_id)

    dispatch_id = data["dispatch"]["dispatch_order_id"]
    note_key = f"delivery-note-{uuid.uuid4()}"
    note = client.post(
        f"/api/shipyard/outbound/dispatches/{dispatch_id}/delivery-note",
        json={
            "delivery_note_no": "202610270004",
            "issue_date": "2026-10-27",
            "idempotency_key": note_key,
        },
        headers=auth_headers,
    )
    assert note.status_code == 201, note.get_json()
    note_data = note.get_json()
    assert note_data["delivery_note"]["dispatch_no"] == ("PT202610278A00910008")
    assert len(note_data["lines"]) == 3
    assert note_data["lines"][0]["section_no"] == "106"
    assert note_data["lines"][0]["demand_import_no"].startswith("DIST-")
    assert note_data["lines"][0]["demand_source_row_no"] == "2"
    assert note_data["lines"][0]["specification"] == "0083-85"

    repeated = client.post(
        f"/api/shipyard/outbound/dispatches/{dispatch_id}/delivery-note",
        json={
            "delivery_note_no": "202610270004",
            "issue_date": "2026-10-27",
            "idempotency_key": note_key,
        },
        headers=auth_headers,
    )
    assert repeated.status_code == 201
    assert (
        repeated.get_json()["delivery_note"]["delivery_note_id"]
        == (note_data["delivery_note"]["delivery_note_id"])
    )

    note_id = note_data["delivery_note"]["delivery_note_id"]
    printed = client.get(
        f"/api/shipyard/outbound/delivery-notes/{note_id}/print",
        headers=auth_headers,
    )
    assert printed.status_code == 200
    assert printed.mimetype == "text/html"
    body = printed.get_data(as_text=True)
    assert "启东前置仓发货单" in body
    assert "PT202610278A00910008" in body
    assert "H1404" in body
    assert "E063-4002(1008)" in body


def test_dispatch_matching_is_scoped_to_the_dispatch_warehouse(client, auth_headers):
    first = _confirm_distribution(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-warehouse-1.xlsx",
    )[1]
    second_warehouse_id = _create_warehouse()
    second_preview = upload_distribution_import(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-warehouse-2.xlsx",
        warehouse_id=second_warehouse_id,
    )
    assert second_preview.status_code == 201, second_preview.get_json()
    second_import_id = second_preview.get_json()["import"]["import_id"]
    confirmed = client.post(
        f"/api/shipyard/outbound/distribution-imports/{second_import_id}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()

    line = dispatch_line_payload(
        engineering_no="H1632",
        pallet_no="106FE120121",
    )
    first_dispatch = _create_dispatch(client, auth_headers, [line])
    assert first_dispatch.status_code == 201, first_dispatch.get_json()
    assert (
        first_dispatch.get_json()["lines"][0]["demand_import_id"]
        == (first["import"]["import_id"])
    )

    second_dispatch = _create_dispatch(
        client,
        auth_headers,
        [line],
        warehouse_id=second_warehouse_id,
    )
    assert second_dispatch.status_code == 201, second_dispatch.get_json()
    assert second_dispatch.get_json()["lines"][0]["demand_import_id"] == (
        second_import_id
    )


def test_manual_match_rejects_cross_warehouse_and_reused_demand(client, auth_headers):
    first = _confirm_distribution(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-reuse-1.xlsx",
    )[1]
    second_warehouse_id = _create_warehouse()
    second_preview = upload_distribution_import(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-reuse-2.xlsx",
        warehouse_id=second_warehouse_id,
    )
    assert second_preview.status_code == 201, second_preview.get_json()
    second_import_id = second_preview.get_json()["import"]["import_id"]
    confirmed = client.post(
        f"/api/shipyard/outbound/distribution-imports/{second_import_id}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()

    first_dispatch = _create_dispatch(
        client,
        auth_headers,
        [
            dispatch_line_payload(
                engineering_no="H1632",
                pallet_no="106FE120121",
            )
        ],
    )
    assert first_dispatch.status_code == 201, first_dispatch.get_json()

    reused_demand_id = first["lines"][0]["line_id"]
    second_dispatch = _create_dispatch(
        client,
        auth_headers,
        [
            dispatch_line_payload(
                engineering_no="H1632",
                pallet_no="106FE120121",
            )
        ],
    )
    assert second_dispatch.status_code == 201, second_dispatch.get_json()
    assert second_dispatch.get_json()["lines"][0]["match_status"] == "UNMATCHED"

    reused = client.put(
        "/api/shipyard/outbound/dispatch-lines/"
        f"{second_dispatch.get_json()['lines'][0]['dispatch_line_id']}/demand-match",
        json={"demand_line_id": reused_demand_id},
        headers=auth_headers,
    )
    assert reused.status_code == 422
    assert reused.get_json()["error"] == "dispatch_match_failed"

    cross_warehouse = client.put(
        "/api/shipyard/outbound/dispatch-lines/"
        f"{second_dispatch.get_json()['lines'][0]['dispatch_line_id']}/demand-match",
        json={"demand_line_id": second_preview.get_json()["lines"][0]["line_id"]},
        headers=auth_headers,
    )
    assert cross_warehouse.status_code == 422
    assert cross_warehouse.get_json()["error"] == "dispatch_match_failed"


def test_used_distribution_import_cannot_be_voided(client, auth_headers):
    confirmed = _confirm_distribution(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-void-used.xlsx",
    )[1]
    dispatch = _create_dispatch(
        client,
        auth_headers,
        [
            dispatch_line_payload(
                engineering_no="H1632",
                pallet_no="106FE120121",
            )
        ],
    )
    assert dispatch.status_code == 201, dispatch.get_json()

    response = client.post(
        "/api/shipyard/outbound/distribution-imports/"
        f"{confirmed['import']['import_id']}/void",
        json={"reason": "误操作"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "distribution_import_state_conflict"
    assert response.get_json()["linked_dispatch_line_count"] == 1


def test_manual_match_rejects_wrong_pallet(client, auth_headers):
    preview, _confirmed = _confirm_distribution(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-manual.xlsx",
    )
    dispatch = _create_dispatch(
        client,
        auth_headers,
        [
            dispatch_line_payload(
                line_no="1",
                engineering_no="H1617",
                pallet_no="DIFFERENT-PALLET",
            )
        ],
    )
    assert dispatch.status_code == 201, dispatch.get_json()
    dispatch_line_id = dispatch.get_json()["lines"][0]["dispatch_line_id"]

    response = client.put(
        f"/api/shipyard/outbound/dispatch-lines/{dispatch_line_id}/demand-match",
        json={"demand_line_id": preview["lines"][0]["line_id"]},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.get_json()["error"] == "dispatch_match_failed"


def test_idempotency_keys_reject_changed_dispatch_and_delivery_content(
    client, auth_headers
):
    _confirm_distribution(
        client,
        auth_headers,
        distribution_demand_bytes(),
        file_name="distribution-idempotency.xlsx",
    )
    dispatch_key = f"dispatch-{uuid.uuid4()}"
    first = _create_dispatch(
        client,
        auth_headers,
        [dispatch_line_payload(line_no="1")],
        dispatch_no="PT-IDEMPOTENCY-1",
        idempotency_key=dispatch_key,
    )
    assert first.status_code == 201, first.get_json()

    changed = _create_dispatch(
        client,
        auth_headers,
        [dispatch_line_payload(line_no="1", quantity=5)],
        dispatch_no="PT-IDEMPOTENCY-1",
        idempotency_key=dispatch_key,
    )
    assert changed.status_code == 409
    assert changed.get_json()["error"] == "dispatch_state_conflict"

    dispatch_id = first.get_json()["dispatch"]["dispatch_order_id"]
    note_key = f"delivery-note-{uuid.uuid4()}"
    note = client.post(
        f"/api/shipyard/outbound/dispatches/{dispatch_id}/delivery-note",
        json={
            "delivery_note_no": "DN-IDEMPOTENCY-1",
            "issue_date": "2026-10-27",
            "idempotency_key": note_key,
        },
        headers=auth_headers,
    )
    assert note.status_code == 201, note.get_json()

    changed_note = client.post(
        f"/api/shipyard/outbound/dispatches/{dispatch_id}/delivery-note",
        json={
            "delivery_note_no": "DN-IDEMPOTENCY-2",
            "issue_date": "2026-10-27",
            "idempotency_key": f"delivery-note-{uuid.uuid4()}",
        },
        headers=auth_headers,
    )
    assert changed_note.status_code == 409
    assert changed_note.get_json()["error"] == "delivery_note_state_conflict"
