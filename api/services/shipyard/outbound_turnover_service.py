"""Whole-ticket outbound scan, photo evidence, stock depletion and loading."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_OUTBOUND_LOADED,
    ACTION_SHIPYARD_TICKET_OUTBOUND,
    SHIPYARD_LABEL_RECOGNITION_FAILED,
    SHIPYARD_LABEL_RECOGNITION_MATCHED,
    SHIPYARD_LABEL_RECOGNITION_NEEDS_REVIEW,
    SHIPYARD_LEDGER_OUTBOUND,
    SHIPYARD_OUTBOUND_LINE_ALLOCATED,
    SHIPYARD_OUTBOUND_LINE_OUTBOUND,
    SHIPYARD_OUTBOUND_LINE_SHORTAGE,
    SHIPYARD_OUTBOUND_ORDER_LOADED,
    SHIPYARD_OUTBOUND_ORDER_OUTBOUND,
    SHIPYARD_OUTBOUND_ORDER_PARTIAL,
    SHIPYARD_OUTBOUND_ORDER_PENDING,
    SHIPYARD_OUTBOUND_TASK_OUTBOUND,
    SHIPYARD_OUTBOUND_TASK_PENDING,
    SHIPYARD_PALLET_OUTBOUND,
    SHIPYARD_PALLET_STORED,
    SHIPYARD_PHOTO_PALLET,
    SHIPYARD_TICKET_ALLOCATED,
    SHIPYARD_TICKET_OUTBOUND,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    OcrProviderError,
    OcrUnavailableError,
    OutboundOrderNotFoundError,
    OutboundStateError,
    OutboundTaskNotFoundError,
    OutboundValidationError,
    PhotoNotFoundError,
    PhotoValidationError,
)
from services.shipyard.label_ocr import recognize_label_image
from services.shipyard.label_recognition_service import (
    append_required_field_mismatches,
    fields_scan_view,
    load_label_recognition,
    recognition_scan_view,
    save_label_recognition,
)
from services.shipyard.outbound_manifest_service import (
    get_outbound_order_payload,
)
from utils.uploads import MAX_UPLOAD_BYTES

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
    return str(value)


def _decimal_text(value: Any) -> str:
    return format(Decimal(value), "f")


def _hash_qr(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized(value: str | None) -> str:
    return (value or "").strip().casefold()


def _normalize_suffix(value: str | None) -> str | None:
    text = (value or "").strip()
    if re.fullmatch(r"[1-9]\d*-[1-9]\d*", text):
        return text
    return None


def _scan_payload(scan) -> dict[str, Any]:
    if hasattr(scan, "model_dump"):
        return scan.model_dump(mode="json")
    return {
        "tag_suffix": scan.tag_suffix,
        "label_recognition_id": getattr(scan, "label_recognition_id", None),
        "ship_no": scan.ship_no,
        "engineering_no": scan.engineering_no,
        "production_drawing_no": scan.production_drawing_no,
        "material_code": getattr(scan, "material_code", None),
        "item_name": getattr(scan, "item_name", None),
        "batch_no": scan.batch_no,
        "label_quantity": (
            _decimal_text(scan.label_quantity)
            if scan.label_quantity is not None
            else None
        ),
    }


def _task_context(db, task_id: str, *, for_update: bool = False):
    lock = "FOR UPDATE OF t, c, o" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT t.task_id, t.task_no, t.outbound_order_id,
                   t.order_line_id, t.ticket_id, t.planned_quantity,
                   t.expected_pallet_count, t.expected_tag_suffixes,
                   t.location_path_snapshot, t.material_snapshot,
                   t.status, t.created_at,
                   im.warehouse_id, c.on_hand_quantity, c.status
                       AS ticket_status, c.assigned_bin_id,
                   c.stock_lot_number,
                   o.outbound_order_no, o.factory_shipment_no,
                   o.status AS order_status,
                   l.source_row_no, l.ticket_no, l.ship_no,
                   l.engineering_no, l.production_drawing_no,
                   l.manufacturer_name, l.material_code, l.item_name,
                   l.item_spec, l.demand_quantity, l.unit,
                   l.allocated_quantity, l.fulfilled_quantity,
                   b.bin_code, z.zone_code, z.zone_name
              FROM shipyard_outbound_tasks t
              JOIN shipyard_outbound_orders o
                ON o.outbound_order_id = t.outbound_order_id
              JOIN shipyard_outbound_order_lines l
                ON l.order_line_id = t.order_line_id
              JOIN shipyard_cargo_tickets c
                ON c.ticket_id = t.ticket_id
              JOIN shipyard_inbound_lines il
                ON il.line_id = c.inbound_line_id
              JOIN shipyard_inbound_manifests im
                ON im.manifest_id = il.manifest_id
              JOIN bins b ON b.bin_id = c.assigned_bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
             WHERE t.task_id = :task_id
             {lock}
            """  # nosec B608
        ),
        {"task_id": task_id},
    ).fetchone()


def get_outbound_task_context(db, task_id: str):
    context = _task_context(db, task_id)
    if context is None:
        raise OutboundTaskNotFoundError("出库任务不存在")
    return context


def _task_pallets(db, ticket_id: str, *, for_update: bool = False):
    lock = "FOR UPDATE" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT pallet_id, ticket_id, tag_suffix, qr_payload,
                   qr_payload_sha256, factory_pallet_no,
                   installation_pallet_no, batch_no, status
              FROM shipyard_pallet_units
             WHERE ticket_id = :ticket_id
             ORDER BY tag_suffix NULLS FIRST, pallet_id
             {lock}
            """  # nosec B608
        ),
        {"ticket_id": ticket_id},
    ).fetchall()


def _ensure_task_pending(context) -> None:
    if context.status != SHIPYARD_OUTBOUND_TASK_PENDING:
        raise OutboundStateError(
            "当前出库任务已经完成或进入异常",
            {"task_id": str(context.task_id), "status": context.status},
        )
    if context.ticket_status != SHIPYARD_TICKET_ALLOCATED:
        raise OutboundStateError(
            "当前票货不是已分配待出库状态",
            {
                "ticket_id": str(context.ticket_id),
                "status": context.ticket_status,
            },
        )
    if context.on_hand_quantity <= 0:
        raise OutboundStateError(
            "当前票货没有可用库存",
            {"ticket_id": str(context.ticket_id)},
        )


def _pallet_by_qr(db, qr_hash: str):
    return db.execute(
        text(
            """
            SELECT p.pallet_id, p.ticket_id, p.tag_suffix, p.qr_payload,
                   p.qr_payload_sha256, p.factory_pallet_no,
                   p.installation_pallet_no, p.batch_no, p.status,
                   t.task_id, t.task_no, t.outbound_order_id,
                   t.status AS task_status
              FROM shipyard_pallet_units p
              LEFT JOIN shipyard_outbound_tasks t
                ON t.ticket_id = p.ticket_id
             WHERE p.qr_payload_sha256 = :qr_hash
             LIMIT 1
            """
        ),
        {"qr_hash": qr_hash},
    ).fetchone()


def _field_mismatches(context, scan) -> list[dict[str, Any]]:
    expected = (
        ("ship_no", context.ship_no, scan.ship_no),
        ("engineering_no", context.engineering_no, scan.engineering_no),
        (
            "production_drawing_no",
            context.production_drawing_no,
            scan.production_drawing_no,
        ),
        ("material_code", context.material_code, scan.material_code),
        ("item_name", context.item_name, scan.item_name),
        (
            "label_quantity",
            context.planned_quantity,
            scan.label_quantity,
        ),
    )
    mismatches = []
    for field, expected_value, actual_value in expected:
        if actual_value is None:
            continue
        if field == "label_quantity":
            matches = Decimal(expected_value) == Decimal(actual_value)
        else:
            matches = _normalized(str(expected_value)) == _normalized(str(actual_value))
        if not matches:
            mismatches.append(
                {
                    "field": field,
                    "expected": _decimal_text(expected_value)
                    if field == "label_quantity"
                    else str(expected_value or ""),
                    "actual": _decimal_text(actual_value)
                    if field == "label_quantity"
                    else str(actual_value),
                    "reason": "MISMATCH",
                }
            )
    return mismatches


def validate_outbound_pallet_scan(
    db,
    *,
    task_id: str,
    scan,
) -> dict[str, Any]:
    if not scan.qr_payload:
        raise OutboundValidationError("请使用吊牌照片识别接口")
    if not scan.material_code or not scan.item_name:
        raise OutboundValidationError(
            "旧版扫码接口必须提供物料编码和品名",
            {"required_fields": ["material_code", "item_name"]},
        )
    context = _task_context(db, task_id)
    if context is None:
        raise OutboundTaskNotFoundError("出库任务不存在")
    _ensure_task_pending(context)
    qr_hash = _hash_qr(scan.qr_payload.strip())
    pallet = _pallet_by_qr(db, qr_hash)
    if pallet is None:
        raise OutboundValidationError(
            "吊牌不在已收货库存中",
            {"qr_payload_sha256": qr_hash},
        )
    if str(pallet.ticket_id) != str(context.ticket_id):
        raise OutboundValidationError(
            "吊牌属于其他票货",
            {
                "tag_suffix": pallet.tag_suffix,
                "matched_task_id": (str(pallet.task_id) if pallet.task_id else None),
                "matched_task_no": pallet.task_no,
                "matched_outbound_order_id": (
                    str(pallet.outbound_order_id) if pallet.outbound_order_id else None
                ),
            },
        )
    if pallet.status != SHIPYARD_PALLET_STORED:
        raise OutboundStateError(
            "吊牌对应托盘不是可出库在库状态",
            {
                "pallet_id": str(pallet.pallet_id),
                "status": pallet.status,
            },
        )
    if scan.tag_suffix != pallet.tag_suffix:
        raise OutboundValidationError(
            "扫描后缀与吊牌实际托盘后缀不一致",
            {
                "scanned_tag_suffix": scan.tag_suffix,
                "stored_tag_suffix": pallet.tag_suffix,
            },
        )
    if scan.batch_no and _normalized(scan.batch_no) != _normalized(pallet.batch_no):
        raise OutboundValidationError(
            "扫描批次号与吊牌库存不一致",
            {"expected": pallet.batch_no, "actual": scan.batch_no},
        )
    mismatches = _field_mismatches(context, scan)
    if mismatches:
        raise OutboundValidationError(
            "吊牌共同信息与出库任务不一致",
            {"mismatches": mismatches},
        )
    return {
        "matched": True,
        "task": {
            "task_id": str(context.task_id),
            "task_no": context.task_no,
            "planned_quantity": _decimal_text(context.planned_quantity),
            "expected_tag_suffixes": list(context.expected_tag_suffixes),
            "material_code": context.material_code,
            "item_name": context.item_name,
            "item_spec": context.item_spec,
            "location_path_snapshot": context.location_path_snapshot,
        },
        "pallet": {
            "pallet_id": str(pallet.pallet_id),
            "tag_suffix": pallet.tag_suffix,
            "qr_payload_sha256": pallet.qr_payload_sha256,
            "factory_pallet_no": pallet.factory_pallet_no,
            "installation_pallet_no": pallet.installation_pallet_no,
            "batch_no": pallet.batch_no,
            "status": pallet.status,
        },
    }


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


def save_outbound_task_photo(
    db,
    *,
    task_id: str,
    tag_suffix: str | None,
    file_name: str,
    content_type: str,
    content: bytes,
    uploaded_by: str,
) -> dict[str, Any]:
    context = _task_context(db, task_id)
    if context is None:
        raise OutboundTaskNotFoundError("出库任务不存在")
    _ensure_task_pending(context)
    pallet = None
    if tag_suffix:
        if tag_suffix not in set(context.expected_tag_suffixes):
            raise OutboundValidationError(
                "照片后缀不属于当前整票任务",
                {
                    "tag_suffix": tag_suffix,
                    "expected_tag_suffixes": list(context.expected_tag_suffixes),
                },
            )
        pallet = db.execute(
            text(
                """
                SELECT pallet_id, status
                  FROM shipyard_pallet_units
                 WHERE ticket_id = :ticket_id
                   AND tag_suffix = :tag_suffix
                """
            ),
            {
                "ticket_id": str(context.ticket_id),
                "tag_suffix": tag_suffix,
            },
        ).fetchone()
        if pallet is None:
            raise OutboundValidationError(
                "当前票货不存在该托盘后缀",
                {"tag_suffix": tag_suffix},
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
    row = db.execute(
        text(
            """
            INSERT INTO shipyard_photos
                (ticket_id, warehouse_id, pallet_id, tag_suffix,
                 photo_kind, file_name, content_type, file_size,
                 file_sha256, file_bytes, uploaded_by, outbound_task_id)
            VALUES
                (:ticket_id, :warehouse_id, :pallet_id, :tag_suffix,
                 :photo_kind, :file_name, :content_type, :file_size,
                 :file_sha256, :file_bytes, :uploaded_by, :outbound_task_id)
            RETURNING photo_id, ticket_id, warehouse_id, receipt_id,
                      pallet_id, tag_suffix, photo_kind, file_name,
                      content_type, file_size, file_sha256, uploaded_by,
                      outbound_task_id, outbound_confirmation_id, created_at
            """
        ),
        {
            "ticket_id": str(context.ticket_id),
            "warehouse_id": context.warehouse_id,
            "pallet_id": str(pallet.pallet_id) if pallet else None,
            "tag_suffix": tag_suffix,
            "photo_kind": SHIPYARD_PHOTO_PALLET,
            "file_name": file_name[:255],
            "content_type": content_type,
            "file_size": len(content),
            "file_sha256": hashlib.sha256(content).hexdigest(),
            "file_bytes": content,
            "uploaded_by": uploaded_by,
            "outbound_task_id": task_id,
        },
    ).fetchone()
    db.commit()
    return {"photo": _photo_payload(row)}


def _matching_outbound_tasks(db, fields: dict[str, Any], suffix: str | None):
    material_code = fields.get("material_code")
    item_name = fields.get("item_name")
    if not material_code or not item_name:
        return []
    rows = db.execute(
        text(
            """
            SELECT t.task_id, t.task_no, t.outbound_order_id,
                   o.outbound_order_no
              FROM shipyard_outbound_tasks t
              JOIN shipyard_outbound_order_lines l
                ON l.order_line_id = t.order_line_id
              JOIN shipyard_outbound_orders o
                ON o.outbound_order_id = t.outbound_order_id
             WHERE t.status = :pending
               AND upper(trim(l.material_code)) = upper(trim(:material_code))
               AND trim(l.item_name) = trim(:item_name)
               AND (
                    :tag_suffix = ''
                    OR :tag_suffix = ANY(t.expected_tag_suffixes)
               )
             ORDER BY t.created_at
             LIMIT 5
            """
        ),
        {
            "pending": SHIPYARD_OUTBOUND_TASK_PENDING,
            "material_code": material_code,
            "item_name": item_name,
            "tag_suffix": suffix or "",
        },
    ).fetchall()
    return [
        {
            "task_id": str(row.task_id),
            "task_no": row.task_no,
            "outbound_order_id": str(row.outbound_order_id),
            "outbound_order_no": row.outbound_order_no,
        }
        for row in rows
    ]


def recognize_outbound_label_photo(
    db,
    *,
    task_id: str,
    file_name: str,
    content_type: str,
    content: bytes,
    uploaded_by: str,
) -> dict[str, Any]:
    context = _task_context(db, task_id)
    if context is None:
        raise OutboundTaskNotFoundError("出库任务不存在")
    _ensure_task_pending(context)
    photo_result = save_outbound_task_photo(
        db,
        task_id=task_id,
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
            ticket_id=str(context.ticket_id),
            task_id=task_id,
            warehouse_id=context.warehouse_id,
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
    mismatches = _field_mismatches(context, scan)
    append_required_field_mismatches(mismatches, scan)
    if suffix and suffix not in set(context.expected_tag_suffixes):
        mismatches.append(
            {
                "field": "tag_suffix",
                "expected": list(context.expected_tag_suffixes),
                "actual": suffix,
                "reason": "MISMATCH",
            }
        )

    pallet = None
    if suffix:
        pallet = db.execute(
            text(
                """
                SELECT pallet_id, status
                  FROM shipyard_pallet_units
                 WHERE ticket_id = :ticket_id
                   AND tag_suffix = :tag_suffix
                """
            ),
            {
                "ticket_id": str(context.ticket_id),
                "tag_suffix": suffix,
            },
        ).fetchone()
        if pallet is None:
            mismatches.append(
                {
                    "field": "tag_suffix",
                    "expected": list(context.expected_tag_suffixes),
                    "actual": suffix,
                    "reason": "NOT_FOUND",
                }
            )
        else:
            db.execute(
                text(
                    """
                    UPDATE shipyard_photos
                       SET tag_suffix = :tag_suffix, pallet_id = :pallet_id
                     WHERE photo_id = :photo_id
                    """
                ),
                {
                    "tag_suffix": suffix,
                    "pallet_id": str(pallet.pallet_id),
                    "photo_id": photo["photo_id"],
                },
            )
            photo["tag_suffix"] = suffix
            photo["pallet_id"] = str(pallet.pallet_id)

    candidates = _matching_outbound_tasks(db, fields, suffix)
    candidates = [
        item for item in candidates if item["task_id"] != str(context.task_id)
    ]
    status = (
        SHIPYARD_LABEL_RECOGNITION_MATCHED
        if suffix and not mismatches
        else SHIPYARD_LABEL_RECOGNITION_NEEDS_REVIEW
    )
    recognition = save_label_recognition(
        db,
        photo_id=photo["photo_id"],
        ticket_id=str(context.ticket_id),
        task_id=task_id,
        warehouse_id=context.warehouse_id,
        result=ocr,
        status=status,
        actor=uploaded_by,
    )
    return {
        "matched": status == SHIPYARD_LABEL_RECOGNITION_MATCHED,
        "needs_suffix": suffix is None,
        "remaining_tag_suffixes": list(context.expected_tag_suffixes),
        "recognized": fields,
        "mismatches": mismatches,
        "candidate_tasks": candidates[:3],
        "photo": photo,
        "recognition": recognition,
        "task": {
            "task_id": str(context.task_id),
            "task_no": context.task_no,
            "planned_quantity": _decimal_text(context.planned_quantity),
            "expected_tag_suffixes": list(context.expected_tag_suffixes),
            "material_code": context.material_code,
            "item_name": context.item_name,
            "item_spec": context.item_spec,
            "location_path_snapshot": context.location_path_snapshot,
        },
    }


def get_outbound_photo(db, *, photo_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT photo_id, ticket_id, warehouse_id, pallet_id,
                   outbound_task_id, outbound_confirmation_id,
                   tag_suffix, photo_kind, file_name, content_type,
                   file_size, file_sha256, file_bytes, uploaded_by,
                   created_at
              FROM shipyard_photos
             WHERE photo_id = :photo_id
               AND outbound_task_id IS NOT NULL
            """
        ),
        {"photo_id": photo_id},
    ).fetchone()
    if row is None:
        raise PhotoNotFoundError("出库照片不存在")
    return {
        **_photo_payload(row),
        "warehouse_id": row.warehouse_id,
        "ticket_id": str(row.ticket_id),
        "file_bytes": bytes(row.file_bytes),
    }


def _evidence_photo(
    db,
    photo_id,
    *,
    context,
    suffix: str,
    pallet_id: str,
    actor: str,
):
    row = db.execute(
        text(
            """
            SELECT photo_id, ticket_id, warehouse_id, receipt_id,
                   pallet_id, tag_suffix, photo_kind, file_name,
                   content_type, file_size, file_sha256, uploaded_by,
                   outbound_task_id, outbound_confirmation_id, created_at
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
    checks = (
        (str(row.ticket_id) == str(context.ticket_id), "照片不属于当前票货"),
        (
            str(row.outbound_task_id) == str(context.task_id),
            "照片不属于当前出库任务",
        ),
        (row.outbound_confirmation_id is None, "照片已用于其他出库记录"),
        (row.photo_kind == SHIPYARD_PHOTO_PALLET, "照片类型必须为 PALLET"),
        (
            row.tag_suffix is None or row.tag_suffix == suffix,
            "照片后缀与当前托盘不一致",
        ),
        (
            row.pallet_id is None or str(row.pallet_id) == str(pallet_id),
            "照片未关联到正确托盘",
        ),
        (row.uploaded_by == actor, "只能使用当前出库人上传的照片"),
    )
    for passed, message in checks:
        if not passed:
            raise PhotoValidationError(
                message,
                {"photo_id": str(photo_id)},
            )
    return row


def _refresh_order_status(db, outbound_order_id: str) -> str:
    task_counts = db.execute(
        text(
            """
            SELECT COUNT(*) AS task_count,
                   COUNT(*) FILTER (WHERE status = 'PENDING')
                       AS pending_count,
                   COUNT(*) FILTER (WHERE status = 'OUTBOUND')
                       AS outbound_count
              FROM shipyard_outbound_tasks
             WHERE outbound_order_id = :outbound_order_id
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchone()
    shortage_count = db.execute(
        text(
            """
            SELECT COUNT(*)
              FROM shipyard_outbound_order_lines
             WHERE outbound_order_id = :outbound_order_id
               AND allocated_quantity < demand_quantity
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).scalar()
    if task_counts.pending_count:
        status = (
            SHIPYARD_OUTBOUND_ORDER_PARTIAL
            if task_counts.outbound_count
            else SHIPYARD_OUTBOUND_ORDER_PENDING
        )
    elif shortage_count:
        status = SHIPYARD_OUTBOUND_ORDER_PARTIAL
    else:
        status = SHIPYARD_OUTBOUND_ORDER_OUTBOUND
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_orders
               SET status = :status, updated_at = NOW()
             WHERE outbound_order_id = :outbound_order_id
            """
        ),
        {"status": status, "outbound_order_id": outbound_order_id},
    )
    return status


def _confirmation_summary(db, confirmation_id: str) -> dict[str, Any]:
    confirmation = db.execute(
        text(
            """
            SELECT confirmation_id, task_id, outbound_order_id, ticket_id,
                   idempotency_key, quantity, previous_on_hand,
                   balance_after, bin_id, location_path_snapshot,
                   client_device_id, outbound_by, outbound_at
              FROM shipyard_outbound_confirmations
             WHERE confirmation_id = :confirmation_id
            """
        ),
        {"confirmation_id": confirmation_id},
    ).fetchone()
    pallets = db.execute(
        text(
            """
            SELECT rp.pallet_id, rp.tag_suffix, rp.qr_payload,
                   rp.qr_payload_sha256, rp.photo_id,
                   rp.label_recognition_id,
                   lr.status AS label_recognition_status,
                   lr.fields AS label_recognition_fields,
                   ph.file_name, ph.content_type, ph.file_size,
                   ph.file_sha256
              FROM shipyard_outbound_confirmation_pallets rp
              JOIN shipyard_photos ph ON ph.photo_id = rp.photo_id
              LEFT JOIN shipyard_label_recognitions lr
                ON lr.recognition_id = rp.label_recognition_id
             WHERE rp.confirmation_id = :confirmation_id
             ORDER BY rp.tag_suffix
            """
        ),
        {"confirmation_id": confirmation_id},
    ).fetchall()
    order_payload = get_outbound_order_payload(
        db,
        outbound_order_id=str(confirmation.outbound_order_id),
    )
    task = next(
        (
            item
            for item in order_payload["tasks"]
            if item["task_id"] == str(confirmation.task_id)
        ),
        None,
    )
    return {
        "confirmation": {
            "confirmation_id": str(confirmation.confirmation_id),
            "task_id": str(confirmation.task_id),
            "outbound_order_id": str(confirmation.outbound_order_id),
            "ticket_id": str(confirmation.ticket_id),
            "idempotency_key": confirmation.idempotency_key,
            "quantity": _decimal_text(confirmation.quantity),
            "previous_on_hand": _decimal_text(confirmation.previous_on_hand),
            "balance_after": _decimal_text(confirmation.balance_after),
            "bin_id": confirmation.bin_id,
            "location_path_snapshot": (confirmation.location_path_snapshot),
            "client_device_id": confirmation.client_device_id,
            "outbound_by": confirmation.outbound_by,
            "outbound_at": _iso(confirmation.outbound_at),
        },
        "pallets": [
            {
                "pallet_id": str(pallet.pallet_id),
                "tag_suffix": pallet.tag_suffix,
                "qr_payload": pallet.qr_payload,
                "qr_payload_sha256": pallet.qr_payload_sha256,
                "label_recognition": {
                    "recognition_id": str(pallet.label_recognition_id),
                    "status": pallet.label_recognition_status,
                    "fields": dict(pallet.label_recognition_fields or {}),
                }
                if pallet.label_recognition_id
                else None,
                "photo": {
                    "photo_id": str(pallet.photo_id),
                    "file_name": pallet.file_name,
                    "content_type": pallet.content_type,
                    "file_size": pallet.file_size,
                    "file_sha256": pallet.file_sha256,
                },
            }
            for pallet in pallets
        ],
        "task": task,
        "order": order_payload["order"],
        "summary": order_payload["summary"],
    }


def confirm_outbound_task(
    db,
    *,
    task_id: str,
    validated,
    outbound_by: str,
) -> dict[str, Any]:
    lock_key = f"{task_id}:outbound:{validated.idempotency_key}"
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    existing = db.execute(
        text(
            """
            SELECT confirmation_id, idempotency_key
              FROM shipyard_outbound_confirmations
             WHERE task_id = :task_id
            """
        ),
        {"task_id": task_id},
    ).fetchone()
    if existing is not None:
        if existing.idempotency_key != validated.idempotency_key:
            raise OutboundStateError(
                "该票货已经完成出库",
                {
                    "task_id": task_id,
                    "confirmation_id": str(existing.confirmation_id),
                },
            )
        return _confirmation_summary(db, str(existing.confirmation_id))

    context = _task_context(db, task_id, for_update=True)
    if context is None:
        raise OutboundTaskNotFoundError("出库任务不存在")
    _ensure_task_pending(context)
    pallets = _task_pallets(
        db,
        str(context.ticket_id),
        for_update=True,
    )
    expected_suffixes = [pallet.tag_suffix for pallet in pallets]
    if (
        not pallets
        or any(not suffix for suffix in expected_suffixes)
        or len(set(expected_suffixes)) != len(expected_suffixes)
    ):
        raise OutboundValidationError(
            "当前票货的托盘后缀未齐套或存在重复",
            {"tag_suffixes": expected_suffixes},
        )
    if set(expected_suffixes) != set(context.expected_tag_suffixes):
        raise OutboundValidationError(
            "出库任务托盘后缀与库存不一致",
            {
                "task_tag_suffixes": list(context.expected_tag_suffixes),
                "stock_tag_suffixes": expected_suffixes,
            },
        )
    scan_suffixes = [scan.tag_suffix for scan in validated.pallets]
    if len(set(scan_suffixes)) != len(scan_suffixes):
        raise OutboundValidationError(
            "本次提交存在重复托盘后缀",
            {"tag_suffixes": scan_suffixes},
        )
    if set(scan_suffixes) != set(expected_suffixes):
        raise OutboundValidationError(
            "整票托盘未拍齐",
            {
                "expected_tag_suffixes": expected_suffixes,
                "scanned_tag_suffixes": scan_suffixes,
                "missing_tag_suffixes": sorted(
                    set(expected_suffixes) - set(scan_suffixes)
                ),
                "unexpected_tag_suffixes": sorted(
                    set(scan_suffixes) - set(expected_suffixes)
                ),
            },
        )
    pallet_by_suffix = {pallet.tag_suffix: pallet for pallet in pallets}
    evidence_rows = []
    seen_photo_ids = set()
    seen_pallet_ids = set()
    for scan in validated.pallets:
        pallet = pallet_by_suffix[scan.tag_suffix]
        qr_hash = None
        if scan.label_recognition_id:
            recognition = load_label_recognition(
                db,
                str(scan.label_recognition_id),
                ticket_id=str(context.ticket_id),
                task_id=str(context.task_id),
                photo_id=str(scan.photo_id),
                for_update=True,
            )
            if recognition.status == SHIPYARD_LABEL_RECOGNITION_FAILED:
                raise OutboundValidationError(
                    "吊牌 OCR 识别失败，请重新拍照",
                    {"tag_suffix": scan.tag_suffix},
                )
            recognized_suffix = _normalize_suffix(
                str(dict(recognition.fields or {}).get("tag_suffix") or "")
            )
            if recognized_suffix and recognized_suffix != scan.tag_suffix:
                raise OutboundValidationError(
                    "OCR 托盘后缀与本次提交不一致",
                    {
                        "expected": recognized_suffix,
                        "actual": scan.tag_suffix,
                    },
                )
            effective_scan = recognition_scan_view(
                recognition,
                suffix=scan.tag_suffix,
            )
        else:
            if not scan.qr_payload:
                raise OutboundValidationError(
                    "吊牌照片缺少 OCR 识别结果，请重新拍照",
                    {"tag_suffix": scan.tag_suffix},
                )
            qr_hash = _hash_qr(scan.qr_payload.strip())
            if pallet.qr_payload_sha256 != qr_hash:
                raise OutboundValidationError(
                    "扫描二维码与托盘实际吊牌不一致",
                    {"tag_suffix": scan.tag_suffix},
                )
            effective_scan = scan
        if pallet.status != SHIPYARD_PALLET_STORED:
            raise OutboundStateError(
                "托盘不是可出库在库状态",
                {
                    "pallet_id": str(pallet.pallet_id),
                    "status": pallet.status,
                },
            )
        if effective_scan.batch_no and _normalized(
            effective_scan.batch_no
        ) != _normalized(pallet.batch_no):
            raise OutboundValidationError(
                "吊牌批次号与库存不一致",
                {"tag_suffix": scan.tag_suffix},
            )
        mismatches = _field_mismatches(context, effective_scan)
        if scan.label_recognition_id:
            append_required_field_mismatches(mismatches, effective_scan)
        if mismatches:
            raise OutboundValidationError(
                "吊牌共同信息与出库任务不一致",
                {"tag_suffix": scan.tag_suffix, "mismatches": mismatches},
            )
        photo = _evidence_photo(
            db,
            scan.photo_id,
            context=context,
            suffix=scan.tag_suffix,
            pallet_id=str(pallet.pallet_id),
            actor=outbound_by,
        )
        if photo.photo_id in seen_photo_ids:
            raise PhotoValidationError("同一张照片不能用于多个托盘")
        if pallet.pallet_id in seen_pallet_ids:
            raise OutboundValidationError("同一托盘不能重复核销")
        seen_photo_ids.add(photo.photo_id)
        seen_pallet_ids.add(pallet.pallet_id)
        if photo.tag_suffix is None or photo.pallet_id is None:
            db.execute(
                text(
                    """
                    UPDATE shipyard_photos
                       SET tag_suffix = :tag_suffix,
                           pallet_id = :pallet_id
                     WHERE photo_id = :photo_id
                    """
                ),
                {
                    "tag_suffix": scan.tag_suffix,
                    "pallet_id": str(pallet.pallet_id),
                    "photo_id": str(photo.photo_id),
                },
            )
        evidence_rows.append(
            {
                "scan": effective_scan,
                "pallet": pallet,
                "photo": photo,
                "qr_hash": qr_hash,
                "label_recognition_id": scan.label_recognition_id,
            }
        )

    quantity = Decimal(context.on_hand_quantity)
    if quantity != Decimal(context.planned_quantity):
        raise OutboundStateError(
            "可用库存与任务分配数量不一致",
            {
                "task_quantity": _decimal_text(context.planned_quantity),
                "on_hand_quantity": _decimal_text(quantity),
            },
        )
    if quantity <= 0:
        raise OutboundStateError("库存数量必须大于零")
    confirmation = db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_confirmations
                (task_id, outbound_order_id, ticket_id, idempotency_key,
                 quantity, previous_on_hand, balance_after, bin_id,
                 location_path_snapshot, scan_payloads,
                 client_device_id, outbound_by)
            VALUES
                (:task_id, :outbound_order_id, :ticket_id,
                 :idempotency_key, :quantity, :previous_on_hand, 0,
                 :bin_id, :location_path_snapshot, :scan_payloads,
                 :client_device_id, :outbound_by)
            RETURNING confirmation_id
            """
        ),
        {
            "task_id": task_id,
            "outbound_order_id": str(context.outbound_order_id),
            "ticket_id": str(context.ticket_id),
            "idempotency_key": validated.idempotency_key,
            "quantity": quantity,
            "previous_on_hand": context.on_hand_quantity,
            "bin_id": context.assigned_bin_id,
            "location_path_snapshot": context.location_path_snapshot,
            "scan_payloads": Json(
                [_scan_payload(item["scan"]) for item in evidence_rows]
            ),
            "client_device_id": validated.client_device_id,
            "outbound_by": outbound_by,
        },
    ).fetchone()
    confirmation_id = confirmation.confirmation_id
    for item in evidence_rows:
        db.execute(
            text(
                """
                INSERT INTO shipyard_outbound_confirmation_pallets
                    (confirmation_id, pallet_id, tag_suffix,
                     qr_payload, qr_payload_sha256, photo_id,
                     label_recognition_id)
                VALUES
                    (:confirmation_id, :pallet_id, :tag_suffix,
                     :qr_payload, :qr_payload_sha256, :photo_id,
                     :label_recognition_id)
                """
            ),
            {
                "confirmation_id": confirmation_id,
                "pallet_id": str(item["pallet"].pallet_id),
                "tag_suffix": item["pallet"].tag_suffix,
                "qr_payload": item["pallet"].qr_payload,
                "qr_payload_sha256": (
                    item["qr_hash"] or item["pallet"].qr_payload_sha256
                ),
                "photo_id": str(item["photo"].photo_id),
                "label_recognition_id": item["label_recognition_id"],
            },
        )
        db.execute(
            text(
                """
                UPDATE shipyard_photos
                   SET outbound_confirmation_id = :confirmation_id
                 WHERE photo_id = :photo_id
                """
            ),
            {
                "confirmation_id": confirmation_id,
                "photo_id": str(item["photo"].photo_id),
            },
        )
        db.execute(
            text(
                """
                UPDATE shipyard_pallet_units
                   SET status = :outbound, updated_at = NOW()
                 WHERE pallet_id = :pallet_id
                """
            ),
            {
                "outbound": SHIPYARD_PALLET_OUTBOUND,
                "pallet_id": str(item["pallet"].pallet_id),
            },
        )

    db.execute(
        text(
            """
            UPDATE shipyard_cargo_tickets
               SET on_hand_quantity = 0,
                   status = :outbound,
                   updated_at = NOW()
             WHERE ticket_id = :ticket_id
            """
        ),
        {
            "outbound": SHIPYARD_TICKET_OUTBOUND,
            "ticket_id": str(context.ticket_id),
        },
    )
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_tasks
               SET status = :outbound, updated_at = NOW()
             WHERE task_id = :task_id
            """
        ),
        {
            "outbound": SHIPYARD_OUTBOUND_TASK_OUTBOUND,
            "task_id": task_id,
        },
    )
    order_line = db.execute(
        text(
            """
            UPDATE shipyard_outbound_order_lines
               SET fulfilled_quantity = fulfilled_quantity + :quantity,
                   updated_at = NOW()
             WHERE order_line_id = :order_line_id
            RETURNING demand_quantity, allocated_quantity,
                      fulfilled_quantity
            """
        ),
        {
            "quantity": quantity,
            "order_line_id": str(context.order_line_id),
        },
    ).fetchone()
    remaining_tasks = db.execute(
        text(
            """
            SELECT COUNT(*)
              FROM shipyard_outbound_tasks
             WHERE order_line_id = :order_line_id
               AND status = 'PENDING'
            """
        ),
        {"order_line_id": str(context.order_line_id)},
    ).scalar()
    if remaining_tasks:
        line_status = SHIPYARD_OUTBOUND_LINE_ALLOCATED
    elif Decimal(order_line.fulfilled_quantity) >= Decimal(order_line.demand_quantity):
        line_status = SHIPYARD_OUTBOUND_LINE_OUTBOUND
    elif Decimal(order_line.allocated_quantity) < Decimal(order_line.demand_quantity):
        line_status = SHIPYARD_OUTBOUND_LINE_SHORTAGE
    else:
        line_status = SHIPYARD_OUTBOUND_LINE_OUTBOUND
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_order_lines
               SET status = :status, updated_at = NOW()
             WHERE order_line_id = :order_line_id
            """
        ),
        {"status": line_status, "order_line_id": str(context.order_line_id)},
    )
    db.execute(
        text(
            """
            INSERT INTO shipyard_stock_ledger
                (ticket_id, source_type, source_id, quantity_delta,
                 balance_after, warehouse_id, bin_id,
                 location_path_snapshot, actor)
            VALUES
                (:ticket_id, :source_type, :source_id, :quantity_delta,
                 0, :warehouse_id, :bin_id, :location_path_snapshot, :actor)
            """
        ),
        {
            "ticket_id": str(context.ticket_id),
            "source_type": SHIPYARD_LEDGER_OUTBOUND,
            "source_id": confirmation_id,
            "quantity_delta": -quantity,
            "warehouse_id": context.warehouse_id,
            "bin_id": context.assigned_bin_id,
            "location_path_snapshot": context.location_path_snapshot,
            "actor": outbound_by,
        },
    )
    order_status = _refresh_order_status(db, str(context.outbound_order_id))
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_TICKET_OUTBOUND,
        entity_type="shipyard_outbound_task",
        entity_id=0,
        user_id=outbound_by,
        warehouse_id=context.warehouse_id,
        device_id=validated.client_device_id,
        details={
            "task_id": task_id,
            "task_no": context.task_no,
            "outbound_order_id": str(context.outbound_order_id),
            "ticket_id": str(context.ticket_id),
            "confirmation_id": str(confirmation_id),
            "quantity": _decimal_text(quantity),
            "tag_suffixes": sorted(scan_suffixes),
            "location_path_snapshot": context.location_path_snapshot,
            "order_status": order_status,
        },
    )
    db.commit()
    return _confirmation_summary(db, str(confirmation_id))


def confirm_outbound_loading(
    db,
    *,
    outbound_order_id: str,
    validated,
    loaded_by: str,
) -> dict[str, Any]:
    order = db.execute(
        text(
            """
            SELECT outbound_order_id, warehouse_id, outbound_order_no, status
              FROM shipyard_outbound_orders
             WHERE outbound_order_id = :outbound_order_id
             FOR UPDATE
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchone()
    if order is None:
        raise OutboundOrderNotFoundError("正式出库单不存在")
    existing = db.execute(
        text(
            """
            SELECT loading_confirmation_id, idempotency_key
              FROM shipyard_outbound_loading_confirmations
             WHERE outbound_order_id = :outbound_order_id
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchone()
    if existing is not None:
        if existing.idempotency_key != validated.idempotency_key:
            raise OutboundStateError(
                "该出库单已经完成装车确认",
                {"loading_confirmation_id": str(existing.loading_confirmation_id)},
            )
        return get_outbound_order_payload(
            db,
            outbound_order_id=outbound_order_id,
        )
    if order.status != SHIPYARD_OUTBOUND_ORDER_OUTBOUND:
        raise OutboundStateError(
            "只有全部票货完成出库且无缺口的出库单可以确认装车",
            {"status": order.status},
        )
    db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_loading_confirmations
                (outbound_order_id, idempotency_key, notes,
                 client_device_id, loaded_by)
            VALUES
                (:outbound_order_id, :idempotency_key, :notes,
                 :client_device_id, :loaded_by)
            """
        ),
        {
            "outbound_order_id": outbound_order_id,
            "idempotency_key": validated.idempotency_key,
            "notes": validated.notes,
            "client_device_id": validated.client_device_id,
            "loaded_by": loaded_by,
        },
    )
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_orders
               SET status = :status, updated_at = NOW()
             WHERE outbound_order_id = :outbound_order_id
            """
        ),
        {
            "status": SHIPYARD_OUTBOUND_ORDER_LOADED,
            "outbound_order_id": outbound_order_id,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_OUTBOUND_LOADED,
        entity_type="shipyard_outbound_order",
        entity_id=0,
        user_id=loaded_by,
        warehouse_id=order.warehouse_id,
        device_id=validated.client_device_id,
        details={
            "outbound_order_id": outbound_order_id,
            "outbound_order_no": order.outbound_order_no,
            "notes": validated.notes,
        },
    )
    db.commit()
    return get_outbound_order_payload(
        db,
        outbound_order_id=outbound_order_id,
    )
