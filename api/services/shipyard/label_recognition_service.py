"""Persistence helpers for photo-based label OCR evidence."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from services.shipyard.errors import LabelRecognitionNotFoundError
from services.shipyard.label_ocr import LabelOcrResult

_REQUIRED_LABEL_FIELDS = {
    "ship_no",
    "engineering_no",
    "production_drawing_no",
    "material_code",
    "item_name",
    "item_spec",
    "batch_no",
    "tag_suffix",
    "manufacturer_name",
    "factory_pallet_no",
    "installation_pallet_no",
    "installation_pallet_name",
    "destination",
    "label_quantity",
}
_DEFAULT_REQUIRED_FIELDS = ("material_code", "item_name", "tag_suffix")


def required_label_fields() -> frozenset[str]:
    raw = os.getenv("SHIPYARD_OCR_REQUIRED_FIELDS", "")
    configured = {
        value.strip()
        for value in raw.split(",")
        if value.strip() in _REQUIRED_LABEL_FIELDS
    }
    return frozenset(configured or _DEFAULT_REQUIRED_FIELDS)


def append_required_field_mismatches(
    mismatches: list[dict[str, Any]],
    scan,
) -> None:
    mismatched_fields = {item["field"] for item in mismatches}
    for field in required_label_fields():
        if field in mismatched_fields or getattr(scan, field, None):
            continue
        mismatches.append(
            {
                "field": field,
                "expected": "吊牌必填字段",
                "actual": None,
                "reason": "REQUIRED",
            }
        )


def save_label_recognition(
    db,
    *,
    photo_id: str,
    ticket_id: str,
    task_id: str | None,
    warehouse_id: int,
    result: LabelOcrResult | None,
    status: str,
    actor: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            INSERT INTO shipyard_label_recognitions
                (photo_id, ticket_id, task_id, warehouse_id, provider,
                 status, raw_text, fields, confidence, error_code,
                 error_message, created_by)
            VALUES
                (:photo_id, :ticket_id, :task_id, :warehouse_id, :provider,
                 :status, :raw_text, :fields, :confidence, :error_code,
                 :error_message, :created_by)
            RETURNING recognition_id, photo_id, ticket_id, task_id,
                      warehouse_id, provider, status, raw_text, fields,
                      confidence, error_code, error_message, created_by,
                      created_at
            """
        ),
        {
            "photo_id": photo_id,
            "ticket_id": ticket_id,
            "task_id": task_id,
            "warehouse_id": warehouse_id,
            "provider": (
                result.provider
                if result
                else (os.getenv("SHIPYARD_OCR_PROVIDER") or "baidu").strip().casefold()
            ),
            "status": status,
            "raw_text": result.raw_text if result else "",
            "fields": Json(result.fields if result else {}),
            "confidence": result.confidence if result else None,
            "error_code": error_code,
            "error_message": error_message,
            "created_by": actor,
        },
    ).fetchone()
    db.commit()
    return label_recognition_payload(row)


def _iso(value):
    return value.isoformat() if value is not None else None


def label_recognition_payload(row) -> dict[str, Any]:
    return {
        "recognition_id": str(row.recognition_id),
        "photo_id": str(row.photo_id),
        "ticket_id": str(row.ticket_id),
        "task_id": str(row.task_id) if row.task_id else None,
        "warehouse_id": row.warehouse_id,
        "provider": row.provider,
        "status": row.status,
        "raw_text": row.raw_text,
        "fields": dict(row.fields or {}),
        "confidence": (float(row.confidence) if row.confidence is not None else None),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
    }


def load_label_recognition(
    db,
    recognition_id: str,
    *,
    ticket_id: str,
    task_id: str | None = None,
    photo_id: str | None = None,
    for_update: bool = False,
):
    lock = "FOR UPDATE" if for_update else ""
    row = db.execute(
        text(
            f"""
            SELECT recognition_id, photo_id, ticket_id, task_id,
                   warehouse_id, provider, status, raw_text, fields,
                   confidence, error_code, error_message, created_by,
                   created_at
              FROM shipyard_label_recognitions
             WHERE recognition_id = :recognition_id
               AND ticket_id = :ticket_id
             {lock}
            """  # nosec B608
        ),
        {
            "recognition_id": str(recognition_id),
            "ticket_id": str(ticket_id),
        },
    ).fetchone()
    if row is None:
        raise LabelRecognitionNotFoundError("吊牌识别记录不存在")
    if task_id is not None and str(row.task_id or "") != str(task_id):
        raise LabelRecognitionNotFoundError("吊牌识别记录不属于当前出库任务")
    if photo_id is not None and str(row.photo_id) != str(photo_id):
        raise LabelRecognitionNotFoundError("吊牌识别记录与照片不一致")
    return row


def fields_scan_view(fields: dict[str, Any], *, suffix: str):
    quantity = fields.get("label_quantity")
    label_quantity = None
    if quantity not in (None, ""):
        try:
            label_quantity = Decimal(str(quantity))
        except (InvalidOperation, ValueError):
            label_quantity = None
    return SimpleNamespace(
        tag_suffix=suffix,
        qr_payload=None,
        qr_pallet_no=fields.get("factory_pallet_no"),
        ship_no=fields.get("ship_no"),
        engineering_no=fields.get("engineering_no"),
        production_drawing_no=fields.get("production_drawing_no"),
        material_code=fields.get("material_code"),
        item_name=fields.get("item_name"),
        item_spec=fields.get("item_spec"),
        batch_no=fields.get("batch_no"),
        manufacturer_name=fields.get("manufacturer_name"),
        installation_pallet_no=fields.get("installation_pallet_no"),
        installation_pallet_name=fields.get("installation_pallet_name"),
        destination=fields.get("destination"),
        label_quantity=label_quantity,
        label_fields=fields,
        label_recognition_id=None,
    )


def recognition_scan_view(recognition, *, suffix: str):
    scan = fields_scan_view(dict(recognition.fields or {}), suffix=suffix)
    scan.label_recognition_id = str(recognition.recognition_id)
    return scan
