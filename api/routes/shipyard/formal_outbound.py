"""Formal shipyard outbound import, mobile tasks and loading confirmation."""

from __future__ import annotations

from io import BytesIO

from flask import g, jsonify, request, send_file

from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from schemas.shipyard import (
    ConfirmOutboundLoadingRequest,
    ConfirmOutboundManifestRequest,
    ConfirmOutboundTaskRequest,
    OutboundPalletScanRequest,
    VoidInboundManifestRequest,
)
from services.shipyard.errors import OutboundManifestStateError
from services.shipyard.outbound_manifest_parser import (
    OutboundManifestParseError,
)
from services.shipyard.outbound_manifest_service import (
    confirm_outbound_manifest,
    create_outbound_manifest_preview,
    get_outbound_manifest_file,
    get_outbound_manifest_payload,
    get_outbound_order_payload,
    list_outbound_manifests,
    list_outbound_orders,
    void_outbound_manifest,
)
from services.shipyard.outbound_turnover_service import (
    confirm_outbound_loading,
    confirm_outbound_task,
    get_outbound_photo,
    get_outbound_task_context,
    recognize_outbound_label_photo,
    save_outbound_task_photo,
    validate_outbound_pallet_scan,
)
from utils.uploads import (
    UnsupportedImageError,
    UploadTooLargeError,
    detect_image_content_type,
    read_upload_bytes,
    safe_upload_name,
)
from utils.validation import validate_body

from . import require_shipyard_roles, shipyard_bp

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OUTBOUND_PAGE = "shipyard-outbound"
FORMAL_OUTBOUND_EDIT_ROLES = ("SHIPYARD_MANAGER",)
FORMAL_OUTBOUND_MOBILE_ROLES = ("SHIPYARD_MANAGER", "OUTBOUND")


def _limit() -> int:
    try:
        return max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc


@shipyard_bp.get("/outbound/formal-manifests")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_formal_outbound_manifests():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        list_outbound_manifests(
            g.db,
            warehouse_id=warehouse_id,
            limit=limit,
        )
    )


@shipyard_bp.post("/outbound/formal-manifests/preview")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def preview_formal_outbound_manifest():
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传正式出库清单",
            }
        ), 415
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    source_file_name = safe_upload_name(
        uploaded.filename,
        fallback="shipyard-outbound.xlsx",
    )
    if not source_file_name.lower().endswith(".xlsx"):
        return jsonify(
            {
                "error": "unsupported_file_type",
                "message": "首期仅接受 .xlsx 正式出库清单",
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
        payload = create_outbound_manifest_preview(
            g.db,
            warehouse_id=warehouse_id,
            manifest_no=request.form.get("manifest_no"),
            factory_shipment_no=request.form.get("factory_shipment_no"),
            source_file_name=source_file_name,
            content_type=XLSX_CONTENT_TYPE,
            content=content,
            created_by=g.current_user["username"],
        )
    except OutboundManifestParseError as exc:
        return jsonify(
            {
                "error": "outbound_manifest_parse_failed",
                "message": str(exc),
                "diagnostics": exc.diagnostics,
            }
        ), 422
    except OutboundManifestStateError as exc:
        if exc.details.get("error_code") == "FACTORY_SHIPMENT_NO_REQUIRED":
            return jsonify(
                {
                    "error": "factory_shipment_no_required",
                    "message": "清单未提供船厂出货单号，请在页面上补充",
                }
            ), 422
        raise
    return jsonify(payload), 201


@shipyard_bp.get("/outbound/formal-manifests/<uuid:manifest_id>")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_formal_outbound_manifest(manifest_id):
    payload = get_outbound_manifest_payload(g.db, manifest_id=str(manifest_id))
    ok, denied = check_warehouse_access(payload["manifest"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.get("/outbound/formal-manifests/<uuid:manifest_id>/file")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def download_formal_outbound_manifest(manifest_id):
    payload = get_outbound_manifest_file(g.db, manifest_id=str(manifest_id))
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


@shipyard_bp.post("/outbound/formal-manifests/<uuid:manifest_id>/confirm")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(ConfirmOutboundManifestRequest)
@with_db
def confirm_formal_outbound_manifest(manifest_id, validated):
    del validated
    current = get_outbound_manifest_payload(g.db, manifest_id=str(manifest_id))
    ok, denied = check_warehouse_access(current["manifest"]["warehouse_id"])
    if not ok:
        return denied
    payload = confirm_outbound_manifest(
        g.db,
        manifest_id=str(manifest_id),
        confirmed_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.post("/outbound/formal-manifests/<uuid:manifest_id>/void")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_EDIT_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(VoidInboundManifestRequest)
@with_db
def void_formal_outbound_manifest(manifest_id, validated):
    current = get_outbound_manifest_payload(g.db, manifest_id=str(manifest_id))
    ok, denied = check_warehouse_access(current["manifest"]["warehouse_id"])
    if not ok:
        return denied
    payload = void_outbound_manifest(
        g.db,
        manifest_id=str(manifest_id),
        reason=validated.reason,
        voided_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.get("/outbound/orders")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_formal_outbound_orders():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        list_outbound_orders(
            g.db,
            warehouse_id=warehouse_id,
            limit=limit,
        )
    )


@shipyard_bp.get("/outbound/orders/<uuid:outbound_order_id>")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_formal_outbound_order(outbound_order_id):
    payload = get_outbound_order_payload(
        g.db,
        outbound_order_id=str(outbound_order_id),
    )
    ok, denied = check_warehouse_access(payload["order"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.get("/outbound/mobile/orders")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_mobile_outbound_orders():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        list_outbound_orders(
            g.db,
            warehouse_id=warehouse_id,
            limit=limit,
        )
    )


@shipyard_bp.get("/outbound/mobile/orders/<uuid:outbound_order_id>")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_mobile_outbound_order(outbound_order_id):
    payload = get_outbound_order_payload(
        g.db,
        outbound_order_id=str(outbound_order_id),
    )
    ok, denied = check_warehouse_access(payload["order"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.post("/outbound/tasks/<uuid:task_id>/scan")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(OutboundPalletScanRequest)
@with_db
def scan_outbound_pallet(task_id, validated):
    context = get_outbound_task_context(g.db, str(task_id))
    ok, denied = check_warehouse_access(context.warehouse_id)
    if not ok:
        return denied
    payload = validate_outbound_pallet_scan(
        g.db,
        task_id=str(task_id),
        scan=validated,
    )
    return jsonify(payload)


@shipyard_bp.post("/outbound/tasks/<uuid:task_id>/label-recognition")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def recognize_outbound_label(task_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传吊牌照片",
            }
        ), 415
    context = get_outbound_task_context(g.db, str(task_id))
    ok, denied = check_warehouse_access(context.warehouse_id)
    if not ok:
        return denied
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    try:
        content = read_upload_bytes(uploaded)
        content_type = detect_image_content_type(content)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    except UnsupportedImageError as exc:
        return jsonify({"error": "unsupported_file_type", "message": str(exc)}), 415
    payload = recognize_outbound_label_photo(
        g.db,
        task_id=str(task_id),
        file_name=safe_upload_name(uploaded.filename, fallback="pallet-label"),
        content_type=content_type,
        content=content,
        uploaded_by=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.post("/outbound/tasks/<uuid:task_id>/photos")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def upload_outbound_task_photo(task_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传照片",
            }
        ), 415
    context = get_outbound_task_context(g.db, str(task_id))
    ok, denied = check_warehouse_access(context.warehouse_id)
    if not ok:
        return denied
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    try:
        content = read_upload_bytes(uploaded)
        content_type = detect_image_content_type(content)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    except UnsupportedImageError as exc:
        return jsonify({"error": "unsupported_file_type", "message": str(exc)}), 415
    payload = save_outbound_task_photo(
        g.db,
        task_id=str(task_id),
        tag_suffix=request.form.get("tag_suffix", ""),
        file_name=safe_upload_name(uploaded.filename, fallback="pallet-photo"),
        content_type=content_type,
        content=content,
        uploaded_by=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.post("/outbound/tasks/<uuid:task_id>/confirm")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(ConfirmOutboundTaskRequest)
@with_db
def confirm_shipyard_outbound_task(task_id, validated):
    context = get_outbound_task_context(g.db, str(task_id))
    ok, denied = check_warehouse_access(context.warehouse_id)
    if not ok:
        return denied
    payload = confirm_outbound_task(
        g.db,
        task_id=str(task_id),
        validated=validated,
        outbound_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.post("/outbound/orders/<uuid:outbound_order_id>/loading")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(ConfirmOutboundLoadingRequest)
@with_db
def confirm_shipyard_loading(outbound_order_id, validated):
    current = get_outbound_order_payload(
        g.db,
        outbound_order_id=str(outbound_order_id),
    )
    ok, denied = check_warehouse_access(current["order"]["warehouse_id"])
    if not ok:
        return denied
    payload = confirm_outbound_loading(
        g.db,
        outbound_order_id=str(outbound_order_id),
        validated=validated,
        loaded_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.get("/outbound/photos/<uuid:photo_id>")
@require_auth
@require_shipyard_roles(*FORMAL_OUTBOUND_MOBILE_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def download_outbound_photo(photo_id):
    photo = get_outbound_photo(g.db, photo_id=str(photo_id))
    ok, denied = check_warehouse_access(photo["warehouse_id"])
    if not ok:
        return denied
    return send_file(
        BytesIO(photo["file_bytes"]),
        mimetype=photo["content_type"],
        as_attachment=True,
        download_name=photo["file_name"],
        max_age=0,
    )
