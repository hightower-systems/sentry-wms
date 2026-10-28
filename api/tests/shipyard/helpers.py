"""Shared fixtures for shipyard API workflow tests."""

from io import BytesIO

from db_test_context import get_raw_connection
from openpyxl import Workbook

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def workbook_bytes(rows, title="外高桥"):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    auxiliary = workbook.create_sheet("Sheet1")
    auxiliary.append([None, None])
    auxiliary.append(["辅助物料", 24])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def manifest_bytes(ship_no="H9001", manufacturer="测试船舶配件有限公司"):
    return workbook_bytes(
        [
            [manufacturer],
            ["2026舾装件发货清单"],
            [
                "序号",
                "船号",
                "品名",
                "图号",
                "图名",
                "件数",
                "PO NO",
                "送货单号",
                "PO号",
                "重量",
                "螺丝",
            ],
            [
                "1",
                ship_no,
                "舾装件",
                "D-001",
                "制作图A",
                15,
                "9001",
                "0929501",
                "SWS202600001",
                39,
                "基座",
            ],
            [
                "2",
                ship_no,
                "舾装件",
                "D-002",
                "制作图B",
                5,
                "9002",
                "0929501",
                "SWS202600002",
                8,
                "托架",
            ],
        ]
    )


def single_line_manifest_bytes(
    ship_no,
    *,
    quantity=4,
    drawing="D-001",
    manufacturer="测试船舶配件有限公司",
    material_code=None,
    item_name="舾装件",
    item_spec=None,
):
    headers = ["序号", "船号", "品名", "图号", "图名", "件数"]
    values = ["1", ship_no, item_name, drawing, "制作图A", quantity]
    if material_code is not None:
        headers.insert(3, "物料编码")
        values.insert(3, material_code)
    if item_spec is not None:
        headers.insert(-1, "型号规格")
        values.insert(-1, item_spec)
    return workbook_bytes(
        [
            [manufacturer],
            ["2026舾装件发货清单"],
            headers,
            values,
        ]
    )


def manifest_bytes_without_drawing(ship_no, manufacturer="测试船舶配件有限公司"):
    return workbook_bytes(
        [
            [manufacturer],
            ["2026舾装件发货清单"],
            ["序号", "船号", "品名", "件数"],
            ["1", ship_no, "舾装件", 5],
        ]
    )


def distribution_demand_bytes():
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
                "配送资源2",
                "配送员2",
                "配送资源3",
                "配送员3",
                "确认人",
                "完成时间",
                "确认状态",
            ],
            [
                "H1632",
                "106",
                "106FE120121",
                "106分段电舾装件安装托盘表(B)",
                "已完成",
                "未到",
                "测试申请人(10001)",
                "总装一部>分段舾装作业区",
                "5号风雨棚",
                "测试申请人",
                "13800000001",
                "2026-10-26 10:30:00~2026-10-26 11:00:00",
                "5T叉车",
                "测试配送员A",
                None,
                None,
                None,
                None,
                None,
                "2026-10-24 14:10:40",
                None,
            ],
            [
                "H1632",
                "106",
                "106FP120141",
                "106分段管支架安装托盘表(B)",
                "已完成",
                "未到",
                "测试申请人(10001)",
                "总装一部>分段舾装作业区",
                "5号风雨棚",
                "测试申请人",
                "13800000001",
                "2026-10-26 10:30:00~2026-10-26 11:00:00",
                "5T叉车",
                "测试配送员A",
                None,
                None,
                None,
                None,
                None,
                "2026-10-24 14:10:40",
                None,
            ],
            [
                "H1617",
                "172",
                "172FE120121",
                "172分段电舾件安装托盘表(B)",
                "已完成",
                "未到",
                "测试申请人(10002)",
                "总装一部>分段舾装作业区",
                "18号风雨棚",
                "测试申请人",
                "13800000002",
                "2026-10-26 10:00:00~2026-10-26 10:30:00",
                "5T叉车",
                "测试配送员B",
                None,
                None,
                None,
                None,
                None,
                "2026-10-26 09:30:00",
                None,
            ],
        ]
    )


def dispatch_line_payload(
    *,
    line_no="1",
    engineering_no="H1404",
    pallet_no="E063-4002(1008)",
    material_spec_raw="E063-4002(1008) 0083-85 电舾件",
    quantity=4,
    demand_line_id=None,
):
    return {
        "line_no": line_no,
        "engineering_no": engineering_no,
        "pallet_no": pallet_no,
        "material_spec_raw": material_spec_raw,
        "material_code": None,
        "material_name": "电舾件",
        "specification": "0083-85",
        "quantity": quantity,
        "unit": "件",
        "expected_arrival_date": "2026-10-28",
        "keeper_name": "测试保管员",
        "keeper_phone": "13800000003",
        "remarks": "0#平台",
        "demand_line_id": demand_line_id,
    }


def outbound_manifest_bytes(
    *,
    factory_shipment_no="CX-20261027-001",
    ship_no="H9301",
    engineering_no=None,
    drawing="D-001",
    manufacturer="测试船舶配件有限公司",
    material_code="MAT-001",
    item_name="船用支架",
    item_spec="S-100",
    demand_quantity=4,
    expected_pallet_count=2,
    suffixes="1-1、1-2",
):
    return workbook_bytes(
        [
            ["外高桥船厂正式出库清单"],
            [
                "清单行号",
                "船厂出货单号",
                "托组号",
                "船号",
                "工程号",
                "制作图号",
                "厂家",
                "物料编码",
                "物料名称",
                "规格型号",
                "需求数量",
                "单位",
                "需求日期",
                "预计托盘数",
                "托盘后缀",
            ],
            [
                "1",
                factory_shipment_no,
                "T930101",
                ship_no,
                engineering_no or ship_no,
                drawing,
                manufacturer,
                material_code,
                item_name,
                item_spec,
                demand_quantity,
                "件",
                "2026-10-27",
                expected_pallet_count,
                suffixes,
            ],
        ]
    )


def first_bin():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT b.bin_id, b.bin_code, z.zone_code
          FROM bins b
          JOIN zones z ON z.zone_id = b.zone_id
         WHERE b.warehouse_id = 1
         ORDER BY b.bin_id
         LIMIT 1
        """
    )
    row = cur.fetchone()
    cur.close()
    return row


def create_binding(client, headers, ship_no, drawing="D-001"):
    response = client.post(
        "/api/shipyard/bindings",
        json={
            "ship_no": ship_no,
            "engineering_no": ship_no,
            "production_drawing_no": drawing,
            "manufacturer_name": "测试船舶配件有限公司",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["binding"]


def create_zone_mapping(client, headers, ship_no):
    bin_id, _bin_code, _zone_code = first_bin()
    response = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": ship_no,
            "is_primary": True,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["mapping"]


def upload_manifest(
    client,
    headers,
    content,
    manifest_no="IN-H9001-001",
):
    return client.post(
        "/api/shipyard/inbound/manifests/preview",
        data={
            "warehouse_id": "1",
            "manifest_no": manifest_no,
            "file": (BytesIO(content), "外高桥发货清单.xlsx"),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def upload_distribution_import(
    client,
    headers,
    content,
    *,
    file_name="distribution-demand.xlsx",
    warehouse_id=1,
):
    return client.post(
        "/api/shipyard/outbound/distribution-imports/preview",
        data={
            "warehouse_id": str(warehouse_id),
            "file": (BytesIO(content), file_name),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def confirm_manifest(client, headers, manifest_id):
    response = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/confirm",
        json={},
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def upload_ticket_photo(
    client,
    headers,
    ticket_id,
    *,
    tag_suffix=None,
    photo_kind="PALLET",
    content=PNG_BYTES,
    file_name="pallet.png",
):
    data = {"photo_kind": photo_kind}
    if tag_suffix is not None:
        data["tag_suffix"] = tag_suffix
    return client.post(
        f"/api/shipyard/inbound/tickets/{ticket_id}/photos",
        data={
            **data,
            "file": (BytesIO(content), file_name),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def receive_ticket(
    client,
    headers,
    ticket_id,
    *,
    quantity=4,
    pallets,
    idempotency_key,
    difference_reason=None,
    group_photo_id=None,
    area_photo_id=None,
    difference_photo_id=None,
):
    payload = {
        "idempotency_key": idempotency_key,
        "received_quantity": quantity,
        "pallets": pallets,
    }
    if difference_reason is not None:
        payload["difference_reason"] = difference_reason
    if group_photo_id is not None:
        payload["group_photo_id"] = group_photo_id
    if area_photo_id is not None:
        payload["area_photo_id"] = area_photo_id
    if difference_photo_id is not None:
        payload["difference_photo_id"] = difference_photo_id
    return client.post(
        f"/api/shipyard/inbound/tickets/{ticket_id}/receipts",
        json=payload,
        headers=headers,
    )


def pallet_payload(
    *,
    photo_id,
    tag_suffix=None,
    ship_no="H9201",
    engineering_no=None,
    drawing="D-001",
    qr_payload=None,
    label_quantity=4,
    batch_no=None,
):
    return {
        "tag_suffix": tag_suffix,
        "qr_payload": qr_payload or f"QR-{ship_no}-{drawing}-{tag_suffix or 'single'}",
        "ship_no": ship_no,
        "engineering_no": engineering_no or ship_no,
        "production_drawing_no": drawing,
        "qr_pallet_no": drawing,
        "batch_no": batch_no or f"{ship_no}-BATCH-1",
        "manufacturer_name": "测试船舶配件有限公司",
        "installation_pallet_no": f"INSTALL-{tag_suffix or 'single'}",
        "installation_pallet_name": "制作图A",
        "destination": "外高桥",
        "label_quantity": label_quantity,
        "photo_id": photo_id,
    }


def upload_outbound_manifest(
    client,
    headers,
    content,
    *,
    file_name="shipyard-outbound.xlsx",
    warehouse_id=1,
):
    return client.post(
        "/api/shipyard/outbound/formal-manifests/preview",
        data={
            "warehouse_id": str(warehouse_id),
            "file": (BytesIO(content), file_name),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def upload_outbound_photo(
    client,
    headers,
    task_id,
    *,
    tag_suffix,
    content=PNG_BYTES,
    file_name="outbound-pallet.png",
):
    return client.post(
        f"/api/shipyard/outbound/tasks/{task_id}/photos",
        data={
            "tag_suffix": tag_suffix,
            "file": (BytesIO(content), file_name),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def outbound_scan_payload(
    task,
    suffix,
    *,
    qr_payload,
    photo_id,
    material_code=None,
    item_name=None,
    batch_no=None,
    label_quantity=None,
):
    return {
        "tag_suffix": suffix,
        "qr_payload": qr_payload,
        "ship_no": task["ship_no"],
        "engineering_no": task["engineering_no"],
        "production_drawing_no": task["production_drawing_no"],
        "material_code": material_code or task["material_code"],
        "item_name": item_name or task["item_name"],
        "batch_no": batch_no,
        "label_quantity": label_quantity,
        "photo_id": photo_id,
    }
