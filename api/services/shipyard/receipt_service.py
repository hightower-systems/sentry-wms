"""Pallet scan validation, actual receipt and shortage tracking."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_SHORTAGE_UPDATED,
    ACTION_SHIPYARD_TICKET_RECEIVED,
    SHIPYARD_LABEL_RECOGNITION_FAILED,
    SHIPYARD_LABEL_RECOGNITION_MATCHED,
    SHIPYARD_LABEL_RECOGNITION_NEEDS_REVIEW,
    SHIPYARD_LEDGER_RECEIPT,
    SHIPYARD_LINE_PARTIAL,
    SHIPYARD_LINE_PENDING,
    SHIPYARD_LINE_RECEIVED,
    SHIPYARD_MANIFEST_CLOSED,
    SHIPYARD_MANIFEST_CONFIRMED,
    SHIPYARD_MANIFEST_PARTIAL,
    SHIPYARD_PALLET_STORED,
    SHIPYARD_PHOTO_AREA,
    SHIPYARD_PHOTO_DIFFERENCE,
    SHIPYARD_PHOTO_GROUP,
    SHIPYARD_PHOTO_PALLET,
    SHIPYARD_SHORTAGE_IN_TRANSIT,
    SHIPYARD_SHORTAGE_NONE,
    SHIPYARD_SHORTAGE_PARTIAL_REPLENISHMENT,
    SHIPYARD_SHORTAGE_PENDING_REPORT,
    SHIPYARD_SHORTAGE_REPLENISHED,
    SHIPYARD_SHORTAGE_REPORTED,
    SHIPYARD_TICKET_EXPECTED,
    SHIPYARD_TICKET_IN_STOCK,
    SHIPYARD_TICKET_PARTIAL,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    OcrProviderError,
    OcrUnavailableError,
    PalletDuplicateError,
    PalletValidationError,
    PhotoNotFoundError,
    PhotoValidationError,
    ShipyardValidationError,
    ShortageStateError,
    TicketNotFoundError,
    TicketStateError,
)
from services.shipyard.label_ocr import recognize_label_image
from services.shipyard.label_recognition_service import (
    append_required_field_mismatches,
    fields_scan_view,
    load_label_recognition,
    recognition_scan_view,
    save_label_recognition,
)
from services.shipyard.manifest_service import get_manifest_payload
from utils.uploads import MAX_UPLOAD_BYTES

ALLOWED_PHOTO_KINDS = {
    SHIPYARD_PHOTO_PALLET,
    SHIPYARD_PHOTO_GROUP,
    SHIPYARD_PHOTO_AREA,
    SHIPYARD_PHOTO_DIFFERENCE,
}
ALLOWED_PHOTO_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _decimal_text(value: Decimal | int | str) -> str:
    return format(Decimal(value), "f")


def _hash_qr(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_suffix(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise PalletValidationError(
            "托盘后缀必须使用 1-1、1-2 这样的格式",
            {"tag_suffix": value},
        )
    if any(int(part) <= 0 for part in parts):
        raise PalletValidationError(
            "托盘后缀序号必须大于 0",
            {"tag_suffix": value},
        )
    if len(text) > 32:
        raise PalletValidationError(
            "托盘后缀不能超过 32 个字符",
            {"tag_suffix": value},
        )
    return text


def _ticket_context(db, ticket_id: str, *, for_update: bool = False):
    # Only a fixed server-side SQL fragment is interpolated; ticket_id remains
    # a bound parameter and never reaches this string.
    lock_clause = "FOR UPDATE OF t" if for_update else ""
    query = f"""
        SELECT t.ticket_id, t.ticket_no, t.inbound_line_id,
               t.declared_quantity, t.received_quantity,
               t.on_hand_quantity, t.expected_pallet_count,
               t.assigned_bin_id, t.location_path_snapshot,
               t.stock_lot_number, t.status, t.shortage_status,
               t.shortage_note,
               l.line_id, l.source_row_no, l.ship_no,
               l.engineering_no, l.production_drawing_no,
               l.drawing_name, l.manufacturer_name, l.material_code, l.item_name,
               l.item_spec, l.unit, l.project_binding_id,
               m.manifest_id, m.manifest_no, m.warehouse_id,
               m.status AS manifest_status,
               b.bin_code, z.zone_code, z.zone_name,
               COALESCE(
                   NULLIF(l.engineering_no, ''),
                   pb.engineering_no,
                   ''
               ) AS expected_engineering_no,
               COALESCE(
                   NULLIF(l.production_drawing_no, ''),
                   pb.production_drawing_no,
                   ''
               ) AS expected_production_drawing_no,
               COALESCE(
                   NULLIF(l.manufacturer_name, ''),
                   pb.manufacturer_name,
                   ''
               ) AS expected_manufacturer_name,
               (
                   SELECT COUNT(*)
                     FROM shipyard_pallet_units p
                    WHERE p.ticket_id = t.ticket_id
               ) AS pallet_count
          FROM shipyard_cargo_tickets t
          JOIN shipyard_inbound_lines l
            ON l.line_id = t.inbound_line_id
          JOIN shipyard_inbound_manifests m
            ON m.manifest_id = l.manifest_id
          JOIN bins b ON b.bin_id = t.assigned_bin_id
          LEFT JOIN zones z ON z.zone_id = b.zone_id
          LEFT JOIN shipyard_project_bindings pb
            ON pb.binding_id = l.project_binding_id
         WHERE t.ticket_id = :ticket_id
         {lock_clause}
    """  # nosec B608
    return db.execute(
        text(query),
        {"ticket_id": ticket_id},
    ).fetchone()


def get_ticket_context(db, ticket_id: str):
    context = _ticket_context(db, ticket_id)
    if context is None:
        raise TicketNotFoundError("票货不存在")
    return context


def _ensure_receivable(context) -> None:
    if context.manifest_status not in (
        SHIPYARD_MANIFEST_CONFIRMED,
        SHIPYARD_MANIFEST_PARTIAL,
    ):
        raise TicketStateError(
            "当前清单状态不允许收货",
            {
                "manifest_id": str(context.manifest_id),
                "status": context.manifest_status,
            },
        )
    if context.status not in (
        SHIPYARD_TICKET_EXPECTED,
        SHIPYARD_TICKET_PARTIAL,
    ):
        raise TicketStateError(
            "当前票货状态不允许继续收货",
            {"ticket_id": str(context.ticket_id), "status": context.status},
        )


def _normalized(value: str | None) -> str:
    return (value or "").strip().casefold()


def _scan_field_mismatches(
    context,
    scan,
    *,
    require_expected_present: bool = False,
) -> list[dict[str, Any]]:
    expected_fields = (
        ("ship_no", context.ship_no, scan.ship_no),
        (
            "engineering_no",
            context.expected_engineering_no,
            scan.engineering_no,
        ),
        (
            "production_drawing_no",
            context.expected_production_drawing_no,
            scan.production_drawing_no or scan.qr_pallet_no,
        ),
        (
            "manufacturer_name",
            context.expected_manufacturer_name,
            scan.manufacturer_name,
        ),
        (
            "material_code",
            context.material_code,
            getattr(scan, "material_code", None),
        ),
        (
            "item_name",
            context.item_name,
            getattr(scan, "item_name", None),
        ),
    )
    mismatches = []
    for field, expected, actual in expected_fields:
        if field in {"material_code", "item_name"} and not hasattr(scan, field):
            continue
        if not actual:
            if not expected or not require_expected_present:
                continue
            mismatches.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": None,
                    "reason": "REQUIRED",
                }
            )
        elif expected and _normalized(expected) != _normalized(actual):
            mismatches.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                    "reason": "MISMATCH",
                }
            )
    if scan.label_quantity is not None and (
        scan.label_quantity != context.declared_quantity
    ):
        mismatches.append(
            {
                "field": "label_quantity",
                "expected": _decimal_text(context.declared_quantity),
                "actual": _decimal_text(scan.label_quantity),
                "reason": "MISMATCH",
            }
        )
    return mismatches


def _validate_scan_fields(
    context,
    scan,
    *,
    require_expected_present: bool = False,
) -> str | None:
    suffix = _normalize_suffix(scan.tag_suffix)
    mismatches = _scan_field_mismatches(
        context,
        scan,
        require_expected_present=require_expected_present,
    )
    if mismatches:
        raise PalletValidationError(
            "吊牌字段与票货或清单不一致",
            {
                "ticket_id": str(context.ticket_id),
                "tag_suffix": suffix,
                "mismatches": mismatches,
            },
        )
    return suffix


def _existing_pallet_for_qr(db, qr_hash: str):
    return db.execute(
        text(
            """
            SELECT pallet_id, ticket_id, tag_suffix, status
              FROM shipyard_pallet_units
             WHERE qr_payload_sha256 = :qr_hash
             LIMIT 1
            """
        ),
        {"qr_hash": qr_hash},
    ).fetchone()


def _ensure_new_pallet(
    db,
    context,
    scan,
    suffix: str | None,
    qr_hash: str | None,
) -> None:
    if qr_hash:
        existing_qr = _existing_pallet_for_qr(db, qr_hash)
        if existing_qr is not None:
            raise PalletDuplicateError(
                "该吊牌二维码已经登记，不能重复入库",
                {
                    "pallet_id": str(existing_qr.pallet_id),
                    "ticket_id": str(existing_qr.ticket_id),
                    "tag_suffix": existing_qr.tag_suffix,
                    "status": existing_qr.status,
                },
            )
    existing_suffix = db.execute(
        text(
            """
            SELECT pallet_id, qr_payload_sha256
              FROM shipyard_pallet_units
             WHERE ticket_id = :ticket_id
               AND COALESCE(tag_suffix, '') = COALESCE(:tag_suffix, '')
             LIMIT 1
            """
        ),
        {"ticket_id": str(context.ticket_id), "tag_suffix": suffix},
    ).fetchone()
    if existing_suffix is not None:
        raise PalletDuplicateError(
            "该托盘后缀已经在当前票货登记",
            {
                "ticket_id": str(context.ticket_id),
                "tag_suffix": suffix,
                "pallet_id": str(existing_suffix.pallet_id),
            },
        )
    existing_pallets = (
        db.execute(
            text(
                """
                SELECT tag_suffix
                  FROM shipyard_pallet_units
                 WHERE ticket_id = :ticket_id
                 ORDER BY created_at, pallet_id
                """
            ),
            {"ticket_id": str(context.ticket_id)},
        )
        .scalars()
        .all()
    )
    if existing_pallets:
        if suffix is None:
            raise PalletValidationError(
                "该票货已有多托盘后缀，不能继续登记无后缀托盘",
                {"ticket_id": str(context.ticket_id)},
            )
        if any(existing is None for existing in existing_pallets):
            raise PalletValidationError(
                "该票货已有无后缀托盘，不能混入多托盘后缀",
                {"ticket_id": str(context.ticket_id)},
            )
        existing_prefixes = {existing.split("-", 1)[0] for existing in existing_pallets}
        if suffix.split("-", 1)[0] not in existing_prefixes:
            raise PalletValidationError(
                "新增托盘后缀与已有托组号不一致",
                {
                    "ticket_id": str(context.ticket_id),
                    "tag_suffix": suffix,
                    "existing_tag_suffixes": existing_pallets,
                },
            )


def _validate_pallet_group(scans) -> list[str | None]:
    suffixes = [_normalize_suffix(scan.tag_suffix) for scan in scans]
    if len(suffixes) > 1 and any(suffix is None for suffix in suffixes):
        raise PalletValidationError(
            "同一次提交包含多个托盘时，每个托盘都必须有 1-1 格式的后缀"
        )
    present = [suffix for suffix in suffixes if suffix is not None]
    if present:
        prefixes = {suffix.split("-", 1)[0] for suffix in present}
        if len(prefixes) != 1:
            raise PalletValidationError(
                "同组托盘后缀必须使用同一个托组号",
                {"tag_suffixes": present},
            )
        if len(set(present)) != len(present):
            raise PalletValidationError(
                "同一次提交中存在重复的托盘后缀",
                {"tag_suffixes": present},
            )
    return suffixes


def _photo_payload(row) -> dict[str, Any]:
    return {
        "photo_id": str(row.photo_id),
        "photo_kind": row.photo_kind,
        "tag_suffix": row.tag_suffix,
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_sha256": row.file_sha256,
        "uploaded_by": row.uploaded_by,
        "created_at": _iso(row.created_at),
    }


def _pallet_photo(db, photo_id, *, context, suffix: str | None, actor: str):
    row = db.execute(
        text(
            """
            SELECT photo_id, ticket_id, warehouse_id, receipt_id,
                   pallet_id, tag_suffix, photo_kind, file_name,
                   content_type, file_size, file_sha256, uploaded_by,
                   created_at
              FROM shipyard_photos
             WHERE photo_id = :photo_id
             FOR UPDATE
            """
        ),
        {"photo_id": str(photo_id)},
    ).fetchone()
    if row is None:
        raise PhotoValidationError(
            "吊牌照片不存在",
            {"photo_id": str(photo_id)},
        )
    if row.ticket_id != context.ticket_id:
        raise PhotoValidationError(
            "照片不属于当前票货",
            {"photo_id": str(photo_id)},
        )
    if row.receipt_id is not None:
        raise PhotoValidationError(
            "照片已经用于其他收货记录",
            {"photo_id": str(photo_id)},
        )
    if row.photo_kind != SHIPYARD_PHOTO_PALLET:
        raise PhotoValidationError(
            "托盘照片类型必须为 PALLET",
            {"photo_id": str(photo_id), "photo_kind": row.photo_kind},
        )
    if row.tag_suffix is not None and row.tag_suffix != suffix:
        raise PhotoValidationError(
            "照片的托盘后缀与识别结果不一致",
            {
                "photo_id": str(photo_id),
                "photo_tag_suffix": row.tag_suffix,
                "scan_tag_suffix": suffix,
            },
        )
    if row.uploaded_by != actor:
        raise PhotoValidationError(
            "只能使用当前收货人上传的照片",
            {"photo_id": str(photo_id)},
        )
    return row


def _optional_photo(
    db,
    photo_id,
    *,
    context,
    photo_kind: str,
    actor: str,
):
    if photo_id is None:
        return None
    row = db.execute(
        text(
            """
            SELECT photo_id, ticket_id, warehouse_id, receipt_id,
                   pallet_id, tag_suffix, photo_kind, file_name,
                   content_type, file_size, file_sha256, uploaded_by,
                   created_at
              FROM shipyard_photos
             WHERE photo_id = :photo_id
             FOR UPDATE
            """
        ),
        {"photo_id": str(photo_id)},
    ).fetchone()
    if row is None or row.ticket_id != context.ticket_id:
        raise PhotoValidationError(
            "照片不存在或不属于当前票货",
            {"photo_id": str(photo_id)},
        )
    if row.receipt_id is not None:
        raise PhotoValidationError(
            "照片已经用于其他收货记录",
            {"photo_id": str(photo_id)},
        )
    if row.photo_kind != photo_kind:
        raise PhotoValidationError(
            "照片类型不匹配",
            {
                "photo_id": str(photo_id),
                "expected_kind": photo_kind,
                "actual_kind": row.photo_kind,
            },
        )
    if row.uploaded_by != actor:
        raise PhotoValidationError(
            "只能使用当前收货人上传的照片",
            {"photo_id": str(photo_id)},
        )
    return row


def _ticket_payload(context) -> dict[str, Any]:
    short_quantity = max(
        context.declared_quantity - context.received_quantity,
        Decimal(0),
    )
    return {
        "ticket_id": str(context.ticket_id),
        "ticket_no": context.ticket_no,
        "manifest_id": str(context.manifest_id),
        "manifest_no": context.manifest_no,
        "source_row_no": context.source_row_no,
        "ship_no": context.ship_no,
        "engineering_no": context.expected_engineering_no,
        "production_drawing_no": context.expected_production_drawing_no,
        "drawing_name": context.drawing_name,
        "manufacturer_name": context.expected_manufacturer_name,
        "material_code": context.material_code,
        "item_name": context.item_name,
        "item_spec": context.item_spec,
        "unit": context.unit,
        "declared_quantity": _decimal_text(context.declared_quantity),
        "received_quantity": _decimal_text(context.received_quantity),
        "on_hand_quantity": _decimal_text(context.on_hand_quantity),
        "expected_pallet_count": context.expected_pallet_count,
        "pallet_count": context.pallet_count,
        "short_quantity": _decimal_text(short_quantity),
        "assigned_bin_id": context.assigned_bin_id,
        "bin_code": context.bin_code,
        "zone_code": context.zone_code,
        "zone_name": context.zone_name,
        "location_path_snapshot": context.location_path_snapshot,
        "stock_lot_number": context.stock_lot_number,
        "status": context.status,
        "shortage_status": context.shortage_status,
    }


def validate_ticket_scan(db, *, ticket_id: str, scan) -> dict[str, Any]:
    if not scan.qr_payload:
        raise PalletValidationError("请使用吊牌照片识别接口")
    context = _ticket_context(db, ticket_id)
    if context is None:
        raise TicketNotFoundError("票货不存在")
    _ensure_receivable(context)
    suffix = _validate_scan_fields(
        context,
        scan,
        require_expected_present=True,
    )
    if (
        context.expected_pallet_count is not None
        and context.pallet_count >= context.expected_pallet_count
    ):
        raise PalletValidationError(
            "票货已登记全部预计托盘",
            {"ticket_id": ticket_id},
        )
    qr_hash = _hash_qr(scan.qr_payload.strip())
    _ensure_new_pallet(db, context, scan, suffix, qr_hash)
    existing_suffixes = (
        db.execute(
            text(
                """
            SELECT tag_suffix
              FROM shipyard_pallet_units
             WHERE ticket_id = :ticket_id
             ORDER BY tag_suffix NULLS FIRST
            """
            ),
            {"ticket_id": ticket_id},
        )
        .scalars()
        .all()
    )
    return {
        "matched": True,
        "ticket": _ticket_payload(context),
        "pallet": {
            "tag_suffix": suffix,
            "qr_payload_sha256": qr_hash,
            "ship_no": scan.ship_no,
            "engineering_no": scan.engineering_no,
            "production_drawing_no": (scan.production_drawing_no or scan.qr_pallet_no),
            "qr_pallet_no": scan.qr_pallet_no,
            "batch_no": scan.batch_no,
            "manufacturer_name": scan.manufacturer_name,
            "installation_pallet_no": scan.installation_pallet_no,
            "installation_pallet_name": scan.installation_pallet_name,
            "destination": scan.destination,
            "label_quantity": (
                _decimal_text(scan.label_quantity)
                if scan.label_quantity is not None
                else None
            ),
        },
        "existing_pallet_suffixes": existing_suffixes,
    }


def save_ticket_photo(
    db,
    *,
    ticket_id: str,
    photo_kind: str,
    tag_suffix: str | None,
    file_name: str,
    content_type: str,
    content: bytes,
    uploaded_by: str,
) -> dict[str, Any]:
    context = _ticket_context(db, ticket_id)
    if context is None:
        raise TicketNotFoundError("票货不存在")
    _ensure_receivable(context)
    if photo_kind not in ALLOWED_PHOTO_KINDS:
        raise PhotoValidationError(
            "照片类型无效",
            {"photo_kind": photo_kind},
        )
    if not content:
        raise PhotoValidationError("照片内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise PhotoValidationError("照片超过 10 MiB 大小限制")
    if content_type not in ALLOWED_PHOTO_CONTENT_TYPES:
        raise PhotoValidationError(
            "照片格式必须是 JPEG、PNG 或 WebP",
            {"content_type": content_type},
        )
    suffix = _normalize_suffix(tag_suffix)
    row = db.execute(
        text(
            """
            INSERT INTO shipyard_photos
                (ticket_id, warehouse_id, tag_suffix, photo_kind,
                 file_name, content_type, file_size, file_sha256,
                 file_bytes, uploaded_by)
            VALUES
                (:ticket_id, :warehouse_id, :tag_suffix, :photo_kind,
                 :file_name, :content_type, :file_size, :file_sha256,
                 :file_bytes, :uploaded_by)
            RETURNING photo_id, ticket_id, warehouse_id, receipt_id,
                      pallet_id, tag_suffix, photo_kind, file_name,
                      content_type, file_size, file_sha256, uploaded_by,
                      created_at
            """
        ),
        {
            "ticket_id": ticket_id,
            "warehouse_id": context.warehouse_id,
            "tag_suffix": suffix,
            "photo_kind": photo_kind,
            "file_name": file_name,
            "content_type": content_type,
            "file_size": len(content),
            "file_sha256": hashlib.sha256(content).hexdigest(),
            "file_bytes": content,
            "uploaded_by": uploaded_by,
        },
    ).fetchone()
    db.commit()
    return {
        "warehouse_id": row.warehouse_id,
        "photo": _photo_payload(row),
    }


def recognize_ticket_label_photo(
    db,
    *,
    ticket_id: str,
    file_name: str,
    content_type: str,
    content: bytes,
    uploaded_by: str,
) -> dict[str, Any]:
    context = _ticket_context(db, ticket_id)
    if context is None:
        raise TicketNotFoundError("票货不存在")
    _ensure_receivable(context)
    photo_result = save_ticket_photo(
        db,
        ticket_id=ticket_id,
        photo_kind=SHIPYARD_PHOTO_PALLET,
        tag_suffix=None,
        file_name=file_name,
        content_type=content_type,
        content=content,
        uploaded_by=uploaded_by,
    )
    photo = photo_result["photo"]
    try:
        ocr = recognize_label_image(content)
    except (OcrUnavailableError, OcrProviderError) as exc:
        recognition = save_label_recognition(
            db,
            photo_id=photo["photo_id"],
            ticket_id=ticket_id,
            task_id=None,
            warehouse_id=photo_result["warehouse_id"],
            result=None,
            status=SHIPYARD_LABEL_RECOGNITION_FAILED,
            actor=uploaded_by,
            error_code=exc.code,
            error_message=exc.message,
        )
        exc.details.update({"photo": photo, "recognition": recognition})
        raise

    fields = dict(ocr.fields)
    suffix = _normalize_suffix(str(fields.get("tag_suffix") or ""))
    scan = fields_scan_view(fields, suffix=suffix or "")
    mismatches = _scan_field_mismatches(context, scan)
    append_required_field_mismatches(mismatches, scan)
    if suffix is None:
        if not any(item["field"] == "tag_suffix" for item in mismatches):
            mismatches.append(
                {
                    "field": "tag_suffix",
                    "expected": "1-1 格式托盘后缀",
                    "actual": None,
                    "reason": "REQUIRED",
                }
            )
    elif (
        context.expected_pallet_count is not None
        and context.pallet_count >= context.expected_pallet_count
    ):
        mismatches.append(
            {
                "field": "pallet_count",
                "expected": context.expected_pallet_count,
                "actual": context.pallet_count,
                "reason": "TOO_MANY",
            }
        )
    else:
        try:
            _ensure_new_pallet(db, context, scan, suffix, None)
        except PalletDuplicateError as exc:
            mismatches.append(
                {
                    "field": "tag_suffix",
                    "expected": suffix,
                    "actual": suffix,
                    "reason": "DUPLICATE",
                    "message": exc.message,
                }
            )

    if suffix:
        db.execute(
            text(
                """
                UPDATE shipyard_photos
                   SET tag_suffix = :tag_suffix
                 WHERE photo_id = :photo_id
                """
            ),
            {"tag_suffix": suffix, "photo_id": photo["photo_id"]},
        )
        photo["tag_suffix"] = suffix

    status = (
        SHIPYARD_LABEL_RECOGNITION_MATCHED
        if suffix and not mismatches
        else SHIPYARD_LABEL_RECOGNITION_NEEDS_REVIEW
    )
    recognition = save_label_recognition(
        db,
        photo_id=photo["photo_id"],
        ticket_id=ticket_id,
        task_id=None,
        warehouse_id=photo_result["warehouse_id"],
        result=ocr,
        status=status,
        actor=uploaded_by,
    )
    return {
        "matched": status == SHIPYARD_LABEL_RECOGNITION_MATCHED,
        "needs_suffix": suffix is None,
        "recognized": fields,
        "mismatches": mismatches,
        "photo": photo,
        "recognition": recognition,
        "ticket": _ticket_payload(context),
    }


def get_photo(db, *, photo_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT photo_id, ticket_id, warehouse_id, receipt_id,
                   pallet_id, tag_suffix, photo_kind, file_name,
                   content_type, file_size, file_sha256, file_bytes,
                   uploaded_by, created_at
              FROM shipyard_photos
             WHERE photo_id = :photo_id
            """
        ),
        {"photo_id": photo_id},
    ).fetchone()
    if row is None:
        raise PhotoNotFoundError("照片不存在", {"photo_id": photo_id})
    payload = _photo_payload(row)
    payload["warehouse_id"] = row.warehouse_id
    payload["ticket_id"] = str(row.ticket_id)
    payload["receipt_id"] = str(row.receipt_id) if row.receipt_id else None
    payload["pallet_id"] = str(row.pallet_id) if row.pallet_id else None
    payload["file_bytes"] = bytes(row.file_bytes)
    return payload


def _shortage_status_after_receipt(
    *,
    current_status: str,
    previous_received: Decimal,
    new_received: Decimal,
    declared_quantity: Decimal,
) -> str:
    if new_received >= declared_quantity:
        if previous_received == 0:
            return SHIPYARD_SHORTAGE_NONE
        return SHIPYARD_SHORTAGE_REPLENISHED
    if current_status in (
        SHIPYARD_SHORTAGE_REPORTED,
        SHIPYARD_SHORTAGE_IN_TRANSIT,
    ):
        return SHIPYARD_SHORTAGE_PARTIAL_REPLENISHMENT
    if current_status == SHIPYARD_SHORTAGE_PARTIAL_REPLENISHMENT:
        return current_status
    return SHIPYARD_SHORTAGE_PENDING_REPORT


def _refresh_manifest_status(db, manifest_id: str) -> str:
    rows = db.execute(
        text(
            """
            SELECT t.declared_quantity, t.received_quantity
              FROM shipyard_cargo_tickets t
              JOIN shipyard_inbound_lines l
                ON l.line_id = t.inbound_line_id
             WHERE l.manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchall()
    if rows and all(row.received_quantity >= row.declared_quantity for row in rows):
        status = SHIPYARD_MANIFEST_CLOSED
    elif any(row.received_quantity > 0 for row in rows):
        status = SHIPYARD_MANIFEST_PARTIAL
    else:
        status = SHIPYARD_MANIFEST_CONFIRMED
    db.execute(
        text(
            """
            UPDATE shipyard_inbound_manifests
               SET status = :status, updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {"status": status, "manifest_id": manifest_id},
    )
    return status


def _validate_receipt_evidence(db, context, validated, received_by: str):
    suffixes, photos, optional_photos = _validate_receipt_evidence(
        db,
        context,
        validated,
        received_by,
    )
    return suffixes, photos, optional_photos


def _insert_pallet_units(db, ticket_id: str, scans, received_by: str):
    pallet_ids = []
    for scan in scans:
        pallet = db.execute(
            text(
                """
                INSERT INTO shipyard_pallet_units
                    (ticket_id, tag_suffix, qr_payload,
                     qr_payload_sha256, factory_pallet_no,
                     installation_pallet_no, batch_no, label_fields,
                     status,
                     received_by, received_at)
                VALUES
                    (:ticket_id, :tag_suffix, :qr_payload,
                     :qr_payload_sha256, :factory_pallet_no,
                     :installation_pallet_no, :batch_no, :label_fields,
                     :status,
                     :received_by, NOW())
                RETURNING pallet_id
                """
            ),
            {
                "ticket_id": ticket_id,
                "tag_suffix": scan.tag_suffix,
                "qr_payload": (scan.qr_payload.strip() if scan.qr_payload else None),
                "qr_payload_sha256": (
                    _hash_qr(scan.qr_payload.strip()) if scan.qr_payload else None
                ),
                "factory_pallet_no": scan.qr_pallet_no,
                "installation_pallet_no": scan.installation_pallet_no,
                "batch_no": scan.batch_no,
                "label_fields": Json(getattr(scan, "label_fields", {}) or {}),
                "status": SHIPYARD_PALLET_STORED,
                "received_by": received_by,
            },
        ).fetchone()
        pallet_ids.append(pallet.pallet_id)
    return pallet_ids


def _attach_receipt_evidence(
    db,
    *,
    receipt_id,
    scans,
    pallet_ids,
    photos,
    optional_photos,
) -> None:
    for scan, pallet_id, photo in zip(
        scans,
        pallet_ids,
        photos,
        strict=True,
    ):
        db.execute(
            text(
                """
                INSERT INTO shipyard_receipt_pallets
                    (receipt_id, pallet_id, tag_suffix)
                VALUES
                    (:receipt_id, :pallet_id, :tag_suffix)
                """
            ),
            {
                "receipt_id": receipt_id,
                "pallet_id": pallet_id,
                "tag_suffix": scan.tag_suffix,
            },
        )
        db.execute(
            text(
                """
                UPDATE shipyard_photos
                   SET receipt_id = :receipt_id,
                       pallet_id = :pallet_id
                 WHERE photo_id = :photo_id
                """
            ),
            {
                "receipt_id": receipt_id,
                "pallet_id": pallet_id,
                "photo_id": photo.photo_id,
            },
        )

    for photo in optional_photos:
        if photo is not None:
            db.execute(
                text(
                    """
                    UPDATE shipyard_photos
                       SET receipt_id = :receipt_id
                     WHERE photo_id = :photo_id
                    """
                ),
                {
                    "receipt_id": receipt_id,
                    "photo_id": photo.photo_id,
                },
            )


def _receipt_summary(db, receipt_id: str) -> dict[str, Any]:
    receipt = db.execute(
        text(
            """
            SELECT r.receipt_id, r.ticket_id, r.idempotency_key,
                   r.previous_received_quantity, r.received_quantity,
                   r.balance_after, r.resulting_ticket_status,
                   r.bin_id, r.location_path_snapshot, r.difference_reason,
                   r.client_device_id, r.received_by, r.received_at,
                   t.ticket_no, t.status AS ticket_status,
                   t.declared_quantity, t.received_quantity
                       AS cumulative_received_quantity,
                   t.on_hand_quantity, t.expected_pallet_count,
                   t.shortage_status,
                   (
                       SELECT COUNT(*)
                         FROM shipyard_pallet_units p
                        WHERE p.ticket_id = t.ticket_id
                   ) AS pallet_count,
                   l.manifest_id, l.source_row_no, l.material_code, l.item_name,
                   l.ship_no, l.production_drawing_no
              FROM shipyard_receipts r
              JOIN shipyard_cargo_tickets t ON t.ticket_id = r.ticket_id
              JOIN shipyard_inbound_lines l ON l.line_id = t.inbound_line_id
             WHERE r.receipt_id = :receipt_id
            """
        ),
        {"receipt_id": receipt_id},
    ).fetchone()
    pallets = db.execute(
        text(
            """
            SELECT p.pallet_id, p.tag_suffix, p.qr_payload,
                   p.qr_payload_sha256, p.label_fields, p.factory_pallet_no,
                   p.installation_pallet_no, p.batch_no, p.status,
                   p.received_by, p.received_at,
                   ph.photo_id, ph.file_name, ph.content_type,
                   ph.file_size, ph.file_sha256,
                   lr.recognition_id AS label_recognition_id,
                   lr.status AS label_recognition_status,
                   lr.fields AS label_recognition_fields
              FROM shipyard_receipt_pallets rp
              JOIN shipyard_pallet_units p ON p.pallet_id = rp.pallet_id
              LEFT JOIN shipyard_photos ph
                ON ph.receipt_id = rp.receipt_id
               AND ph.pallet_id = rp.pallet_id
               AND ph.photo_kind = :pallet_kind
              LEFT JOIN shipyard_label_recognitions lr
                ON lr.photo_id = ph.photo_id
             WHERE rp.receipt_id = :receipt_id
             ORDER BY p.tag_suffix NULLS FIRST, p.pallet_id
            """
        ),
        {
            "receipt_id": receipt_id,
            "pallet_kind": SHIPYARD_PHOTO_PALLET,
        },
    ).fetchall()
    evidence = db.execute(
        text(
            """
            SELECT photo_id, photo_kind, tag_suffix, file_name,
                   content_type, file_size, file_sha256, created_at
              FROM shipyard_photos
             WHERE receipt_id = :receipt_id
               AND photo_kind <> :pallet_kind
             ORDER BY created_at
            """
        ),
        {
            "receipt_id": receipt_id,
            "pallet_kind": SHIPYARD_PHOTO_PALLET,
        },
    ).fetchall()
    manifest_payload = get_manifest_payload(db, str(receipt.manifest_id))
    return {
        "receipt": {
            "receipt_id": str(receipt.receipt_id),
            "idempotency_key": receipt.idempotency_key,
            "previous_received_quantity": _decimal_text(
                receipt.previous_received_quantity
            ),
            "received_quantity": _decimal_text(receipt.received_quantity),
            "cumulative_received_quantity": _decimal_text(
                receipt.cumulative_received_quantity
            ),
            "balance_after": _decimal_text(receipt.balance_after),
            "resulting_ticket_status": receipt.resulting_ticket_status,
            "bin_id": receipt.bin_id,
            "location_path_snapshot": receipt.location_path_snapshot,
            "difference_reason": receipt.difference_reason,
            "client_device_id": receipt.client_device_id,
            "received_by": receipt.received_by,
            "received_at": _iso(receipt.received_at),
        },
        "ticket": {
            "ticket_id": str(receipt.ticket_id),
            "ticket_no": receipt.ticket_no,
            "source_row_no": receipt.source_row_no,
            "ship_no": receipt.ship_no,
            "production_drawing_no": receipt.production_drawing_no,
            "material_code": receipt.material_code,
            "item_name": receipt.item_name,
            "declared_quantity": _decimal_text(receipt.declared_quantity),
            "received_quantity": _decimal_text(receipt.cumulative_received_quantity),
            "on_hand_quantity": _decimal_text(receipt.on_hand_quantity),
            "expected_pallet_count": receipt.expected_pallet_count,
            "pallet_count": receipt.pallet_count,
            "short_quantity": _decimal_text(
                max(
                    receipt.declared_quantity - receipt.cumulative_received_quantity,
                    Decimal(0),
                )
            ),
            "status": receipt.ticket_status,
            "shortage_status": receipt.shortage_status,
        },
        "pallets": [
            {
                "pallet_id": str(pallet.pallet_id),
                "tag_suffix": pallet.tag_suffix,
                "qr_payload": pallet.qr_payload,
                "qr_payload_sha256": pallet.qr_payload_sha256,
                "label_fields": dict(pallet.label_fields or {}),
                "factory_pallet_no": pallet.factory_pallet_no,
                "installation_pallet_no": pallet.installation_pallet_no,
                "batch_no": pallet.batch_no,
                "status": pallet.status,
                "received_by": pallet.received_by,
                "received_at": _iso(pallet.received_at),
                "photo": {
                    "photo_id": str(pallet.photo_id),
                    "file_name": pallet.file_name,
                    "content_type": pallet.content_type,
                    "file_size": pallet.file_size,
                    "file_sha256": pallet.file_sha256,
                }
                if pallet.photo_id
                else None,
                "label_recognition": {
                    "recognition_id": str(pallet.label_recognition_id),
                    "status": pallet.label_recognition_status,
                    "fields": dict(pallet.label_recognition_fields or {}),
                }
                if pallet.label_recognition_id
                else None,
            }
            for pallet in pallets
        ],
        "evidence": [
            {
                "photo_id": str(photo.photo_id),
                "photo_kind": photo.photo_kind,
                "tag_suffix": photo.tag_suffix,
                "file_name": photo.file_name,
                "content_type": photo.content_type,
                "file_size": photo.file_size,
                "file_sha256": photo.file_sha256,
                "created_at": _iso(photo.created_at),
            }
            for photo in evidence
        ],
        "manifest": {
            "manifest_id": str(receipt.manifest_id),
            "status": manifest_payload["manifest"]["status"],
            "summary": manifest_payload["validation"]["summary"],
        },
    }


def confirm_ticket_receipt(
    db,
    *,
    ticket_id: str,
    validated,
    received_by: str,
) -> dict[str, Any]:
    lock_key = f"{ticket_id}:{validated.idempotency_key}"
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    existing = db.execute(
        text(
            """
            SELECT receipt_id
              FROM shipyard_receipts
             WHERE ticket_id = :ticket_id
               AND idempotency_key = :idempotency_key
            """
        ),
        {
            "ticket_id": ticket_id,
            "idempotency_key": validated.idempotency_key,
        },
    ).fetchone()
    if existing is not None:
        return _receipt_summary(db, str(existing.receipt_id))

    context = _ticket_context(db, ticket_id, for_update=True)
    if context is None:
        raise TicketNotFoundError("票货不存在")
    _ensure_receivable(context)
    remaining = context.declared_quantity - context.received_quantity
    if validated.received_quantity > remaining:
        raise PalletValidationError(
            "本次实收数量超过票货剩余应收数量",
            {
                "ticket_id": ticket_id,
                "remaining_quantity": _decimal_text(remaining),
                "received_quantity": _decimal_text(validated.received_quantity),
            },
        )

    suffixes = _validate_pallet_group(validated.pallets)
    photos = []
    photo_ids = set()
    qr_hashes = set()
    effective_scans = []
    for scan, suffix in zip(validated.pallets, suffixes, strict=True):
        qr_hash = None
        if scan.label_recognition_id:
            recognition = load_label_recognition(
                db,
                str(scan.label_recognition_id),
                ticket_id=ticket_id,
                photo_id=str(scan.photo_id),
                for_update=True,
            )
            if recognition.status == SHIPYARD_LABEL_RECOGNITION_FAILED:
                raise PalletValidationError(
                    "吊牌 OCR 识别失败，请重新拍照",
                    {"recognition_id": str(recognition.recognition_id)},
                )
            recognized_suffix = _normalize_suffix(
                str(dict(recognition.fields or {}).get("tag_suffix") or "")
            )
            if recognized_suffix and recognized_suffix != suffix:
                raise PalletValidationError(
                    "OCR 托盘后缀与本次提交不一致",
                    {
                        "expected": recognized_suffix,
                        "actual": suffix,
                    },
                )
            effective_scan = recognition_scan_view(recognition, suffix=suffix)
        else:
            if not scan.qr_payload:
                raise PalletValidationError("吊牌照片缺少 OCR 识别结果，请重新拍照")
            effective_scan = scan
            effective_scan.tag_suffix = suffix
            qr_hash = _hash_qr(scan.qr_payload.strip())
            if qr_hash in qr_hashes:
                raise PalletDuplicateError(
                    "同一次提交中存在重复的吊牌二维码",
                    {"tag_suffix": suffix},
                )
            qr_hashes.add(qr_hash)
        _validate_scan_fields(
            context,
            effective_scan,
            require_expected_present=not scan.label_recognition_id,
        )
        if scan.label_recognition_id:
            required_mismatches: list[dict[str, Any]] = []
            append_required_field_mismatches(
                required_mismatches,
                effective_scan,
            )
            if required_mismatches:
                raise PalletValidationError(
                    "吊牌必填字段未识别",
                    {"mismatches": required_mismatches},
                )
        _ensure_new_pallet(db, context, effective_scan, suffix, qr_hash)
        photo = _pallet_photo(
            db,
            scan.photo_id,
            context=context,
            suffix=suffix,
            actor=received_by,
        )
        if photo.photo_id in photo_ids:
            raise PhotoValidationError(
                "同一张照片不能用于多个托盘",
                {"photo_id": str(photo.photo_id)},
            )
        photo_ids.add(photo.photo_id)
        if photo.tag_suffix is None:
            db.execute(
                text(
                    """
                    UPDATE shipyard_photos
                       SET tag_suffix = :tag_suffix
                     WHERE photo_id = :photo_id
                    """
                ),
                {"tag_suffix": suffix, "photo_id": str(photo.photo_id)},
            )
        photos.append(photo)
        effective_scans.append(effective_scan)

    optional_photos = [
        _optional_photo(
            db,
            validated.group_photo_id,
            context=context,
            photo_kind=SHIPYARD_PHOTO_GROUP,
            actor=received_by,
        ),
        _optional_photo(
            db,
            validated.area_photo_id,
            context=context,
            photo_kind=SHIPYARD_PHOTO_AREA,
            actor=received_by,
        ),
        _optional_photo(
            db,
            validated.difference_photo_id,
            context=context,
            photo_kind=SHIPYARD_PHOTO_DIFFERENCE,
            actor=received_by,
        ),
    ]
    for photo in optional_photos:
        if photo is not None and photo.photo_id in photo_ids:
            raise PhotoValidationError(
                "汇总照片与托盘照片不能重复",
                {"photo_id": str(photo.photo_id)},
            )

    new_received = context.received_quantity + validated.received_quantity
    new_on_hand = context.on_hand_quantity + validated.received_quantity
    total_pallet_count = context.pallet_count + len(validated.pallets)
    if (
        context.expected_pallet_count is not None
        and total_pallet_count > context.expected_pallet_count
    ):
        raise PalletValidationError(
            "登记的托盘数量超过清单预计托盘数",
            {
                "ticket_id": ticket_id,
                "expected_pallet_count": context.expected_pallet_count,
                "resulting_pallet_count": total_pallet_count,
            },
        )
    if (
        context.expected_pallet_count is not None
        and new_received >= context.declared_quantity
        and total_pallet_count < context.expected_pallet_count
    ):
        raise PalletValidationError(
            "整票数量已到齐，但托盘后缀尚未齐套",
            {
                "ticket_id": ticket_id,
                "expected_pallet_count": context.expected_pallet_count,
                "resulting_pallet_count": total_pallet_count,
            },
        )
    ticket_status = (
        SHIPYARD_TICKET_IN_STOCK
        if new_received >= context.declared_quantity
        else SHIPYARD_TICKET_PARTIAL
    )
    shortage_status = _shortage_status_after_receipt(
        current_status=context.shortage_status,
        previous_received=context.received_quantity,
        new_received=new_received,
        declared_quantity=context.declared_quantity,
    )

    pallet_ids = _insert_pallet_units(
        db,
        ticket_id,
        effective_scans,
        received_by,
    )

    receipt = db.execute(
        text(
            """
            INSERT INTO shipyard_receipts
                (ticket_id, idempotency_key,
                 previous_received_quantity, received_quantity,
                 balance_after, resulting_ticket_status, bin_id,
                 location_path_snapshot, difference_reason,
                 client_device_id, received_by)
            VALUES
                (:ticket_id, :idempotency_key,
                 :previous_received_quantity, :received_quantity,
                 :balance_after, :resulting_ticket_status, :bin_id,
                 :location_path_snapshot, :difference_reason,
                 :client_device_id, :received_by)
            RETURNING receipt_id
            """
        ),
        {
            "ticket_id": ticket_id,
            "idempotency_key": validated.idempotency_key,
            "previous_received_quantity": context.received_quantity,
            "received_quantity": validated.received_quantity,
            "balance_after": new_on_hand,
            "resulting_ticket_status": ticket_status,
            "bin_id": context.assigned_bin_id,
            "location_path_snapshot": context.location_path_snapshot,
            "difference_reason": validated.difference_reason,
            "client_device_id": validated.client_device_id,
            "received_by": received_by,
        },
    ).fetchone()
    receipt_id = receipt.receipt_id

    _attach_receipt_evidence(
        db,
        receipt_id=receipt_id,
        scans=effective_scans,
        pallet_ids=pallet_ids,
        photos=photos,
        optional_photos=optional_photos,
    )

    db.execute(
        text(
            """
            UPDATE shipyard_cargo_tickets
               SET received_quantity = :received_quantity,
                   on_hand_quantity = :on_hand_quantity,
                   status = :status,
                   shortage_status = :shortage_status,
                   updated_at = NOW()
             WHERE ticket_id = :ticket_id
            """
        ),
        {
            "ticket_id": ticket_id,
            "received_quantity": new_received,
            "on_hand_quantity": new_on_hand,
            "status": ticket_status,
            "shortage_status": shortage_status,
        },
    )
    line_status = (
        SHIPYARD_LINE_RECEIVED
        if new_received >= context.declared_quantity
        else SHIPYARD_LINE_PARTIAL
        if new_received > 0
        else SHIPYARD_LINE_PENDING
    )
    db.execute(
        text(
            """
            UPDATE shipyard_inbound_lines
               SET status = :status, updated_at = NOW()
             WHERE line_id = :line_id
            """
        ),
        {"status": line_status, "line_id": context.line_id},
    )
    manifest_status = _refresh_manifest_status(db, str(context.manifest_id))
    db.execute(
        text(
            """
            INSERT INTO shipyard_stock_ledger
                (ticket_id, source_type, source_id, quantity_delta,
                 balance_after, warehouse_id, bin_id,
                 location_path_snapshot, actor)
            VALUES
                (:ticket_id, :source_type, :source_id, :quantity_delta,
                 :balance_after, :warehouse_id, :bin_id,
                 :location_path_snapshot, :actor)
            """
        ),
        {
            "ticket_id": ticket_id,
            "source_type": SHIPYARD_LEDGER_RECEIPT,
            "source_id": receipt_id,
            "quantity_delta": validated.received_quantity,
            "balance_after": new_on_hand,
            "warehouse_id": context.warehouse_id,
            "bin_id": context.assigned_bin_id,
            "location_path_snapshot": context.location_path_snapshot,
            "actor": received_by,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_TICKET_RECEIVED,
        entity_type="shipyard_cargo_ticket",
        entity_id=0,
        user_id=received_by,
        warehouse_id=context.warehouse_id,
        device_id=validated.client_device_id,
        details={
            "ticket_id": ticket_id,
            "ticket_no": context.ticket_no,
            "manifest_id": str(context.manifest_id),
            "receipt_id": str(receipt_id),
            "received_quantity": _decimal_text(validated.received_quantity),
            "cumulative_received_quantity": _decimal_text(new_received),
            "short_quantity": _decimal_text(
                max(context.declared_quantity - new_received, Decimal(0))
            ),
            "tag_suffixes": suffixes,
            "ticket_status": ticket_status,
            "manifest_status": manifest_status,
            "location_path_snapshot": context.location_path_snapshot,
        },
    )
    db.commit()
    return _receipt_summary(db, str(receipt_id))


def report_ticket_shortage(
    db,
    *,
    ticket_id: str,
    status: str,
    note: str,
    reported_by: str,
) -> dict[str, Any]:
    if status not in (
        SHIPYARD_SHORTAGE_REPORTED,
        SHIPYARD_SHORTAGE_IN_TRANSIT,
    ):
        raise ShipyardValidationError(
            "缺件报送状态必须是 REPORTED 或 IN_TRANSIT",
            {"status": status},
        )
    context = _ticket_context(db, ticket_id, for_update=True)
    if context is None:
        raise TicketNotFoundError("票货不存在")
    if context.received_quantity >= context.declared_quantity:
        raise ShortageStateError(
            "票货已经全部到货，不需要登记缺件报送",
            {"ticket_id": ticket_id},
        )
    db.execute(
        text(
            """
            UPDATE shipyard_cargo_tickets
               SET shortage_status = :shortage_status,
                   shortage_reported_at = NOW(),
                   shortage_reported_by = :reported_by,
                   shortage_note = :note,
                   updated_at = NOW()
             WHERE ticket_id = :ticket_id
            """
        ),
        {
            "ticket_id": ticket_id,
            "shortage_status": status,
            "reported_by": reported_by,
            "note": note,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_SHORTAGE_UPDATED,
        entity_type="shipyard_cargo_ticket",
        entity_id=0,
        user_id=reported_by,
        warehouse_id=context.warehouse_id,
        details={
            "ticket_id": ticket_id,
            "ticket_no": context.ticket_no,
            "previous_status": context.shortage_status,
            "status": status,
            "note": note,
        },
    )
    db.commit()
    updated = _ticket_context(db, ticket_id)
    manifest_payload = get_manifest_payload(db, str(context.manifest_id))
    return {
        "ticket": _ticket_payload(updated),
        "manifest": {
            "manifest_id": str(context.manifest_id),
            "status": manifest_payload["manifest"]["status"],
            "summary": manifest_payload["validation"]["summary"],
        },
    }
