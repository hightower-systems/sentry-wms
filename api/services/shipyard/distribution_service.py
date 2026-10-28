"""Database orchestration for shipyard distribution-demand imports."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_DISTRIBUTION_CONFIRMED,
    ACTION_SHIPYARD_DISTRIBUTION_IMPORTED,
    ACTION_SHIPYARD_DISTRIBUTION_VOIDED,
    SHIPYARD_DISTRIBUTION_CONFIRMED,
    SHIPYARD_DISTRIBUTION_PARSED,
    SHIPYARD_DISTRIBUTION_VOID,
)
from services.audit_service import write_audit_log
from services.shipyard.distribution_parser import parse_distribution_workbook
from services.shipyard.errors import (
    DistributionImportNotFoundError,
    DistributionImportStateError,
    DuplicateDistributionFileError,
    WarehouseNotFoundError,
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


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


def _import_header(db, import_id: str, *, for_update: bool = False):
    lock_clause = "FOR UPDATE" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT import_id, warehouse_id, import_no, source_file_name,
                   source_file_sha256, source_template_version, status,
                   metadata, created_by, created_at, updated_at
              FROM shipyard_distribution_imports
             WHERE import_id = :import_id
             {lock_clause}
            """  # nosec B608
        ),
        {"import_id": import_id},
    ).fetchone()


def _import_lines(db, import_id: str):
    return db.execute(
        text(
            """
            SELECT line_id, source_row_no, source_row_hash,
                   engineering_no, section_no, pallet_no, pallet_name,
                   work_order_status, delivery_status, applicant_raw,
                   department, delivery_address, contact_name,
                   contact_phone, demand_window_raw, demand_start_at,
                   demand_end_at, resource_1, driver_1, resource_2,
                   driver_2, resource_3, driver_3, confirmer,
                   completed_at_raw, completed_at, confirmation_status,
                   raw_data, created_at
              FROM shipyard_distribution_lines
             WHERE import_id = :import_id
             ORDER BY source_row_no::int
            """
        ),
        {"import_id": import_id},
    ).fetchall()


def _line_payload(row) -> dict[str, Any]:
    return {
        "line_id": str(row.line_id),
        "source_row_no": row.source_row_no,
        "source_row_hash": row.source_row_hash,
        "engineering_no": row.engineering_no,
        "section_no": row.section_no,
        "pallet_no": row.pallet_no,
        "pallet_name": row.pallet_name,
        "work_order_status": row.work_order_status,
        "delivery_status": row.delivery_status,
        "applicant_raw": row.applicant_raw,
        "department": row.department,
        "delivery_address": row.delivery_address,
        "contact_name": row.contact_name,
        "contact_phone": row.contact_phone,
        "demand_window_raw": row.demand_window_raw,
        "demand_start_at": _iso(row.demand_start_at),
        "demand_end_at": _iso(row.demand_end_at),
        "resource_1": row.resource_1,
        "driver_1": row.driver_1,
        "resource_2": row.resource_2,
        "driver_2": row.driver_2,
        "resource_3": row.resource_3,
        "driver_3": row.driver_3,
        "confirmer": row.confirmer,
        "completed_at_raw": row.completed_at_raw,
        "completed_at": _iso(row.completed_at),
        "confirmation_status": row.confirmation_status,
        "raw_data": row.raw_data,
    }


def get_distribution_import_payload(db, *, import_id: str) -> dict[str, Any]:
    header = _import_header(db, import_id)
    if header is None:
        raise DistributionImportNotFoundError("配送需求导入不存在")
    lines = _import_lines(db, import_id)
    warnings = (header.metadata or {}).get("warnings", [])
    return {
        "import": {
            "import_id": str(header.import_id),
            "warehouse_id": header.warehouse_id,
            "import_no": header.import_no,
            "source_file_name": header.source_file_name,
            "source_file_sha256": header.source_file_sha256,
            "source_template_version": header.source_template_version,
            "status": header.status,
            "metadata": header.metadata,
            "original_file_url": (
                f"/api/shipyard/outbound/distribution-imports/{header.import_id}/file"
            ),
            "created_by": header.created_by,
            "created_at": _iso(header.created_at),
            "updated_at": _iso(header.updated_at),
        },
        "lines": [_line_payload(line) for line in lines],
        "warnings": warnings,
    }


def create_distribution_import(
    db,
    *,
    warehouse_id: int,
    source_file_name: str,
    content_type: str,
    content: bytes,
    created_by: str,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    file_hash = hashlib.sha256(content).hexdigest()
    duplicate = db.execute(
        text(
            """
            SELECT import_id
              FROM shipyard_distribution_imports
             WHERE warehouse_id = :warehouse_id
               AND source_file_sha256 = :file_hash
               AND status <> :void_status
             LIMIT 1
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "file_hash": file_hash,
            "void_status": SHIPYARD_DISTRIBUTION_VOID,
        },
    ).fetchone()
    if duplicate is not None:
        raise DuplicateDistributionFileError(
            "同一配送需求文件已经导入",
            {"import_id": str(duplicate.import_id)},
        )

    parsed = parse_distribution_workbook(
        content,
        source_file_name=source_file_name,
    )
    metadata = {
        **parsed.metadata,
        "warnings": list(parsed.warnings),
    }
    import_no = f"DIST-{datetime.now(UTC):%Y%m%d}-{file_hash[:8].upper()}"
    header = db.execute(
        text(
            """
            INSERT INTO shipyard_distribution_imports
                (warehouse_id, import_no, source_file_name,
                 source_file_sha256, source_template_version,
                 status, metadata, created_by)
            VALUES
                (:warehouse_id, :import_no, :source_file_name,
                 :source_file_sha256, :source_template_version,
                 :status, :metadata, :created_by)
            RETURNING import_id
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "import_no": import_no,
            "source_file_name": source_file_name[:255],
            "source_file_sha256": file_hash,
            "source_template_version": parsed.metadata["parser_version"],
            "status": SHIPYARD_DISTRIBUTION_PARSED,
            "metadata": Json(metadata),
            "created_by": created_by,
        },
    ).fetchone()
    import_id = header.import_id
    db.execute(
        text(
            """
            INSERT INTO shipyard_distribution_files
                (import_id, file_name, content_type, file_size,
                 file_sha256, file_bytes)
            VALUES
                (:import_id, :file_name, :content_type, :file_size,
                 :file_sha256, :file_bytes)
            """
        ),
        {
            "import_id": import_id,
            "file_name": source_file_name[:255],
            "content_type": content_type[:128],
            "file_size": len(content),
            "file_sha256": file_hash,
            "file_bytes": content,
        },
    )
    for line in parsed.lines:
        db.execute(
            text(
                """
                INSERT INTO shipyard_distribution_lines
                    (import_id, source_row_no, source_row_hash,
                     engineering_no, section_no, pallet_no, pallet_name,
                     work_order_status, delivery_status, applicant_raw,
                     department, delivery_address, contact_name,
                     contact_phone, demand_window_raw, demand_start_at,
                     demand_end_at, resource_1, driver_1, resource_2,
                     driver_2, resource_3, driver_3, confirmer,
                     completed_at_raw, completed_at, confirmation_status,
                     raw_data)
                VALUES
                    (:import_id, :source_row_no, :source_row_hash,
                     :engineering_no, :section_no, :pallet_no, :pallet_name,
                     :work_order_status, :delivery_status, :applicant_raw,
                     :department, :delivery_address, :contact_name,
                     :contact_phone, :demand_window_raw, :demand_start_at,
                     :demand_end_at, :resource_1, :driver_1, :resource_2,
                     :driver_2, :resource_3, :driver_3, :confirmer,
                     :completed_at_raw, :completed_at, :confirmation_status,
                     :raw_data)
                """
            ),
            {
                "import_id": import_id,
                "source_row_no": line.source_row_no,
                "source_row_hash": line.source_row_hash,
                "engineering_no": line.engineering_no,
                "section_no": line.section_no,
                "pallet_no": line.pallet_no,
                "pallet_name": line.pallet_name,
                "work_order_status": line.work_order_status,
                "delivery_status": line.delivery_status,
                "applicant_raw": line.applicant_raw,
                "department": line.department,
                "delivery_address": line.delivery_address,
                "contact_name": line.contact_name,
                "contact_phone": line.contact_phone,
                "demand_window_raw": line.demand_window_raw,
                "demand_start_at": line.demand_start_at,
                "demand_end_at": line.demand_end_at,
                "resource_1": line.resource_1,
                "driver_1": line.driver_1,
                "resource_2": line.resource_2,
                "driver_2": line.driver_2,
                "resource_3": line.resource_3,
                "driver_3": line.driver_3,
                "confirmer": line.confirmer,
                "completed_at_raw": line.completed_at_raw,
                "completed_at": line.completed_at,
                "confirmation_status": line.confirmation_status,
                "raw_data": Json(line.raw_data),
            },
        )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_DISTRIBUTION_IMPORTED,
        entity_type="shipyard_distribution_import",
        entity_id=0,
        user_id=created_by,
        warehouse_id=warehouse_id,
        details={
            "import_id": str(import_id),
            "import_no": import_no,
            "line_count": len(parsed.lines),
            "source_file_name": source_file_name,
            "source_file_sha256": file_hash,
        },
    )
    db.commit()
    return get_distribution_import_payload(db, import_id=str(import_id))


def confirm_distribution_import(
    db,
    *,
    import_id: str,
    confirmed_by: str,
) -> dict[str, Any]:
    header = _import_header(db, import_id, for_update=True)
    if header is None:
        raise DistributionImportNotFoundError("配送需求导入不存在")
    if header.status != SHIPYARD_DISTRIBUTION_PARSED:
        raise DistributionImportStateError(
            "只有已解析、未确认的配送需求可以确认",
            {"status": header.status},
        )
    db.execute(
        text(
            """
            UPDATE shipyard_distribution_imports
               SET status = :status, updated_at = NOW()
             WHERE import_id = :import_id
            """
        ),
        {
            "status": SHIPYARD_DISTRIBUTION_CONFIRMED,
            "import_id": import_id,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_DISTRIBUTION_CONFIRMED,
        entity_type="shipyard_distribution_import",
        entity_id=0,
        user_id=confirmed_by,
        warehouse_id=header.warehouse_id,
        details={
            "import_id": import_id,
            "import_no": header.import_no,
        },
    )
    db.commit()
    return get_distribution_import_payload(db, import_id=import_id)


def void_distribution_import(
    db,
    *,
    import_id: str,
    reason: str,
    voided_by: str,
) -> dict[str, Any]:
    header = _import_header(db, import_id, for_update=True)
    if header is None:
        raise DistributionImportNotFoundError("配送需求导入不存在")
    if header.status == SHIPYARD_DISTRIBUTION_VOID:
        raise DistributionImportStateError(
            "配送需求导入已经作废",
            {"status": header.status},
        )
    linked_count = db.execute(
        text(
            """
            SELECT COUNT(*)
              FROM shipyard_dispatch_lines dl
              JOIN shipyard_distribution_lines l
                ON l.line_id = dl.demand_line_id
             WHERE l.import_id = :import_id
            """
        ),
        {"import_id": import_id},
    ).scalar_one()
    if linked_count:
        raise DistributionImportStateError(
            "配送需求已经关联派工单，不能作废",
            {
                "import_id": import_id,
                "linked_dispatch_line_count": linked_count,
            },
        )
    metadata = {
        **(header.metadata or {}),
        "void_reason": reason,
        "voided_by": voided_by,
        "voided_at": datetime.now(UTC).isoformat(),
    }
    db.execute(
        text(
            """
            UPDATE shipyard_distribution_imports
               SET status = :status, metadata = :metadata, updated_at = NOW()
             WHERE import_id = :import_id
            """
        ),
        {
            "status": SHIPYARD_DISTRIBUTION_VOID,
            "metadata": Json(metadata),
            "import_id": import_id,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_DISTRIBUTION_VOIDED,
        entity_type="shipyard_distribution_import",
        entity_id=0,
        user_id=voided_by,
        warehouse_id=header.warehouse_id,
        details={
            "import_id": import_id,
            "import_no": header.import_no,
            "reason": reason,
        },
    )
    db.commit()
    return get_distribution_import_payload(db, import_id=import_id)


def get_distribution_file(db, *, import_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT i.warehouse_id, f.file_name, f.content_type,
                   f.file_size, f.file_sha256, f.file_bytes
              FROM shipyard_distribution_imports i
              JOIN shipyard_distribution_files f
                ON f.import_id = i.import_id
             WHERE i.import_id = :import_id
            """
        ),
        {"import_id": import_id},
    ).fetchone()
    if row is None:
        raise DistributionImportNotFoundError("配送需求原始文件不存在")
    return {
        "warehouse_id": row.warehouse_id,
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_sha256": row.file_sha256,
        "file_bytes": bytes(row.file_bytes),
    }


def list_distribution_imports(
    db,
    *,
    warehouse_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            SELECT i.import_id, i.import_no, i.source_file_name, i.status,
                   i.metadata, i.created_by, i.created_at, i.updated_at,
                   COUNT(l.line_id) AS line_count
              FROM shipyard_distribution_imports i
              LEFT JOIN shipyard_distribution_lines l
                ON l.import_id = i.import_id
             WHERE i.warehouse_id = :warehouse_id
             GROUP BY i.import_id
             ORDER BY i.created_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": max(1, min(limit, 200))},
    ).fetchall()
    return {
        "imports": [
            {
                "import_id": str(row.import_id),
                "import_no": row.import_no,
                "source_file_name": row.source_file_name,
                "status": row.status,
                "metadata": row.metadata,
                "line_count": row.line_count,
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ]
    }
