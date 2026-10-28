"""Dispatch-order matching and print-ready delivery-note generation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_DELIVERY_NOTE_CREATED,
    ACTION_SHIPYARD_DISPATCH_CREATED,
    ACTION_SHIPYARD_DISPATCH_LINE_MATCHED,
    SHIPYARD_DELIVERY_ISSUED,
    SHIPYARD_DISPATCH_CONFIRMED,
    SHIPYARD_DISTRIBUTION_CONFIRMED,
    SHIPYARD_MATCH_AMBIGUOUS,
    SHIPYARD_MATCH_MANUAL,
    SHIPYARD_MATCH_MATCHED,
    SHIPYARD_MATCH_UNMATCHED,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    DeliveryNoteNotFoundError,
    DeliveryNoteStateError,
    DispatchMatchError,
    DispatchNotFoundError,
    DispatchStateError,
    WarehouseNotFoundError,
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _decimal_text(value: Any) -> str:
    return format(Decimal(value), "f")


def _check_warehouse(db, warehouse_id: int) -> None:
    exists = db.execute(
        text(
            """
            SELECT 1
              FROM warehouses
             WHERE warehouse_id = :warehouse_id
               AND is_active = TRUE
            """
        ),
        {"warehouse_id": warehouse_id},
    ).scalar()
    if not exists:
        raise WarehouseNotFoundError("仓库不存在或已停用")


def _normalized_code(value: str) -> str:
    return (value or "").strip().casefold()


def _candidate_payload(row) -> dict[str, Any]:
    return {
        "line_id": str(row.line_id),
        "import_id": str(row.import_id),
        "import_no": row.import_no,
        "source_row_no": row.source_row_no,
        "engineering_no": row.engineering_no,
        "section_no": row.section_no,
        "pallet_no": row.pallet_no,
        "pallet_name": row.pallet_name,
        "work_order_status": row.work_order_status,
        "delivery_status": row.delivery_status,
        "demand_window_raw": row.demand_window_raw,
    }


def _demand_candidates(
    db,
    *,
    warehouse_id: int,
    engineering_no: str,
    pallet_no: str,
):
    return db.execute(
        text(
            """
            SELECT l.line_id, l.import_id, i.import_no, l.source_row_no,
                   l.engineering_no, l.section_no, l.pallet_no,
                   l.pallet_name, l.work_order_status, l.delivery_status,
                   l.demand_window_raw, l.created_at
              FROM shipyard_distribution_lines l
              JOIN shipyard_distribution_imports i
                ON i.import_id = l.import_id
             WHERE i.status = :confirmed
               AND i.warehouse_id = :warehouse_id
               AND LOWER(BTRIM(l.engineering_no)) = :engineering_no
               AND LOWER(BTRIM(l.pallet_no)) = :pallet_no
               AND NOT EXISTS (
                   SELECT 1
                     FROM shipyard_dispatch_lines used
                    WHERE used.demand_line_id = l.line_id
               )
             ORDER BY i.created_at DESC, l.created_at DESC
            """
        ),
        {
            "confirmed": SHIPYARD_DISTRIBUTION_CONFIRMED,
            "warehouse_id": warehouse_id,
            "engineering_no": _normalized_code(engineering_no),
            "pallet_no": _normalized_code(pallet_no),
        },
    ).fetchall()


def _auto_match(
    db,
    *,
    warehouse_id: int,
    engineering_no: str,
    pallet_no: str,
):
    candidates = _demand_candidates(
        db,
        warehouse_id=warehouse_id,
        engineering_no=engineering_no,
        pallet_no=pallet_no,
    )
    if len(candidates) == 1:
        return (
            candidates[0].line_id,
            SHIPYARD_MATCH_MATCHED,
            {"candidate_count": 1},
        )
    if candidates:
        return (
            None,
            SHIPYARD_MATCH_AMBIGUOUS,
            {
                "candidate_count": len(candidates),
                "candidates": [_candidate_payload(row) for row in candidates],
            },
        )
    return (
        None,
        SHIPYARD_MATCH_UNMATCHED,
        {"candidate_count": 0, "candidates": []},
    )


def _manual_candidate(
    db,
    *,
    warehouse_id: int,
    demand_line_id: str,
    engineering_no: str,
    pallet_no: str,
    excluding_dispatch_line_id: str | None = None,
):
    row = db.execute(
        text(
            """
            SELECT l.line_id, l.import_id, i.import_no, l.source_row_no,
                   l.engineering_no, l.section_no, l.pallet_no,
                   l.pallet_name, l.work_order_status, l.delivery_status,
                   l.demand_window_raw, l.created_at
              FROM shipyard_distribution_lines l
              JOIN shipyard_distribution_imports i
                ON i.import_id = l.import_id
             WHERE l.line_id = :line_id
               AND i.status = :confirmed
               AND i.warehouse_id = :warehouse_id
            """
        ),
        {
            "line_id": demand_line_id,
            "confirmed": SHIPYARD_DISTRIBUTION_CONFIRMED,
            "warehouse_id": warehouse_id,
        },
    ).fetchone()
    if row is None:
        raise DispatchMatchError(
            "指定的配送需求行不存在或未确认",
            {"demand_line_id": str(demand_line_id)},
        )
    if _normalized_code(row.engineering_no) != _normalized_code(
        engineering_no
    ) or _normalized_code(row.pallet_no) != _normalized_code(pallet_no):
        raise DispatchMatchError(
            "派工行与指定配送需求的工程号或托盘号不一致",
            {
                "demand_line_id": str(demand_line_id),
                "demand_engineering_no": row.engineering_no,
                "demand_pallet_no": row.pallet_no,
                "dispatch_engineering_no": engineering_no,
                "dispatch_pallet_no": pallet_no,
            },
        )
    used_by = db.execute(
        text(
            """
            SELECT dispatch_line_id
              FROM shipyard_dispatch_lines
             WHERE demand_line_id = :demand_line_id
               AND (
                   :excluding_dispatch_line_id IS NULL
                   OR dispatch_line_id <> :excluding_dispatch_line_id
               )
             LIMIT 1
            """
        ),
        {
            "demand_line_id": demand_line_id,
            "excluding_dispatch_line_id": excluding_dispatch_line_id,
        },
    ).fetchone()
    if used_by is not None:
        raise DispatchMatchError(
            "该配送需求已经关联其他派工明细",
            {
                "demand_line_id": str(demand_line_id),
                "dispatch_line_id": str(used_by.dispatch_line_id),
            },
        )
    return row


def _dispatch_header(db, dispatch_order_id: str, *, for_update: bool = False):
    lock_clause = "FOR UPDATE" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT dispatch_order_id, warehouse_id, dispatch_no,
                   dispatch_date, source_type, source_file_name, status,
                   metadata, idempotency_key, created_by, created_at,
                   updated_at
              FROM shipyard_dispatch_orders
             WHERE dispatch_order_id = :dispatch_order_id
             {lock_clause}
            """  # nosec B608
        ),
        {"dispatch_order_id": dispatch_order_id},
    ).fetchone()


def _dispatch_lines(db, dispatch_order_id: str):
    return db.execute(
        text(
            """
            SELECT dl.dispatch_line_id, dl.dispatch_order_id, dl.line_no,
                   dl.engineering_no, dl.pallet_no, dl.material_spec_raw,
                   dl.material_code, dl.material_name, dl.specification,
                   dl.quantity, dl.unit, dl.expected_arrival_date,
                   dl.keeper_name, dl.keeper_phone, dl.remarks,
                   dl.demand_line_id, dl.match_status, dl.match_details,
                   dl.raw_text, dl.created_at, dl.updated_at,
                   l.section_no AS demand_section_no,
                   l.pallet_name AS demand_pallet_name,
                   l.source_row_no AS demand_source_row_no,
                   i.import_id AS demand_import_id,
                   i.import_no AS demand_import_no
              FROM shipyard_dispatch_lines dl
              LEFT JOIN shipyard_distribution_lines l
                ON l.line_id = dl.demand_line_id
              LEFT JOIN shipyard_distribution_imports i
                ON i.import_id = l.import_id
             WHERE dl.dispatch_order_id = :dispatch_order_id
             ORDER BY dl.line_no
            """
        ),
        {"dispatch_order_id": dispatch_order_id},
    ).fetchall()


def _dispatch_line_payload(row) -> dict[str, Any]:
    return {
        "dispatch_line_id": str(row.dispatch_line_id),
        "line_no": row.line_no,
        "engineering_no": row.engineering_no,
        "pallet_no": row.pallet_no,
        "material_spec_raw": row.material_spec_raw,
        "material_code": row.material_code,
        "material_name": row.material_name,
        "specification": row.specification,
        "quantity": _decimal_text(row.quantity),
        "unit": row.unit,
        "expected_arrival_date": _iso(row.expected_arrival_date),
        "keeper_name": row.keeper_name,
        "keeper_phone": row.keeper_phone,
        "remarks": row.remarks,
        "demand_line_id": str(row.demand_line_id) if row.demand_line_id else None,
        "demand_import_id": (
            str(row.demand_import_id) if row.demand_import_id else None
        ),
        "demand_import_no": row.demand_import_no,
        "demand_section_no": row.demand_section_no,
        "demand_pallet_name": row.demand_pallet_name,
        "demand_source_row_no": row.demand_source_row_no,
        "match_status": row.match_status,
        "match_details": row.match_details,
        "raw_text": row.raw_text,
    }


def get_dispatch_payload(db, *, dispatch_order_id: str) -> dict[str, Any]:
    header = _dispatch_header(db, dispatch_order_id)
    if header is None:
        raise DispatchNotFoundError("派工单不存在")
    lines = _dispatch_lines(db, dispatch_order_id)
    return {
        "dispatch": {
            "dispatch_order_id": str(header.dispatch_order_id),
            "warehouse_id": header.warehouse_id,
            "dispatch_no": header.dispatch_no,
            "dispatch_date": _iso(header.dispatch_date),
            "source_type": header.source_type,
            "source_file_name": header.source_file_name,
            "status": header.status,
            "metadata": header.metadata,
            "created_by": header.created_by,
            "created_at": _iso(header.created_at),
            "updated_at": _iso(header.updated_at),
        },
        "lines": [_dispatch_line_payload(line) for line in lines],
        "match_summary": {
            "matched": sum(
                line.match_status in (SHIPYARD_MATCH_MATCHED, SHIPYARD_MATCH_MANUAL)
                for line in lines
            ),
            "ambiguous": sum(
                line.match_status == SHIPYARD_MATCH_AMBIGUOUS for line in lines
            ),
            "unmatched": sum(
                line.match_status == SHIPYARD_MATCH_UNMATCHED for line in lines
            ),
        },
    }


def _dispatch_request_hash(validated) -> str:
    payload = validated.model_dump(
        mode="json",
        exclude={"idempotency_key"},
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_dispatch_order(db, *, validated, actor: str) -> dict[str, Any]:
    _check_warehouse(db, validated.warehouse_id)
    lock_key = f"{validated.warehouse_id}:dispatch:{validated.idempotency_key}"
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    existing = db.execute(
        text(
            """
            SELECT dispatch_order_id, dispatch_no, metadata
              FROM shipyard_dispatch_orders
             WHERE warehouse_id = :warehouse_id
               AND idempotency_key = :idempotency_key
            """
        ),
        {
            "warehouse_id": validated.warehouse_id,
            "idempotency_key": validated.idempotency_key,
        },
    ).fetchone()
    if existing is not None:
        request_hash = _dispatch_request_hash(validated)
        if existing.dispatch_no != validated.dispatch_no:
            raise DispatchStateError(
                "幂等键已经用于另一张派工单",
                {
                    "dispatch_order_id": str(existing.dispatch_order_id),
                    "dispatch_no": existing.dispatch_no,
                },
            )
        previous_hash = (existing.metadata or {}).get("request_sha256")
        if previous_hash is not None and previous_hash != request_hash:
            raise DispatchStateError(
                "幂等键已经用于内容不同的派工单",
                {
                    "dispatch_order_id": str(existing.dispatch_order_id),
                    "dispatch_no": existing.dispatch_no,
                },
            )
        return get_dispatch_payload(
            db,
            dispatch_order_id=str(existing.dispatch_order_id),
        )

    dispatch_metadata = {"request_sha256": _dispatch_request_hash(validated)}
    header = db.execute(
        text(
            """
            INSERT INTO shipyard_dispatch_orders
                (warehouse_id, dispatch_no, dispatch_date, source_type,
                 source_file_name, status, metadata, idempotency_key,
                 created_by)
            VALUES
                (:warehouse_id, :dispatch_no, :dispatch_date, :source_type,
                 :source_file_name, :status, :metadata, :idempotency_key,
                 :created_by)
            RETURNING dispatch_order_id
            """
        ),
        {
            "warehouse_id": validated.warehouse_id,
            "dispatch_no": validated.dispatch_no,
            "dispatch_date": validated.dispatch_date,
            "source_type": validated.source_type,
            "source_file_name": validated.source_file_name,
            "status": SHIPYARD_DISPATCH_CONFIRMED,
            "metadata": Json(dispatch_metadata),
            "idempotency_key": validated.idempotency_key,
            "created_by": actor,
        },
    ).fetchone()
    dispatch_order_id = header.dispatch_order_id
    for line in validated.lines:
        if line.demand_line_id is not None:
            demand = _manual_candidate(
                db,
                warehouse_id=validated.warehouse_id,
                demand_line_id=str(line.demand_line_id),
                engineering_no=line.engineering_no,
                pallet_no=line.pallet_no,
            )
            demand_line_id = demand.line_id
            match_status = SHIPYARD_MATCH_MANUAL
            match_details = {
                "candidate_count": 1,
                "candidates": [_candidate_payload(demand)],
            }
        else:
            demand_line_id, match_status, match_details = _auto_match(
                db,
                warehouse_id=validated.warehouse_id,
                engineering_no=line.engineering_no,
                pallet_no=line.pallet_no,
            )
        db.execute(
            text(
                """
                INSERT INTO shipyard_dispatch_lines
                    (dispatch_order_id, line_no, engineering_no, pallet_no,
                     material_spec_raw, material_code, material_name,
                     specification, quantity, unit, expected_arrival_date,
                     keeper_name, keeper_phone, remarks, demand_line_id,
                     match_status, match_details)
                VALUES
                    (:dispatch_order_id, :line_no, :engineering_no, :pallet_no,
                     :material_spec_raw, :material_code, :material_name,
                     :specification, :quantity, :unit, :expected_arrival_date,
                     :keeper_name, :keeper_phone, :remarks, :demand_line_id,
                     :match_status, :match_details)
                """
            ),
            {
                "dispatch_order_id": dispatch_order_id,
                "line_no": line.line_no,
                "engineering_no": line.engineering_no,
                "pallet_no": line.pallet_no,
                "material_spec_raw": line.material_spec_raw,
                "material_code": line.material_code,
                "material_name": line.material_name,
                "specification": line.specification,
                "quantity": line.quantity,
                "unit": line.unit,
                "expected_arrival_date": line.expected_arrival_date,
                "keeper_name": line.keeper_name,
                "keeper_phone": line.keeper_phone,
                "remarks": line.remarks,
                "demand_line_id": demand_line_id,
                "match_status": match_status,
                "match_details": Json(match_details),
            },
        )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_DISPATCH_CREATED,
        entity_type="shipyard_dispatch_order",
        entity_id=0,
        user_id=actor,
        warehouse_id=validated.warehouse_id,
        details={
            "dispatch_order_id": str(dispatch_order_id),
            "dispatch_no": validated.dispatch_no,
            "line_count": len(validated.lines),
        },
    )
    db.commit()
    return get_dispatch_payload(db, dispatch_order_id=str(dispatch_order_id))


def match_dispatch_line(
    db,
    *,
    dispatch_line_id: str,
    demand_line_id: str,
    actor: str,
) -> dict[str, Any]:
    line = db.execute(
        text(
            """
            SELECT dl.dispatch_line_id, dl.dispatch_order_id,
                   dl.engineering_no, dl.pallet_no, dl.match_status,
                   d.warehouse_id, d.dispatch_no
              FROM shipyard_dispatch_lines dl
              JOIN shipyard_dispatch_orders d
                ON d.dispatch_order_id = dl.dispatch_order_id
             WHERE dl.dispatch_line_id = :dispatch_line_id
             FOR UPDATE OF dl, d
            """
        ),
        {"dispatch_line_id": dispatch_line_id},
    ).fetchone()
    if line is None:
        raise DispatchNotFoundError("派工明细不存在")
    note_exists = db.execute(
        text(
            """
            SELECT 1 FROM shipyard_delivery_notes
             WHERE dispatch_order_id = :dispatch_order_id
            """
        ),
        {"dispatch_order_id": line.dispatch_order_id},
    ).scalar()
    if note_exists:
        raise DispatchStateError(
            "已经生成发货单，不能再修改派工明细的配送需求匹配",
            {"dispatch_order_id": str(line.dispatch_order_id)},
        )
    demand = _manual_candidate(
        db,
        warehouse_id=line.warehouse_id,
        demand_line_id=demand_line_id,
        engineering_no=line.engineering_no,
        pallet_no=line.pallet_no,
        excluding_dispatch_line_id=dispatch_line_id,
    )
    details = {
        "candidate_count": 1,
        "candidates": [_candidate_payload(demand)],
    }
    db.execute(
        text(
            """
            UPDATE shipyard_dispatch_lines
               SET demand_line_id = :demand_line_id,
                   match_status = :match_status,
                   match_details = :match_details,
                   updated_at = NOW()
             WHERE dispatch_line_id = :dispatch_line_id
            """
        ),
        {
            "dispatch_line_id": dispatch_line_id,
            "demand_line_id": demand_line_id,
            "match_status": SHIPYARD_MATCH_MANUAL,
            "match_details": Json(details),
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_DISPATCH_LINE_MATCHED,
        entity_type="shipyard_dispatch_line",
        entity_id=0,
        user_id=actor,
        warehouse_id=line.warehouse_id,
        details={
            "dispatch_line_id": dispatch_line_id,
            "dispatch_no": line.dispatch_no,
            "demand_line_id": demand_line_id,
            "previous_match_status": line.match_status,
        },
    )
    db.commit()
    return get_dispatch_payload(
        db,
        dispatch_order_id=str(line.dispatch_order_id),
    )


def _delivery_note_header(db, delivery_note_id: str):
    return db.execute(
        text(
            """
            SELECT n.delivery_note_id, n.warehouse_id, n.delivery_note_no,
                   n.dispatch_order_id, n.issue_date, n.status,
                   n.metadata, n.created_by, n.created_at,
                   n.receiver_name, n.received_at,
                   d.dispatch_no, d.dispatch_date, d.source_type,
                   d.source_file_name
              FROM shipyard_delivery_notes n
              JOIN shipyard_dispatch_orders d
                ON d.dispatch_order_id = n.dispatch_order_id
             WHERE n.delivery_note_id = :delivery_note_id
            """
        ),
        {"delivery_note_id": delivery_note_id},
    ).fetchone()


def _delivery_note_lines(db, delivery_note_id: str):
    return db.execute(
        text(
            """
            SELECT delivery_line_id, dispatch_line_id, line_no,
                   engineering_no, section_no, pallet_no,
                   material_code, material_name, specification,
                   material_description, quantity, unit, keeper_name,
                   remarks, demand_line_id, demand_import_no,
                   demand_source_row_no, created_at
              FROM shipyard_delivery_lines
             WHERE delivery_note_id = :delivery_note_id
             ORDER BY line_no
            """
        ),
        {"delivery_note_id": delivery_note_id},
    ).fetchall()


def get_delivery_note_payload(db, *, delivery_note_id: str) -> dict[str, Any]:
    header = _delivery_note_header(db, delivery_note_id)
    if header is None:
        raise DeliveryNoteNotFoundError("发货单不存在")
    lines = _delivery_note_lines(db, delivery_note_id)
    return {
        "delivery_note": {
            "delivery_note_id": str(header.delivery_note_id),
            "warehouse_id": header.warehouse_id,
            "delivery_note_no": header.delivery_note_no,
            "dispatch_order_id": str(header.dispatch_order_id),
            "dispatch_no": header.dispatch_no,
            "dispatch_date": _iso(header.dispatch_date),
            "issue_date": _iso(header.issue_date),
            "source_type": header.source_type,
            "source_file_name": header.source_file_name,
            "status": header.status,
            "metadata": header.metadata,
            "created_by": header.created_by,
            "created_at": _iso(header.created_at),
            "receiver_name": header.receiver_name,
            "received_at": _iso(header.received_at),
            "print_url": (
                f"/api/shipyard/outbound/delivery-notes/{header.delivery_note_id}/print"
            ),
        },
        "lines": [
            {
                "delivery_line_id": str(line.delivery_line_id),
                "dispatch_line_id": str(line.dispatch_line_id),
                "line_no": line.line_no,
                "engineering_no": line.engineering_no,
                "section_no": line.section_no,
                "pallet_no": line.pallet_no,
                "material_code": line.material_code,
                "material_name": line.material_name,
                "specification": line.specification,
                "material_description": line.material_description,
                "quantity": _decimal_text(line.quantity),
                "unit": line.unit,
                "keeper_name": line.keeper_name,
                "remarks": line.remarks,
                "demand_line_id": (
                    str(line.demand_line_id) if line.demand_line_id else None
                ),
                "demand_import_no": line.demand_import_no,
                "demand_source_row_no": line.demand_source_row_no,
            }
            for line in lines
        ],
    }


def create_delivery_note(
    db,
    *,
    dispatch_order_id: str,
    validated,
    actor: str,
) -> dict[str, Any]:
    dispatch = _dispatch_header(db, dispatch_order_id, for_update=True)
    if dispatch is None:
        raise DispatchNotFoundError("派工单不存在")
    if dispatch.status != SHIPYARD_DISPATCH_CONFIRMED:
        raise DispatchStateError(
            "当前派工单状态不能生成发货单",
            {"status": dispatch.status},
        )
    lock_key = f"{dispatch.warehouse_id}:delivery-note:{validated.idempotency_key}"
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    existing_by_key = db.execute(
        text(
            """
            SELECT delivery_note_id, dispatch_order_id,
                   delivery_note_no, issue_date
              FROM shipyard_delivery_notes
             WHERE warehouse_id = :warehouse_id
               AND idempotency_key = :idempotency_key
            """
        ),
        {
            "warehouse_id": dispatch.warehouse_id,
            "idempotency_key": validated.idempotency_key,
        },
    ).fetchone()
    if existing_by_key is not None:
        if str(existing_by_key.dispatch_order_id) != dispatch_order_id:
            raise DeliveryNoteStateError(
                "幂等键已经用于另一张发货单",
                {
                    "delivery_note_id": str(existing_by_key.delivery_note_id),
                    "dispatch_order_id": str(existing_by_key.dispatch_order_id),
                },
            )
        if (
            existing_by_key.delivery_note_no != validated.delivery_note_no
            or existing_by_key.issue_date != validated.issue_date
        ):
            raise DeliveryNoteStateError(
                "幂等键已经用于内容不同的发货单",
                {
                    "delivery_note_id": str(existing_by_key.delivery_note_id),
                    "delivery_note_no": existing_by_key.delivery_note_no,
                },
            )
        return get_delivery_note_payload(
            db,
            delivery_note_id=str(existing_by_key.delivery_note_id),
        )
    existing_for_dispatch = db.execute(
        text(
            """
            SELECT delivery_note_id, delivery_note_no, issue_date
              FROM shipyard_delivery_notes
             WHERE dispatch_order_id = :dispatch_order_id
            """
        ),
        {"dispatch_order_id": dispatch_order_id},
    ).fetchone()
    if existing_for_dispatch is not None:
        if (
            existing_for_dispatch.delivery_note_no != validated.delivery_note_no
            or existing_for_dispatch.issue_date != validated.issue_date
        ):
            raise DeliveryNoteStateError(
                "该派工单已经生成内容不同的发货单",
                {
                    "delivery_note_id": str(existing_for_dispatch.delivery_note_id),
                    "delivery_note_no": existing_for_dispatch.delivery_note_no,
                },
            )
        return get_delivery_note_payload(
            db,
            delivery_note_id=str(existing_for_dispatch.delivery_note_id),
        )

    lines = _dispatch_lines(db, dispatch_order_id)
    if not lines:
        raise DeliveryNoteStateError("派工单没有明细，不能生成发货单")
    note = db.execute(
        text(
            """
            INSERT INTO shipyard_delivery_notes
                (warehouse_id, delivery_note_no, dispatch_order_id,
                 issue_date, status, metadata, idempotency_key, created_by)
            VALUES
                (:warehouse_id, :delivery_note_no, :dispatch_order_id,
                 :issue_date, :status, '{}'::jsonb, :idempotency_key,
                 :created_by)
            RETURNING delivery_note_id
            """
        ),
        {
            "warehouse_id": dispatch.warehouse_id,
            "delivery_note_no": validated.delivery_note_no,
            "dispatch_order_id": dispatch_order_id,
            "issue_date": validated.issue_date,
            "status": SHIPYARD_DELIVERY_ISSUED,
            "idempotency_key": validated.idempotency_key,
            "created_by": actor,
        },
    ).fetchone()
    delivery_note_id = note.delivery_note_id
    for line in lines:
        db.execute(
            text(
                """
                INSERT INTO shipyard_delivery_lines
                    (delivery_note_id, dispatch_line_id, line_no,
                     engineering_no, section_no, pallet_no, material_code,
                     material_name, specification, material_description,
                     quantity, unit, keeper_name, remarks, demand_line_id,
                     demand_import_no, demand_source_row_no)
                VALUES
                    (:delivery_note_id, :dispatch_line_id, :line_no,
                     :engineering_no, :section_no, :pallet_no, :material_code,
                     :material_name, :specification, :material_description,
                     :quantity, :unit, :keeper_name, :remarks,
                     :demand_line_id, :demand_import_no,
                     :demand_source_row_no)
                """
            ),
            {
                "delivery_note_id": delivery_note_id,
                "dispatch_line_id": line.dispatch_line_id,
                "line_no": line.line_no,
                "engineering_no": line.engineering_no,
                "section_no": line.demand_section_no,
                "pallet_no": line.pallet_no,
                "material_code": line.material_code,
                "material_name": line.material_name,
                "specification": line.specification,
                "material_description": line.material_spec_raw,
                "quantity": line.quantity,
                "unit": line.unit,
                "keeper_name": line.keeper_name,
                "remarks": line.remarks,
                "demand_line_id": line.demand_line_id,
                "demand_import_no": line.demand_import_no,
                "demand_source_row_no": line.demand_source_row_no,
            },
        )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_DELIVERY_NOTE_CREATED,
        entity_type="shipyard_delivery_note",
        entity_id=0,
        user_id=actor,
        warehouse_id=dispatch.warehouse_id,
        details={
            "delivery_note_id": str(delivery_note_id),
            "delivery_note_no": validated.delivery_note_no,
            "dispatch_order_id": dispatch_order_id,
            "dispatch_no": dispatch.dispatch_no,
            "line_count": len(lines),
        },
    )
    db.commit()
    return get_delivery_note_payload(
        db,
        delivery_note_id=str(delivery_note_id),
    )


def list_dispatches(
    db,
    *,
    warehouse_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            SELECT d.dispatch_order_id, d.dispatch_no, d.dispatch_date,
                   d.source_type, d.status, d.created_by, d.created_at,
                   COUNT(dl.dispatch_line_id) AS line_count,
                   COUNT(dl.dispatch_line_id) FILTER (
                       WHERE dl.match_status IN ('MATCHED', 'MANUAL')
                   ) AS matched_line_count,
                   COUNT(dl.dispatch_line_id) FILTER (
                       WHERE dl.match_status = 'AMBIGUOUS'
                   ) AS ambiguous_line_count,
                   COUNT(dl.dispatch_line_id) FILTER (
                       WHERE dl.match_status = 'UNMATCHED'
                   ) AS unmatched_line_count
              FROM shipyard_dispatch_orders d
              LEFT JOIN shipyard_dispatch_lines dl
                ON dl.dispatch_order_id = d.dispatch_order_id
             WHERE d.warehouse_id = :warehouse_id
             GROUP BY d.dispatch_order_id
             ORDER BY d.dispatch_date DESC, d.created_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": max(1, min(limit, 200))},
    ).fetchall()
    return {
        "dispatches": [
            {
                "dispatch_order_id": str(row.dispatch_order_id),
                "dispatch_no": row.dispatch_no,
                "dispatch_date": _iso(row.dispatch_date),
                "source_type": row.source_type,
                "status": row.status,
                "line_count": row.line_count,
                "matched_line_count": row.matched_line_count,
                "ambiguous_line_count": row.ambiguous_line_count,
                "unmatched_line_count": row.unmatched_line_count,
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    }


def list_delivery_notes(
    db,
    *,
    warehouse_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            SELECT n.delivery_note_id, n.delivery_note_no, n.issue_date,
                   n.status, n.created_by, n.created_at,
                   d.dispatch_order_id, d.dispatch_no,
                   COUNT(l.delivery_line_id) AS line_count,
                   COALESCE(SUM(l.quantity), 0) AS total_quantity
              FROM shipyard_delivery_notes n
              JOIN shipyard_dispatch_orders d
                ON d.dispatch_order_id = n.dispatch_order_id
              LEFT JOIN shipyard_delivery_lines l
                ON l.delivery_note_id = n.delivery_note_id
             WHERE n.warehouse_id = :warehouse_id
             GROUP BY n.delivery_note_id, d.dispatch_order_id
             ORDER BY n.issue_date DESC, n.created_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": max(1, min(limit, 200))},
    ).fetchall()
    return {
        "delivery_notes": [
            {
                "delivery_note_id": str(row.delivery_note_id),
                "delivery_note_no": row.delivery_note_no,
                "issue_date": _iso(row.issue_date),
                "status": row.status,
                "dispatch_order_id": str(row.dispatch_order_id),
                "dispatch_no": row.dispatch_no,
                "line_count": row.line_count,
                "total_quantity": _decimal_text(row.total_quantity),
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    }
