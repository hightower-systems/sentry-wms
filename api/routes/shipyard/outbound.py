"""Distribution-demand import, dispatch matching and delivery-note printing."""

from __future__ import annotations

import html
from io import BytesIO

from flask import Response, g, jsonify, request, send_file
from sqlalchemy import text

from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from schemas.shipyard import (
    ConfirmDistributionImportRequest,
    CreateDeliveryNoteRequest,
    CreateDispatchRequest,
    MatchDispatchLineRequest,
    VoidDistributionImportRequest,
)
from services.shipyard.distribution_parser import DistributionParseError
from services.shipyard.distribution_service import (
    confirm_distribution_import,
    create_distribution_import,
    get_distribution_file,
    get_distribution_import_payload,
    list_distribution_imports,
    void_distribution_import,
)
from services.shipyard.outbound_service import (
    create_delivery_note,
    create_dispatch_order,
    get_delivery_note_payload,
    get_dispatch_payload,
    list_delivery_notes,
    list_dispatches,
    match_dispatch_line,
)
from utils.uploads import (
    UploadTooLargeError,
    read_upload_bytes,
    safe_upload_name,
)
from utils.validation import validate_body

from . import require_shipyard_roles, shipyard_bp

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OUTBOUND_PAGE = "shipyard-outbound"
OUTBOUND_DOCUMENT_EDIT_ROLES = ("SHIPYARD_MANAGER",)


def _list_limit() -> int:
    try:
        return max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc


@shipyard_bp.get("/outbound/distribution-imports")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_distribution_imports():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _list_limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        list_distribution_imports(
            g.db,
            warehouse_id=warehouse_id,
            limit=limit,
        )
    )


@shipyard_bp.post("/outbound/distribution-imports/preview")
@require_auth
@require_shipyard_roles(*OUTBOUND_DOCUMENT_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def preview_distribution_import():
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传 Excel 配送需求",
            }
        ), 415
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    source_file_name = safe_upload_name(
        uploaded.filename,
        fallback="distribution-demand.xlsx",
    )
    if not source_file_name.lower().endswith(".xlsx"):
        return jsonify(
            {
                "error": "unsupported_file_type",
                "message": "首期仅接受 .xlsx 配送需求表",
            }
        ), 415
    try:
        warehouse_id = int(request.form.get("warehouse_id", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id is required"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    try:
        content = read_upload_bytes(uploaded)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    if not content:
        return jsonify({"error": "empty_file"}), 400
    try:
        payload = create_distribution_import(
            g.db,
            warehouse_id=warehouse_id,
            source_file_name=source_file_name,
            content_type=XLSX_CONTENT_TYPE,
            content=content,
            created_by=g.current_user["username"],
        )
    except DistributionParseError as exc:
        return jsonify(
            {
                "error": "distribution_parse_failed",
                "message": str(exc),
                "diagnostics": exc.diagnostics,
            }
        ), 422
    return jsonify(payload), 201


@shipyard_bp.get("/outbound/distribution-imports/<uuid:import_id>")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_distribution_import(import_id):
    payload = get_distribution_import_payload(g.db, import_id=str(import_id))
    ok, denied = check_warehouse_access(payload["import"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.get("/outbound/distribution-imports/<uuid:import_id>/file")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def download_distribution_file(import_id):
    payload = get_distribution_file(g.db, import_id=str(import_id))
    ok, denied = check_warehouse_access(payload["warehouse_id"])
    if not ok:
        return denied
    return send_file(
        BytesIO(payload["file_bytes"]),
        mimetype=payload["content_type"],
        as_attachment=True,
        download_name=payload["file_name"],
        max_age=0,
    )


@shipyard_bp.post("/outbound/distribution-imports/<uuid:import_id>/confirm")
@require_auth
@require_shipyard_roles(*OUTBOUND_DOCUMENT_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(ConfirmDistributionImportRequest)
@with_db
def confirm_distribution(import_id, validated):
    del validated
    current = get_distribution_import_payload(g.db, import_id=str(import_id))
    ok, denied = check_warehouse_access(current["import"]["warehouse_id"])
    if not ok:
        return denied
    payload = confirm_distribution_import(
        g.db,
        import_id=str(import_id),
        confirmed_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.post("/outbound/distribution-imports/<uuid:import_id>/void")
@require_auth
@require_shipyard_roles(*OUTBOUND_DOCUMENT_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(VoidDistributionImportRequest)
@with_db
def void_distribution(import_id, validated):
    current = get_distribution_import_payload(g.db, import_id=str(import_id))
    ok, denied = check_warehouse_access(current["import"]["warehouse_id"])
    if not ok:
        return denied
    payload = void_distribution_import(
        g.db,
        import_id=str(import_id),
        reason=validated.reason,
        voided_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.get("/outbound/dispatches")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_dispatches():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _list_limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(list_dispatches(g.db, warehouse_id=warehouse_id, limit=limit))


@shipyard_bp.post("/outbound/dispatches")
@require_auth
@require_shipyard_roles(*OUTBOUND_DOCUMENT_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(CreateDispatchRequest)
@with_db
def create_dispatch(validated):
    ok, denied = check_warehouse_access(validated.warehouse_id)
    if not ok:
        return denied
    payload = create_dispatch_order(
        g.db,
        validated=validated,
        actor=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.get("/outbound/dispatches/<uuid:dispatch_order_id>")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_dispatch(dispatch_order_id):
    payload = get_dispatch_payload(
        g.db,
        dispatch_order_id=str(dispatch_order_id),
    )
    ok, denied = check_warehouse_access(payload["dispatch"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.put("/outbound/dispatch-lines/<uuid:dispatch_line_id>/demand-match")
@require_auth
@require_shipyard_roles(*OUTBOUND_DOCUMENT_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(MatchDispatchLineRequest)
@with_db
def match_dispatch_demand(dispatch_line_id, validated):
    line = g.db.execute(
        text(
            """
            SELECT d.warehouse_id
              FROM shipyard_dispatch_lines dl
              JOIN shipyard_dispatch_orders d
                ON d.dispatch_order_id = dl.dispatch_order_id
             WHERE dl.dispatch_line_id = :dispatch_line_id
            """
        ),
        {"dispatch_line_id": str(dispatch_line_id)},
    ).fetchone()
    if line is None:
        return jsonify({"error": "dispatch_not_found"}), 404
    ok, denied = check_warehouse_access(line.warehouse_id)
    if not ok:
        return denied
    payload = match_dispatch_line(
        g.db,
        dispatch_line_id=str(dispatch_line_id),
        demand_line_id=str(validated.demand_line_id),
        actor=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.post("/outbound/dispatches/<uuid:dispatch_order_id>/delivery-note")
@require_auth
@require_shipyard_roles(*OUTBOUND_DOCUMENT_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(CreateDeliveryNoteRequest)
@with_db
def create_dispatch_delivery_note(dispatch_order_id, validated):
    dispatch = get_dispatch_payload(
        g.db,
        dispatch_order_id=str(dispatch_order_id),
    )
    ok, denied = check_warehouse_access(dispatch["dispatch"]["warehouse_id"])
    if not ok:
        return denied
    payload = create_delivery_note(
        g.db,
        dispatch_order_id=str(dispatch_order_id),
        validated=validated,
        actor=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.get("/outbound/delivery-notes")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_delivery_notes():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _list_limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        list_delivery_notes(
            g.db,
            warehouse_id=warehouse_id,
            limit=limit,
        )
    )


@shipyard_bp.get("/outbound/delivery-notes/<uuid:delivery_note_id>")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_delivery_note(delivery_note_id):
    payload = get_delivery_note_payload(
        g.db,
        delivery_note_id=str(delivery_note_id),
    )
    ok, denied = check_warehouse_access(payload["delivery_note"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.get("/outbound/delivery-notes/<uuid:delivery_note_id>/print")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def print_delivery_note(delivery_note_id):
    payload = get_delivery_note_payload(
        g.db,
        delivery_note_id=str(delivery_note_id),
    )
    ok, denied = check_warehouse_access(payload["delivery_note"]["warehouse_id"])
    if not ok:
        return denied
    note = payload["delivery_note"]
    rows = []
    for index, line in enumerate(payload["lines"], start=1):
        source = ""
        if line["demand_import_no"]:
            source = f"{line['demand_import_no']} / 行 {line['demand_source_row_no']}"
        material = html.escape(line["material_description"])
        if line["material_code"]:
            material = f"{html.escape(line['material_code'])} · {material}"
        if line["specification"]:
            material = (
                f"{material}<br><span class='small'>"
                f"{html.escape(line['specification'])}</span>"
            )
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{html.escape(line['engineering_no'])}</td>"
            f"<td>{html.escape(line['section_no'] or '')}</td>"
            f"<td>{html.escape(line['pallet_no'])}</td>"
            f"<td>{material}</td>"
            f"<td class='num'>{html.escape(line['quantity'])}</td>"
            f"<td>{html.escape(line['unit'])}</td>"
            f"<td>{html.escape(line['keeper_name'] or '')}</td>"
            f"<td>{html.escape(source)}</td>"
            f"<td>{html.escape(line['remarks'] or '')}</td>"
            "</tr>"
        )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>启东前置仓发货单 {html.escape(note["delivery_note_no"])}</title>
  <style>
    @page {{ size: A4 landscape; margin: 10mm; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: "SimSun", "Songti SC", serif; color: #111; margin: 0; }}
    .sheet {{ border: 2px solid #111; }}
    .head {{ display: grid; grid-template-columns: 1fr 2.2fr 1fr; border-bottom: 1px solid #111; }}
    .head > div {{ padding: 3mm; border-right: 1px solid #111; min-height: 18mm; }}
    .head > div:last-child {{ border-right: 0; }}
    .label {{ font-size: 11px; font-weight: 700; }}
    .value {{ font-size: 18px; font-weight: 700; margin-top: 2mm; }}
    .title {{ text-align: center; font-size: 25px; font-weight: 700; padding-top: 4mm; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-right: 1px solid #111; border-bottom: 1px solid #111; padding: 2.2mm; font-size: 12px; }}
    th {{ font-weight: 700; text-align: center; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    td.num {{ text-align: right; }}
    tbody tr:nth-child(-n+5) {{ height: 12mm; }}
    .source {{ padding: 2.5mm 3mm; border-bottom: 1px solid #111; font-size: 11px; }}
    .signatures {{ display: grid; grid-template-columns: repeat(3, 1fr); }}
    .signatures > div {{ min-height: 22mm; padding: 3mm; border-right: 1px solid #111; }}
    .signatures > div:last-child {{ border-right: 0; }}
    .small {{ font-size: 11px; color: #333; }}
  </style>
</head>
<body>
  <main class="sheet">
    <div class="head">
      <div><div class="label">发放日期</div><div class="value">{html.escape(note["issue_date"])}</div></div>
      <div><div class="title">启东前置仓发货单</div></div>
      <div><div class="label">单号</div><div class="value">{html.escape(note["delivery_note_no"])}</div></div>
    </div>
    <div class="source">
      来源派工单：{html.escape(note["dispatch_no"])}
      <span class="small">　派工日期：{html.escape(note["dispatch_date"])}</span>
      <span class="small">　来源文件：{html.escape(note["source_file_name"] or "手工录入")}</span>
    </div>
    <table>
      <colgroup>
        <col style="width:4%"><col style="width:8%"><col style="width:5%">
        <col style="width:13%"><col style="width:28%"><col style="width:6%">
        <col style="width:4%"><col style="width:9%"><col style="width:12%">
        <col style="width:11%">
      </colgroup>
      <thead>
        <tr><th>序号</th><th>工程号</th><th>分段</th><th>托盘号</th><th>物资编码 / 描述</th><th>数量</th><th>单位</th><th>保管员</th><th>来源需求</th><th>备注</th></tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div class="signatures">
      <div>前置仓信息员：<br><br>日期：</div>
      <div>前置仓保管员：<br><br>日期：</div>
      <div>接收人：{html.escape(note["receiver_name"] or "")}<br><br>接收日期：{html.escape(note["received_at"] or "")}</div>
    </div>
  </main>
</body>
</html>"""
    return Response(document, mimetype="text/html")
