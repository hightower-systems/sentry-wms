"""Database orchestration for shipyard inbound manifests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_INBOUND_MANIFEST_CONFIRMED,
    ACTION_SHIPYARD_INBOUND_MANIFEST_PARSED,
    ACTION_SHIPYARD_INBOUND_MANIFEST_VOIDED,
    SHIPYARD_BINDING_AMBIGUOUS,
    SHIPYARD_BINDING_MATCHED,
    SHIPYARD_BINDING_MISMATCH,
    SHIPYARD_BINDING_NOT_CONFIGURED,
    SHIPYARD_LINE_PENDING,
    SHIPYARD_MANIFEST_CONFIRMED,
    SHIPYARD_MANIFEST_PARSED,
    SHIPYARD_MANIFEST_VOID,
    SHIPYARD_STATUS_ACTIVE,
    SHIPYARD_TICKET_EXPECTED,
    SHIPYARD_TICKET_IN_STOCK,
    SHIPYARD_TICKET_OUTBOUND,
    SHIPYARD_TICKET_PARTIAL,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    BindingConfigurationError,
    DuplicateManifestFileError,
    ManifestNotFoundError,
    ManifestStateError,
    ShipyardServiceError,
    WarehouseNotFoundError,
    ZoneConfigurationError,
)
from services.shipyard.manifest_parser import parse_inbound_workbook

_BINDING_FIELD_LABELS = {
    "engineering_no": "工程号",
    "production_drawing_no": "制作图号",
    "manufacturer_name": "厂家",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _decimal_text(value: Any) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _location_path(zone_code: Any, bin_code: Any) -> str:
    parts = [str(value).strip() for value in (zone_code, bin_code) if value]
    return " / ".join(dict.fromkeys(parts))


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _binding_conflicts(line, binding) -> list[dict[str, Any]]:
    conflicts = []
    for field, label in _BINDING_FIELD_LABELS.items():
        manifest_value = getattr(line, field, None)
        binding_value = getattr(binding, field, None)
        if not manifest_value or not binding_value:
            continue
        if _normalized_text(manifest_value) != _normalized_text(binding_value):
            conflicts.append(
                {
                    "field": field,
                    "label": label,
                    "manifest_value": str(manifest_value),
                    "binding_value": str(binding_value),
                }
            )
    return conflicts


def _zone_rows_for_ships(db, warehouse_id: int, ship_nos: list[str]):
    if not ship_nos:
        return {}
    rows = db.execute(
        text(
            """
            SELECT m.mapping_id, m.ship_no, m.bin_id,
                   b.bin_code, b.warehouse_id,
                   z.zone_code, z.zone_name,
                   m.effective_from, m.effective_to
              FROM shipyard_zone_ship_mappings m
              JOIN bins b ON b.bin_id = m.bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
             WHERE m.status = :active_status
               AND m.is_primary = TRUE
               AND m.ship_no = ANY(:ship_nos)
               AND m.effective_from <= NOW()
               AND (m.effective_to IS NULL OR m.effective_to > NOW())
             ORDER BY m.ship_no
            """
        ),
        {
            "ship_nos": ship_nos,
            "active_status": SHIPYARD_STATUS_ACTIVE,
        },
    ).fetchall()
    return {row.ship_no: row for row in rows if row.warehouse_id == warehouse_id}


def _binding_rows_for_ships(db, ship_nos: list[str]):
    if not ship_nos:
        return {}
    rows = db.execute(
        text(
            """
            SELECT binding_id, ship_no, engineering_no,
                   production_drawing_no, manufacturer_name,
                   effective_from, effective_to
              FROM shipyard_project_bindings
             WHERE status = :active_status
               AND ship_no = ANY(:ship_nos)
               AND effective_from <= NOW()
               AND (effective_to IS NULL OR effective_to > NOW())
             ORDER BY ship_no, engineering_no, production_drawing_no,
                      manufacturer_name
            """
        ),
        {
            "ship_nos": ship_nos,
            "active_status": SHIPYARD_STATUS_ACTIVE,
        },
    ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.ship_no, []).append(row)
    return grouped


def _binding_status(line, bindings: dict[str, list[Any]]) -> dict[str, Any]:
    candidates = bindings.get(line.ship_no, [])
    if not candidates:
        return {
            "status": SHIPYARD_BINDING_NOT_CONFIGURED,
            "message": "该船号尚未配置船号/工程号/制作图绑定",
        }

    matches = []
    conflict_sets = {}
    for binding in candidates:
        conflicts = _binding_conflicts(line, binding)
        conflict_sets[binding.binding_id] = conflicts
        if not conflicts:
            matches.append(binding)

    if len(matches) == 1:
        return {
            "status": SHIPYARD_BINDING_MATCHED,
            "binding_id": matches[0].binding_id,
        }
    if len(matches) > 1:
        return {
            "status": SHIPYARD_BINDING_AMBIGUOUS,
            "message": "清单字段匹配到多条船号绑定",
            "binding_ids": [binding.binding_id for binding in matches],
        }
    closest = min(
        candidates, key=lambda binding: len(conflict_sets[binding.binding_id])
    )
    conflicts = conflict_sets[closest.binding_id]
    labels = [item["label"] for item in conflicts]
    return {
        "status": SHIPYARD_BINDING_MISMATCH,
        "message": (
            f"项目绑定字段不一致：{'、'.join(labels)}"
            if labels
            else "清单字段与现有绑定不一致"
        ),
        "mismatched_fields": [item["field"] for item in conflicts],
        "binding_conflicts": conflicts,
    }


def _manifest_header(db, manifest_id: str):
    return db.execute(
        text(
            """
            SELECT manifest_id, warehouse_id, manifest_no, source_type,
                   source_file_name, source_file_sha256,
                   source_template_version, original_file_url, status,
                   metadata, created_by, created_at, updated_at
              FROM shipyard_inbound_manifests
             WHERE manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()


def _manifest_lines(db, manifest_id: str):
    return db.execute(
        text(
            """
            SELECT line_id, source_row_no, source_row_hash, ship_no,
                   engineering_no, production_drawing_no, drawing_name,
                   manufacturer_name, material_code, delivery_note_no, po_no,
                   item_name, item_spec, unit, declared_quantity,
                   assigned_bin_id, raw_data, status, created_at, updated_at
              FROM shipyard_inbound_lines
             WHERE manifest_id = :manifest_id
             ORDER BY COALESCE((raw_data->>'excel_row')::int, 0),
                      source_row_no
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchall()


def get_manifest_payload(db, manifest_id: str) -> dict[str, Any]:
    manifest = _manifest_header(db, manifest_id)
    if manifest is None:
        raise ManifestNotFoundError("入库清单不存在")
    lines = _manifest_lines(db, manifest_id)
    ship_nos = sorted({line.ship_no for line in lines})
    zones = _zone_rows_for_ships(db, manifest.warehouse_id, ship_nos)
    bindings = _binding_rows_for_ships(db, ship_nos)

    missing_zone_ships: list[str] = []
    binding_errors: list[dict[str, Any]] = []
    line_payloads = []
    for line in lines:
        zone = zones.get(line.ship_no)
        if zone is None:
            missing_zone_ships.append(line.ship_no)
        binding_status = _binding_status(line, bindings)
        if binding_status["status"] != SHIPYARD_BINDING_MATCHED:
            binding_errors.append(
                {
                    "source_row_no": line.source_row_no,
                    "ship_no": line.ship_no,
                    "production_drawing_no": line.production_drawing_no,
                    "manufacturer_name": line.manufacturer_name,
                    **binding_status,
                }
            )
        line_payloads.append(
            {
                "line_id": str(line.line_id),
                "source_row_no": line.source_row_no,
                "ship_no": line.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": line.production_drawing_no,
                "drawing_name": line.drawing_name,
                "manufacturer_name": line.manufacturer_name,
                "material_code": line.material_code,
                "delivery_note_no": line.delivery_note_no,
                "po_no": line.po_no,
                "item_name": line.item_name,
                "item_spec": line.item_spec,
                "unit": line.unit,
                "declared_quantity": _decimal_text(line.declared_quantity),
                "assigned_bin_id": line.assigned_bin_id,
                "suggested_bin": (
                    {
                        "bin_id": zone.bin_id,
                        "bin_code": zone.bin_code,
                        "zone_code": zone.zone_code,
                        "zone_name": zone.zone_name,
                        "location_path": _location_path(zone.zone_code, zone.bin_code),
                    }
                    if zone
                    else None
                ),
                "zone_status": "READY" if zone else "MISSING",
                "binding": binding_status,
                "status": line.status,
                "raw_data": line.raw_data,
            }
        )

    tickets = db.execute(
        text(
            """
            SELECT t.ticket_id, t.ticket_no, t.inbound_line_id,
                   t.declared_quantity, t.received_quantity,
                   t.on_hand_quantity, t.assigned_bin_id,
                   t.location_path_snapshot, t.stock_lot_number, t.status
                   , t.shortage_status,
                   (
                       SELECT COUNT(*)
                         FROM shipyard_pallet_units p
                        WHERE p.ticket_id = t.ticket_id
                   ) AS pallet_count
              FROM shipyard_cargo_tickets t
              JOIN shipyard_inbound_lines l
                ON l.line_id = t.inbound_line_id
             WHERE l.manifest_id = :manifest_id
             ORDER BY t.created_at, t.ticket_no
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchall()
    ticket_payloads = [
        {
            "ticket_id": str(ticket.ticket_id),
            "ticket_no": ticket.ticket_no,
            "inbound_line_id": str(ticket.inbound_line_id),
            "declared_quantity": _decimal_text(ticket.declared_quantity),
            "received_quantity": _decimal_text(ticket.received_quantity),
            "on_hand_quantity": _decimal_text(ticket.on_hand_quantity),
            "short_quantity": _decimal_text(
                max(ticket.declared_quantity - ticket.received_quantity, Decimal(0))
            ),
            "assigned_bin_id": ticket.assigned_bin_id,
            "location_path_snapshot": ticket.location_path_snapshot,
            "stock_lot_number": ticket.stock_lot_number,
            "status": ticket.status,
            "shortage_status": ticket.shortage_status,
            "pallet_count": ticket.pallet_count,
        }
        for ticket in tickets
    ]
    received_ticket_count = sum(
        ticket["status"] in (SHIPYARD_TICKET_IN_STOCK, SHIPYARD_TICKET_OUTBOUND)
        for ticket in ticket_payloads
    )
    partial_ticket_count = sum(
        ticket["status"] == SHIPYARD_TICKET_PARTIAL for ticket in ticket_payloads
    )
    pending_ticket_count = sum(
        ticket["status"] == SHIPYARD_TICKET_EXPECTED for ticket in ticket_payloads
    )

    can_confirm = (
        manifest.status == SHIPYARD_MANIFEST_PARSED
        and bool(line_payloads)
        and not missing_zone_ships
        and not binding_errors
    )
    blocking_errors = []
    if missing_zone_ships:
        blocking_errors.append(
            {
                "code": "ZONE_CONFIGURATION_REQUIRED",
                "ship_nos": sorted(set(missing_zone_ships)),
                "message": "以下船号未配置唯一的主入库二级区域",
            }
        )
    if binding_errors:
        blocking_errors.append(
            {
                "code": "BINDING_CONFIGURATION_REQUIRED",
                "lines": binding_errors,
                "message": "存在未配置、冲突或无法唯一匹配的船号/工程号/制作图绑定",
            }
        )

    return {
        "manifest": {
            "manifest_id": str(manifest.manifest_id),
            "warehouse_id": manifest.warehouse_id,
            "manifest_no": manifest.manifest_no,
            "source_type": manifest.source_type,
            "source_file_name": manifest.source_file_name,
            "source_file_sha256": manifest.source_file_sha256,
            "source_template_version": manifest.source_template_version,
            "original_file_url": manifest.original_file_url,
            "status": manifest.status,
            "metadata": manifest.metadata,
            "created_by": manifest.created_by,
            "created_at": _iso(manifest.created_at),
            "updated_at": _iso(manifest.updated_at),
        },
        "lines": line_payloads,
        "tickets": ticket_payloads,
        "validation": {
            "can_confirm": can_confirm,
            "missing_zone_ship_nos": sorted(set(missing_zone_ships)),
            "binding_error_count": len(binding_errors),
            "blocking_errors": blocking_errors,
            "summary": {
                "line_count": len(line_payloads),
                "ship_count": len(ship_nos),
                "total_quantity": format(
                    sum(
                        (line.declared_quantity for line in lines),
                        Decimal(0),
                    ),
                    "f",
                ),
                "received_ticket_count": received_ticket_count,
                "partial_ticket_count": partial_ticket_count,
                "pending_ticket_count": pending_ticket_count,
                "total_received_quantity": _decimal_text(
                    sum(
                        (ticket.received_quantity for ticket in tickets),
                        Decimal(0),
                    )
                ),
                "total_short_quantity": _decimal_text(
                    sum(
                        (
                            max(
                                ticket.declared_quantity - ticket.received_quantity,
                                Decimal(0),
                            )
                            for ticket in tickets
                        ),
                        Decimal(0),
                    )
                ),
            },
        },
    }


def _check_warehouse(db, warehouse_id: int) -> None:
    row = db.execute(
        text(
            """
            SELECT warehouse_id
              FROM warehouses
             WHERE warehouse_id = :warehouse_id
               AND is_active = TRUE
            """
        ),
        {"warehouse_id": warehouse_id},
    ).fetchone()
    if row is None:
        raise WarehouseNotFoundError("仓库不存在或已停用")


def create_manifest_preview(
    db,
    *,
    warehouse_id: int,
    manifest_no: str | None,
    source_file_name: str,
    content_type: str,
    content: bytes,
    manufacturer_name: str | None,
    created_by: str,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    file_hash = hashlib.sha256(content).hexdigest()
    existing = db.execute(
        text(
            """
            SELECT manifest_id
              FROM shipyard_inbound_manifests
             WHERE warehouse_id = :warehouse_id
               AND source_file_sha256 = :file_hash
               AND status <> :void_status
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "file_hash": file_hash,
            "void_status": SHIPYARD_MANIFEST_VOID,
        },
    ).fetchone()
    if existing is not None:
        raise DuplicateManifestFileError(
            "该清单文件已经导入",
            {"manifest_id": str(existing.manifest_id)},
        )

    parsed = parse_inbound_workbook(
        content,
        manufacturer_name=manufacturer_name,
        source_file_name=source_file_name,
    )
    generated_manifest_no = (
        manifest_no or ""
    ).strip() or f"IN-{datetime.now(UTC):%Y%m%d}-{file_hash[:8].upper()}"
    if len(generated_manifest_no) > 128:
        raise ShipyardServiceError("清单编号不能超过 128 个字符")

    conflict = db.execute(
        text(
            """
            SELECT manifest_id
              FROM shipyard_inbound_manifests
             WHERE warehouse_id = :warehouse_id
               AND manifest_no = :manifest_no
            """
        ),
        {"warehouse_id": warehouse_id, "manifest_no": generated_manifest_no},
    ).fetchone()
    if conflict is not None:
        raise ShipyardServiceError(
            "同一仓库内清单编号已存在",
            {"manifest_id": str(conflict.manifest_id)},
        )

    manifest = db.execute(
        text(
            """
            INSERT INTO shipyard_inbound_manifests
                (warehouse_id, manifest_no, source_type, source_file_name,
                 source_file_sha256, source_template_version, metadata,
                 status, created_by)
            VALUES
                (:warehouse_id, :manifest_no, 'EXCEL', :source_file_name,
                 :source_file_sha256, 'shipyard-excel-v1', :metadata,
                 :parsed_status, :created_by)
            RETURNING manifest_id
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "manifest_no": generated_manifest_no,
            "source_file_name": source_file_name[:255],
            "source_file_sha256": file_hash,
            "metadata": Json(parsed.metadata),
            "parsed_status": SHIPYARD_MANIFEST_PARSED,
            "created_by": created_by,
        },
    ).fetchone()
    manifest_id = manifest.manifest_id
    original_file_url = f"/api/shipyard/inbound/manifests/{manifest_id}/file"
    db.execute(
        text(
            """
            INSERT INTO shipyard_manifest_files
                (manifest_id, file_name, content_type, file_size,
                 file_sha256, file_bytes)
            VALUES
                (:manifest_id, :file_name, :content_type, :file_size,
                 :file_sha256, :file_bytes)
            """
        ),
        {
            "manifest_id": manifest_id,
            "file_name": source_file_name[:255],
            "content_type": content_type[:128],
            "file_size": len(content),
            "file_sha256": file_hash,
            "file_bytes": content,
        },
    )
    db.execute(
        text(
            """
            UPDATE shipyard_inbound_manifests
               SET original_file_url = :original_file_url,
                   updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {
            "manifest_id": manifest_id,
            "original_file_url": original_file_url,
        },
    )

    for line in parsed.lines:
        db.execute(
            text(
                """
                INSERT INTO shipyard_inbound_lines
                    (manifest_id, source_row_no, source_row_hash, ship_no,
                     engineering_no, production_drawing_no, drawing_name,
                     manufacturer_name, material_code, delivery_note_no, po_no,
                     item_name,
                     item_spec, unit, declared_quantity, raw_data, status)
                VALUES
                    (:manifest_id, :source_row_no, :source_row_hash, :ship_no,
                     :engineering_no, :production_drawing_no, :drawing_name,
                     :manufacturer_name, :material_code, :delivery_note_no,
                     :po_no, :item_name,
                     :item_spec, :unit, :declared_quantity, :raw_data,
                     :pending_status)
                """
            ),
            {
                "manifest_id": manifest_id,
                "source_row_no": line.source_row_no,
                "source_row_hash": line.source_row_hash,
                "ship_no": line.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": line.production_drawing_no,
                "drawing_name": line.drawing_name,
                "manufacturer_name": line.manufacturer_name,
                "material_code": line.material_code,
                "delivery_note_no": line.delivery_note_no,
                "po_no": line.po_no,
                "item_name": line.item_name,
                "item_spec": line.item_spec,
                "unit": line.unit,
                "declared_quantity": line.declared_quantity,
                "raw_data": Json(line.raw_data),
                "pending_status": SHIPYARD_LINE_PENDING,
            },
        )

    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_INBOUND_MANIFEST_PARSED,
        entity_type="shipyard_inbound_manifest",
        entity_id=0,
        user_id=created_by,
        warehouse_id=warehouse_id,
        details={
            "manifest_id": str(manifest_id),
            "manifest_no": generated_manifest_no,
            "source_file_sha256": file_hash,
            "line_count": len(parsed.lines),
        },
    )
    db.commit()
    return get_manifest_payload(db, str(manifest_id))


def confirm_manifest(db, *, manifest_id: str, confirmed_by: str) -> dict[str, Any]:
    manifest = db.execute(
        text(
            """
            SELECT manifest_id, warehouse_id, manifest_no, status
              FROM shipyard_inbound_manifests
             WHERE manifest_id = :manifest_id
             FOR UPDATE
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()
    if manifest is None:
        raise ManifestNotFoundError("入库清单不存在")
    if manifest.status != SHIPYARD_MANIFEST_PARSED:
        raise ManifestStateError(
            "只有已解析、未确认的入库清单可以确认",
            {"status": manifest.status},
        )

    lines = db.execute(
        text(
            """
            SELECT line_id, source_row_no, ship_no, engineering_no,
                   production_drawing_no, manufacturer_name,
                   item_name, declared_quantity, assigned_bin_id
              FROM shipyard_inbound_lines
             WHERE manifest_id = :manifest_id
             ORDER BY source_row_no
             FOR UPDATE
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchall()
    if not lines:
        raise ShipyardServiceError("入库清单没有明细行")

    ship_nos = sorted({line.ship_no for line in lines})
    bindings = _binding_rows_for_ships(db, ship_nos)
    binding_errors = []
    binding_ids_by_line: dict[Any, int] = {}
    for line in lines:
        status = _binding_status(line, bindings)
        if status["status"] != SHIPYARD_BINDING_MATCHED:
            binding_errors.append(
                {
                    "source_row_no": line.source_row_no,
                    "ship_no": line.ship_no,
                    "production_drawing_no": line.production_drawing_no,
                    "manufacturer_name": line.manufacturer_name,
                    **status,
                }
            )
        else:
            binding_ids_by_line[line.line_id] = status["binding_id"]
    if binding_errors:
        raise BindingConfigurationError(
            "船号/工程号/制作图绑定未配置、冲突或不唯一，不能确认清单",
            {"lines": binding_errors},
        )

    zones = _zone_rows_for_ships(db, manifest.warehouse_id, ship_nos)
    missing = [ship_no for ship_no in ship_nos if ship_no not in zones]
    if missing:
        raise ZoneConfigurationError(
            "船号未配置唯一主入库区域，不能确认清单",
            {"ship_nos": missing},
        )

    created_tickets = []
    used_ticket_nos: set[str] = set()
    for line in lines:
        zone = zones[line.ship_no]
        while True:
            ticket_suffix = uuid.uuid4().hex[:12].upper()
            ticket_no = f"SY-{datetime.now(UTC):%Y%m%d}-{ticket_suffix}"
            if ticket_no not in used_ticket_nos:
                used_ticket_nos.add(ticket_no)
                break
        stock_lot_number = str(uuid.uuid4())
        location_path = _location_path(zone.zone_code, zone.bin_code)
        ticket = db.execute(
            text(
                """
                INSERT INTO shipyard_cargo_tickets
                    (ticket_no, inbound_line_id, declared_quantity,
                     received_quantity, assigned_bin_id,
                     location_path_snapshot, stock_lot_number, status,
                     created_by)
                VALUES
                    (:ticket_no, :inbound_line_id, :declared_quantity,
                     0, :assigned_bin_id, :location_path_snapshot,
                     :stock_lot_number, :ticket_status, :created_by)
                RETURNING ticket_id, ticket_no
                """
            ),
            {
                "ticket_no": ticket_no,
                "inbound_line_id": line.line_id,
                "declared_quantity": line.declared_quantity,
                "assigned_bin_id": zone.bin_id,
                "location_path_snapshot": location_path,
                "stock_lot_number": stock_lot_number,
                "ticket_status": SHIPYARD_TICKET_EXPECTED,
                "created_by": confirmed_by,
            },
        ).fetchone()
        db.execute(
            text(
                """
                UPDATE shipyard_inbound_lines
                   SET assigned_bin_id = :bin_id,
                       project_binding_id = :project_binding_id,
                       updated_at = NOW()
                 WHERE line_id = :line_id
                """
            ),
            {
                "bin_id": zone.bin_id,
                "project_binding_id": binding_ids_by_line[line.line_id],
                "line_id": line.line_id,
            },
        )
        created_tickets.append(
            {
                "ticket_id": str(ticket.ticket_id),
                "ticket_no": ticket.ticket_no,
                "source_row_no": line.source_row_no,
                "ship_no": line.ship_no,
                "assigned_bin_id": zone.bin_id,
                "location_path_snapshot": location_path,
            }
        )

    db.execute(
        text(
            """
            UPDATE shipyard_inbound_manifests
               SET status = :confirmed_status,
                   updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {
            "manifest_id": manifest_id,
            "confirmed_status": SHIPYARD_MANIFEST_CONFIRMED,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_INBOUND_MANIFEST_CONFIRMED,
        entity_type="shipyard_inbound_manifest",
        entity_id=0,
        user_id=confirmed_by,
        warehouse_id=manifest.warehouse_id,
        details={
            "manifest_id": manifest_id,
            "manifest_no": manifest.manifest_no,
            "ticket_count": len(created_tickets),
        },
    )
    db.commit()
    payload = get_manifest_payload(db, manifest_id)
    payload["confirmed_tickets"] = created_tickets
    return payload


def void_manifest(
    db,
    *,
    manifest_id: str,
    reason: str,
    voided_by: str,
) -> dict[str, Any]:
    manifest = db.execute(
        text(
            """
            SELECT manifest_id, warehouse_id, manifest_no, status
              FROM shipyard_inbound_manifests
             WHERE manifest_id = :manifest_id
             FOR UPDATE
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()
    if manifest is None:
        raise ManifestNotFoundError("入库清单不存在")
    if manifest.status != SHIPYARD_MANIFEST_PARSED:
        raise ManifestStateError(
            "只有尚未确认的入库清单可以废弃",
            {"status": manifest.status},
        )

    db.execute(
        text(
            """
            UPDATE shipyard_inbound_manifests
               SET status = :void_status,
                   metadata = metadata || :void_metadata,
                   updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {
            "manifest_id": manifest_id,
            "void_status": SHIPYARD_MANIFEST_VOID,
            "void_metadata": Json(
                {
                    "void_reason": reason,
                    "voided_by": voided_by,
                    "voided_at": datetime.now(UTC).isoformat(),
                }
            ),
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_INBOUND_MANIFEST_VOIDED,
        entity_type="shipyard_inbound_manifest",
        entity_id=0,
        user_id=voided_by,
        warehouse_id=manifest.warehouse_id,
        details={
            "manifest_id": manifest_id,
            "manifest_no": manifest.manifest_no,
            "reason": reason,
        },
    )
    db.commit()
    return get_manifest_payload(db, manifest_id)


def get_manifest_file(db, *, manifest_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT m.warehouse_id, f.file_name, f.content_type,
                   f.file_size, f.file_sha256, f.file_bytes
              FROM shipyard_inbound_manifests m
              JOIN shipyard_manifest_files f
                ON f.manifest_id = m.manifest_id
             WHERE m.manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()
    if row is None:
        raise ManifestNotFoundError("入库清单原始文件不存在")
    return {
        "warehouse_id": row.warehouse_id,
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_sha256": row.file_sha256,
        "file_bytes": bytes(row.file_bytes),
    }


def list_manifests(db, *, warehouse_id: int, limit: int = 50) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            SELECT m.manifest_id, m.manifest_no, m.source_file_name,
                   m.original_file_url, m.status, m.metadata, m.created_by,
                   m.created_at, m.updated_at,
                   COUNT(l.line_id) AS line_count,
                   COALESCE(SUM(l.declared_quantity), 0) AS total_quantity
              FROM shipyard_inbound_manifests m
              LEFT JOIN shipyard_inbound_lines l
                ON l.manifest_id = m.manifest_id
             WHERE m.warehouse_id = :warehouse_id
             GROUP BY m.manifest_id
             ORDER BY m.created_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": min(max(limit, 1), 200)},
    ).fetchall()
    return {
        "manifests": [
            {
                "manifest_id": str(row.manifest_id),
                "manifest_no": row.manifest_no,
                "source_file_name": row.source_file_name,
                "original_file_url": row.original_file_url,
                "status": row.status,
                "metadata": row.metadata,
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "line_count": row.line_count,
                "total_quantity": _decimal_text(row.total_quantity),
            }
            for row in rows
        ]
    }
