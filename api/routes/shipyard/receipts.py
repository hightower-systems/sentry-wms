"""Handheld pallet scan, evidence upload and actual receipt endpoints."""

from __future__ import annotations

from io import BytesIO

from flask import g, jsonify, request, send_file

from constants import SHIPYARD_PHOTO_PALLET
from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from schemas.shipyard import (
    PalletScanRequest,
    ReceiveTicketRequest,
    ShortageReportRequest,
)
from services.shipyard.receipt_service import (
    confirm_ticket_receipt,
    get_photo,
    get_ticket_context,
    recognize_ticket_label_photo,
    report_ticket_shortage,
    save_ticket_photo,
    validate_ticket_scan,
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

RECEIPT_WRITE_ROLES = ("SHIPYARD_MANAGER", "RECEIVER")


def _check_ticket_access(ticket_id):
    context = get_ticket_context(g.db, str(ticket_id))
    ok, denied = check_warehouse_access(context.warehouse_id)
    return context, ok, denied


@shipyard_bp.post("/inbound/tickets/<uuid:ticket_id>/scan")
@require_auth
@require_shipyard_roles(*RECEIPT_WRITE_ROLES)
@require_admin_or_page_permission("inbound")
@validate_body(PalletScanRequest)
@with_db
def scan_inbound_pallet(ticket_id, validated):
    _context, ok, denied = _check_ticket_access(ticket_id)
    if not ok:
        return denied
    payload = validate_ticket_scan(
        g.db,
        ticket_id=str(ticket_id),
        scan=validated,
    )
    return jsonify(payload)


@shipyard_bp.post("/inbound/tickets/<uuid:ticket_id>/label-recognition")
@require_auth
@require_shipyard_roles(*RECEIPT_WRITE_ROLES)
@require_admin_or_page_permission("inbound")
@with_db
def recognize_inbound_ticket_label(ticket_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传吊牌照片",
            }
        ), 415
    _context, ok, denied = _check_ticket_access(ticket_id)
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
    payload = recognize_ticket_label_photo(
        g.db,
        ticket_id=str(ticket_id),
        file_name=safe_upload_name(uploaded.filename, fallback="pallet-label"),
        content_type=content_type,
        content=content,
        uploaded_by=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.post("/inbound/tickets/<uuid:ticket_id>/photos")
@require_auth
@require_shipyard_roles(*RECEIPT_WRITE_ROLES)
@require_admin_or_page_permission("inbound")
@with_db
def upload_inbound_ticket_photo(ticket_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传照片",
            }
        ), 415
    _context, ok, denied = _check_ticket_access(ticket_id)
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
    if not content:
        return jsonify({"error": "empty_file"}), 400
    payload = save_ticket_photo(
        g.db,
        ticket_id=str(ticket_id),
        photo_kind=request.form.get("photo_kind", SHIPYARD_PHOTO_PALLET),
        tag_suffix=request.form.get("tag_suffix"),
        file_name=safe_upload_name(uploaded.filename, fallback="pallet-photo"),
        content_type=content_type,
        content=content,
        uploaded_by=g.current_user["username"],
    )
    return jsonify({"photo": payload["photo"]}), 201


@shipyard_bp.post("/inbound/tickets/<uuid:ticket_id>/receipts")
@require_auth
@require_shipyard_roles(*RECEIPT_WRITE_ROLES)
@require_admin_or_page_permission("inbound")
@validate_body(ReceiveTicketRequest)
@with_db
def receive_inbound_ticket(ticket_id, validated):
    _context, ok, denied = _check_ticket_access(ticket_id)
    if not ok:
        return denied
    payload = confirm_ticket_receipt(
        g.db,
        ticket_id=str(ticket_id),
        validated=validated,
        received_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.put("/inbound/tickets/<uuid:ticket_id>/shortage")
@require_auth
@require_shipyard_roles(*RECEIPT_WRITE_ROLES)
@require_admin_or_page_permission("inbound")
@validate_body(ShortageReportRequest)
@with_db
def update_ticket_shortage(ticket_id, validated):
    _context, ok, denied = _check_ticket_access(ticket_id)
    if not ok:
        return denied
    payload = report_ticket_shortage(
        g.db,
        ticket_id=str(ticket_id),
        status=validated.status,
        note=validated.note,
        reported_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.get("/photos/<uuid:photo_id>")
@require_auth
@require_admin_or_page_permission("inbound")
@with_db
def download_shipyard_photo(photo_id):
    photo = get_photo(g.db, photo_id=str(photo_id))
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
