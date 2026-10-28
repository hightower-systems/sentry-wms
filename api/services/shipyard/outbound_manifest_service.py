"""Formal outbound manifest import, stock matching and order allocation."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED,
    ACTION_SHIPYARD_OUTBOUND_MANIFEST_PARSED,
    ACTION_SHIPYARD_OUTBOUND_MANIFEST_VOIDED,
    SHIPYARD_BINDING_AMBIGUOUS,
    SHIPYARD_BINDING_MATCHED,
    SHIPYARD_BINDING_MISMATCH,
    SHIPYARD_BINDING_NOT_CONFIGURED,
    SHIPYARD_OUTBOUND_LINE_ALLOCATED,
    SHIPYARD_OUTBOUND_LINE_PENDING,
    SHIPYARD_OUTBOUND_LINE_SHORTAGE,
    SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED,
    SHIPYARD_OUTBOUND_MANIFEST_PARSED,
    SHIPYARD_OUTBOUND_MANIFEST_VOID,
    SHIPYARD_OUTBOUND_ORDER_PENDING,
    SHIPYARD_OUTBOUND_TASK_OUTBOUND,
    SHIPYARD_OUTBOUND_TASK_PENDING,
    SHIPYARD_STATUS_ACTIVE,
    SHIPYARD_TICKET_ALLOCATED,
    SHIPYARD_TICKET_IN_STOCK,
    SHIPYARD_TICKET_PARTIAL,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    BindingConfigurationError,
    DuplicateOutboundManifestFileError,
    OutboundManifestNotFoundError,
    OutboundManifestStateError,
    OutboundOrderNotFoundError,
    WarehouseNotFoundError,
)
from services.shipyard.outbound_manifest_parser import parse_outbound_workbook

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
        return "0.000"
    return format(Decimal(value), "f")


def _normalized(value: str | None) -> str:
    return (value or "").strip().casefold()


def _binding_conflicts(line, binding) -> list[dict[str, Any]]:
    conflicts = []
    for field, label in _BINDING_FIELD_LABELS.items():
        manifest_value = getattr(line, field, None)
        binding_value = getattr(binding, field, None)
        if field == "manufacturer_name" and (not manifest_value or not binding_value):
            continue
        if _normalized(str(manifest_value)) != _normalized(str(binding_value)):
            conflicts.append(
                {
                    "field": field,
                    "label": label,
                    "manifest_value": str(manifest_value),
                    "binding_value": str(binding_value),
                }
            )
    return conflicts


def _location_path(zone_code: Any, bin_code: Any) -> str:
    return " / ".join(
        dict.fromkeys(
            str(value).strip()
            for value in (zone_code, bin_code)
            if value and str(value).strip()
        )
    )


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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _manifest_header(db, manifest_id: str, *, for_update: bool = False):
    lock = "FOR UPDATE" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT manifest_id, warehouse_id, manifest_no,
                   factory_shipment_no, source_file_name,
                   source_file_sha256, source_template_version, status,
                   metadata, created_by, created_at, updated_at
              FROM shipyard_outbound_manifests
             WHERE manifest_id = :manifest_id
             {lock}
            """  # nosec B608
        ),
        {"manifest_id": manifest_id},
    ).fetchone()


def _manifest_lines(db, manifest_id: str):
    return db.execute(
        text(
            """
            SELECT line_id, manifest_id, source_row_no, ticket_no, ship_no,
                   engineering_no, production_drawing_no, manufacturer_name,
                   material_code, item_name, item_spec, demand_quantity,
                   unit, demand_date, expected_pallet_count,
                   expected_tag_suffixes, project_binding_id, status,
                   raw_data, created_at, updated_at
              FROM shipyard_outbound_manifest_lines
             WHERE manifest_id = :manifest_id
             ORDER BY COALESCE((raw_data->>'excel_row')::int, 0),
                      source_row_no
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchall()


def _bindings_for_ships(db, ship_nos: list[str]):
    if not ship_nos:
        return {}
    rows = db.execute(
        text(
            """
            SELECT binding_id, ship_no, engineering_no,
                   production_drawing_no, manufacturer_name
              FROM shipyard_project_bindings
             WHERE status = :active
               AND ship_no = ANY(:ship_nos)
               AND effective_from <= NOW()
               AND (effective_to IS NULL OR effective_to > NOW())
             ORDER BY ship_no, engineering_no, production_drawing_no,
                      manufacturer_name
            """
        ),
        {"active": SHIPYARD_STATUS_ACTIVE, "ship_nos": ship_nos},
    ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.ship_no, []).append(row)
    return grouped


def _binding_for_line(line, bindings: dict[str, list[Any]]) -> dict[str, Any]:
    candidates = bindings.get(line.ship_no, [])
    if not candidates:
        return {
            "status": SHIPYARD_BINDING_NOT_CONFIGURED,
            "message": "该船号尚未配置生效的项目绑定",
        }
    conflict_sets = {
        binding.binding_id: _binding_conflicts(line, binding) for binding in candidates
    }
    matches = [
        binding for binding in candidates if not conflict_sets[binding.binding_id]
    ]
    if len(matches) == 1:
        return {
            "status": SHIPYARD_BINDING_MATCHED,
            "binding_id": matches[0].binding_id,
            "manufacturer_name": matches[0].manufacturer_name,
        }
    if matches:
        return {
            "status": SHIPYARD_BINDING_AMBIGUOUS,
            "message": "清单字段匹配到多条项目绑定",
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
            else "船号、工程号或制作图与项目绑定不一致"
        ),
        "mismatched_fields": [item["field"] for item in conflicts],
        "binding_conflicts": conflicts,
    }


def _inventory_rows(
    db,
    *,
    warehouse_id: int,
    line,
    manufacturer_name: str,
    for_update: bool = False,
):
    lock = "FOR UPDATE OF t" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT t.ticket_id, t.ticket_no, t.declared_quantity,
                   t.received_quantity, t.on_hand_quantity,
                   t.expected_pallet_count, t.assigned_bin_id,
                   t.location_path_snapshot, t.stock_lot_number,
                   t.status, t.created_at,
                   l.line_id AS inbound_line_id, l.ship_no,
                   COALESCE(NULLIF(l.engineering_no, ''), pb.engineering_no, '')
                       AS engineering_no,
                   COALESCE(
                       NULLIF(l.production_drawing_no, ''),
                       pb.production_drawing_no,
                       ''
                   ) AS production_drawing_no,
                   COALESCE(
                       NULLIF(l.manufacturer_name, ''),
                       pb.manufacturer_name,
                       ''
                   ) AS manufacturer_name,
                   l.material_code, l.item_name,
                   l.item_spec, l.unit,
                   b.bin_code, z.zone_code, z.zone_name
              FROM shipyard_cargo_tickets t
              JOIN shipyard_inbound_lines l
                ON l.line_id = t.inbound_line_id
              JOIN shipyard_inbound_manifests im
                ON im.manifest_id = l.manifest_id
              JOIN bins b ON b.bin_id = t.assigned_bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
              LEFT JOIN shipyard_project_bindings pb
                ON pb.binding_id = l.project_binding_id
             WHERE im.warehouse_id = :warehouse_id
               AND t.status IN (:in_stock, :partial)
               AND t.on_hand_quantity > 0
               AND LOWER(BTRIM(COALESCE(l.material_code, '')))
                   = :material_code
               AND LOWER(BTRIM(l.item_name)) = :item_name
               AND LOWER(BTRIM(COALESCE(l.item_spec, '')))
                   = :item_spec
               AND LOWER(BTRIM(l.ship_no)) = :ship_no
               AND LOWER(BTRIM(COALESCE(
                       NULLIF(l.engineering_no, ''),
                       pb.engineering_no,
                       ''
                   )))
                   = :engineering_no
               AND LOWER(BTRIM(COALESCE(
                       NULLIF(l.production_drawing_no, ''),
                       pb.production_drawing_no,
                       ''
                   )))
                   = :production_drawing_no
               AND (
                   :manufacturer_name = ''
                   OR LOWER(BTRIM(COALESCE(
                           NULLIF(l.manufacturer_name, ''),
                           pb.manufacturer_name,
                           ''
                       )))
                       = :manufacturer_name
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM shipyard_outbound_tasks ot
                    WHERE ot.ticket_id = t.ticket_id
               )
             ORDER BY t.created_at, t.ticket_no
             {lock}
            """  # nosec B608
        ),
        {
            "warehouse_id": warehouse_id,
            "in_stock": SHIPYARD_TICKET_IN_STOCK,
            "partial": SHIPYARD_TICKET_PARTIAL,
            "material_code": _normalized(line.material_code),
            "item_name": _normalized(line.item_name),
            "item_spec": _normalized(line.item_spec),
            "ship_no": _normalized(line.ship_no),
            "engineering_no": _normalized(line.engineering_no),
            "production_drawing_no": _normalized(line.production_drawing_no),
            "manufacturer_name": _normalized(manufacturer_name),
        },
    ).fetchall()


def _task_pallets(db, ticket_id: str):
    return db.execute(
        text(
            """
            SELECT pallet_id, tag_suffix, qr_payload, qr_payload_sha256,
                   factory_pallet_no, installation_pallet_no, batch_no, status
              FROM shipyard_pallet_units
             WHERE ticket_id = :ticket_id
             ORDER BY tag_suffix NULLS FIRST, pallet_id
            """
        ),
        {"ticket_id": ticket_id},
    ).fetchall()


def _candidate_from_row(db, row, line) -> dict[str, Any]:
    pallets = _task_pallets(db, str(row.ticket_id))
    suffixes = [pallet.tag_suffix for pallet in pallets]
    suffix_set = set(suffixes)
    blocking_reason = None
    if not pallets:
        blocking_reason = "票货没有已登记的托盘吊牌"
    elif any(not suffix for suffix in suffixes):
        blocking_reason = "票货存在无托盘后缀的吊牌，不能执行整票出库"
    elif len(suffix_set) != len(suffixes):
        blocking_reason = "票货存在重复托盘后缀"
    elif any(pallet.status != "STORED" for pallet in pallets):
        blocking_reason = "票货存在非在库托盘"
    elif (
        line.expected_pallet_count is not None
        and len(pallets) != line.expected_pallet_count
    ):
        blocking_reason = "库存托盘数与出库清单预计托盘数不一致"
    elif line.expected_tag_suffixes and suffix_set != set(line.expected_tag_suffixes):
        blocking_reason = "库存托盘后缀与出库清单不一致"
    elif (
        row.expected_pallet_count is not None
        and len(pallets) != row.expected_pallet_count
    ):
        blocking_reason = "库存托盘数与入库票货预计托盘数不一致"
    return {
        "ticket_id": str(row.ticket_id),
        "ticket_no": row.ticket_no,
        "material_code": row.material_code,
        "item_name": row.item_name,
        "item_spec": row.item_spec,
        "manufacturer_name": row.manufacturer_name,
        "available_quantity": _decimal_text(row.on_hand_quantity),
        "declared_quantity": _decimal_text(row.declared_quantity),
        "received_quantity": _decimal_text(row.received_quantity),
        "assigned_bin_id": row.assigned_bin_id,
        "bin_code": row.bin_code,
        "zone_code": row.zone_code,
        "zone_name": row.zone_name,
        "location_path_snapshot": row.location_path_snapshot,
        "stock_lot_number": row.stock_lot_number,
        "expected_pallet_count": (
            line.expected_pallet_count or row.expected_pallet_count or len(pallets)
        ),
        "tag_suffixes": suffixes,
        "pallets": [
            {
                "pallet_id": str(pallet.pallet_id),
                "tag_suffix": pallet.tag_suffix,
                "qr_payload_sha256": pallet.qr_payload_sha256,
                "factory_pallet_no": pallet.factory_pallet_no,
                "installation_pallet_no": pallet.installation_pallet_no,
                "batch_no": pallet.batch_no,
                "status": pallet.status,
            }
            for pallet in pallets
        ],
        "eligible": blocking_reason is None,
        "blocking_reason": blocking_reason,
        "_created_at": row.created_at,
        "_quantity": Decimal(row.on_hand_quantity),
    }


def _allocate_line_tasks(
    db,
    *,
    warehouse_id: int,
    line,
    binding: dict[str, Any],
    used_ticket_ids: set[str],
    for_update: bool = False,
) -> dict[str, Any]:
    if binding["status"] != SHIPYARD_BINDING_MATCHED:
        return {
            "tasks": [],
            "allocated_quantity": Decimal(0),
            "shortage_quantity": Decimal(line.demand_quantity),
            "diagnostics": [
                {
                    "code": "BINDING_NOT_MATCHED",
                    "message": binding["message"],
                }
            ],
            "candidates": [],
            "binding": binding,
        }

    manufacturer = line.manufacturer_name or binding["manufacturer_name"] or ""
    rows = _inventory_rows(
        db,
        warehouse_id=warehouse_id,
        line=line,
        manufacturer_name=manufacturer,
        for_update=for_update,
    )
    candidates = []
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        ticket_id = str(row.ticket_id)
        if ticket_id in used_ticket_ids:
            continue
        candidate = _candidate_from_row(db, row, line)
        candidates.append(candidate)
        if not candidate["eligible"]:
            diagnostics.append(
                {
                    "code": "TICKET_NOT_OUTBOUND_READY",
                    "ticket_id": ticket_id,
                    "ticket_no": candidate["ticket_no"],
                    "message": candidate["blocking_reason"],
                }
            )

    remaining = Decimal(line.demand_quantity)
    selected: list[dict[str, Any]] = []
    for candidate in sorted(
        (item for item in candidates if item["eligible"]),
        key=lambda item: (item["_created_at"], item["ticket_no"]),
    ):
        quantity = candidate["_quantity"]
        ticket_id = candidate["ticket_id"]
        if ticket_id in used_ticket_ids:
            continue
        if quantity > remaining:
            diagnostics.append(
                {
                    "code": "WHOLE_TICKET_EXCEEDS_REMAINING_DEMAND",
                    "ticket_id": ticket_id,
                    "ticket_no": candidate["ticket_no"],
                    "ticket_quantity": _decimal_text(quantity),
                    "remaining_quantity": _decimal_text(remaining),
                    "message": "整票数量大于剩余需求，系统不会自动拆票",
                }
            )
            continue
        selected.append(candidate)
        used_ticket_ids.add(ticket_id)
        remaining -= quantity
        if remaining == 0:
            break

    allocated = sum(
        (candidate["_quantity"] for candidate in selected),
        Decimal(0),
    )
    return {
        "tasks": selected,
        "allocated_quantity": allocated,
        "shortage_quantity": max(remaining, Decimal(0)),
        "diagnostics": diagnostics,
        "candidates": candidates,
        "binding": {
            **binding,
            "manufacturer_name": manufacturer,
        },
    }


def _existing_order_summary(db, manifest_id: str):
    return db.execute(
        text(
            """
            SELECT outbound_order_id, outbound_order_no, status,
                   confirmed_by, confirmed_at
              FROM shipyard_outbound_orders
             WHERE manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()


def get_outbound_manifest_payload(db, *, manifest_id: str) -> dict[str, Any]:
    manifest = _manifest_header(db, manifest_id)
    if manifest is None:
        raise OutboundManifestNotFoundError("正式出库清单不存在")
    lines = _manifest_lines(db, manifest_id)
    bindings = _bindings_for_ships(
        db,
        sorted({line.ship_no for line in lines}),
    )
    order = _existing_order_summary(db, manifest_id)
    allocated_ticket_ids: set[str] = set()
    if order is not None:
        allocated_ticket_ids = set(
            db.execute(
                text(
                    """
                    SELECT ticket_id::text
                      FROM shipyard_outbound_tasks
                     WHERE outbound_order_id = :order_id
                    """
                ),
                {"order_id": str(order.outbound_order_id)},
            )
            .scalars()
            .all()
        )

    line_payloads: list[dict[str, Any]] = []
    binding_errors: list[dict[str, Any]] = []
    for line in lines:
        binding = _binding_for_line(line, bindings)
        if binding["status"] != SHIPYARD_BINDING_MATCHED:
            binding_errors.append(
                {
                    "source_row_no": line.source_row_no,
                    "ship_no": line.ship_no,
                    "engineering_no": line.engineering_no,
                    "production_drawing_no": line.production_drawing_no,
                    **binding,
                }
            )
        allocation = _allocate_line_tasks(
            db,
            warehouse_id=manifest.warehouse_id,
            line=line,
            binding=binding,
            used_ticket_ids=allocated_ticket_ids,
            for_update=False,
        )
        tasks = allocation["tasks"]
        line_payloads.append(
            {
                "line_id": str(line.line_id),
                "source_row_no": line.source_row_no,
                "ticket_no": line.ticket_no,
                "ship_no": line.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": line.production_drawing_no,
                "manufacturer_name": allocation["binding"].get(
                    "manufacturer_name",
                    line.manufacturer_name,
                ),
                "material_code": line.material_code,
                "item_name": line.item_name,
                "item_spec": line.item_spec,
                "demand_quantity": _decimal_text(line.demand_quantity),
                "unit": line.unit,
                "demand_date": _iso(line.demand_date),
                "expected_pallet_count": line.expected_pallet_count,
                "expected_tag_suffixes": list(line.expected_tag_suffixes),
                "binding": allocation["binding"],
                "allocated_quantity": _decimal_text(allocation["allocated_quantity"]),
                "shortage_quantity": _decimal_text(allocation["shortage_quantity"]),
                "match_status": (
                    "READY"
                    if allocation["shortage_quantity"] == 0
                    else "SHORTAGE"
                    if allocation["allocated_quantity"] == 0
                    else "PARTIAL"
                ),
                "tasks": [
                    {
                        key: value
                        for key, value in task.items()
                        if not key.startswith("_")
                    }
                    for task in tasks
                ],
                "candidates": [
                    {
                        key: value
                        for key, value in candidate.items()
                        if not key.startswith("_")
                    }
                    for candidate in allocation["candidates"]
                ],
                "diagnostics": allocation["diagnostics"],
                "status": line.status,
                "raw_data": line.raw_data,
            }
        )

    if order is not None:
        confirmed_order = get_outbound_order_payload(
            db,
            outbound_order_id=str(order.outbound_order_id),
        )
        tasks_by_source: dict[str, list[dict[str, Any]]] = {}
        for task in confirmed_order["tasks"]:
            tasks_by_source.setdefault(task["source_row_no"], []).append(task)
        for line in line_payloads:
            tasks = tasks_by_source.get(line["source_row_no"], [])
            allocated = sum(
                (Decimal(task["planned_quantity"]) for task in tasks),
                Decimal(0),
            )
            line["tasks"] = [
                {
                    **task,
                    "available_quantity": task["planned_quantity"],
                    "tag_suffixes": task["expected_tag_suffixes"],
                }
                for task in tasks
            ]
            line["allocated_quantity"] = _decimal_text(allocated)
            line["shortage_quantity"] = _decimal_text(
                max(Decimal(line["demand_quantity"]) - allocated, Decimal(0))
            )
            line["match_status"] = (
                "READY"
                if allocated >= Decimal(line["demand_quantity"])
                else "SHORTAGE"
                if allocated == 0
                else "PARTIAL"
            )

    total_required = sum(
        (Decimal(line.demand_quantity) for line in lines),
        Decimal(0),
    )
    total_allocated = sum(
        (Decimal(line["allocated_quantity"]) for line in line_payloads),
        Decimal(0),
    )
    blocking_errors = []
    if binding_errors:
        blocking_errors.append(
            {
                "code": "BINDING_CONFIGURATION_REQUIRED",
                "message": "存在未配置、冲突或无法唯一匹配的项目绑定",
                "lines": binding_errors,
            }
        )
    return {
        "manifest": {
            "manifest_id": str(manifest.manifest_id),
            "warehouse_id": manifest.warehouse_id,
            "manifest_no": manifest.manifest_no,
            "factory_shipment_no": manifest.factory_shipment_no,
            "source_file_name": manifest.source_file_name,
            "source_file_sha256": manifest.source_file_sha256,
            "source_template_version": manifest.source_template_version,
            "status": manifest.status,
            "metadata": manifest.metadata,
            "created_by": manifest.created_by,
            "created_at": _iso(manifest.created_at),
            "updated_at": _iso(manifest.updated_at),
        },
        "lines": line_payloads,
        "order": (
            {
                "outbound_order_id": str(order.outbound_order_id),
                "outbound_order_no": order.outbound_order_no,
                "status": order.status,
                "confirmed_by": order.confirmed_by,
                "confirmed_at": _iso(order.confirmed_at),
            }
            if order is not None
            else None
        ),
        "validation": {
            "can_confirm": (
                manifest.status == SHIPYARD_OUTBOUND_MANIFEST_PARSED
                and not binding_errors
            ),
            "binding_error_count": len(binding_errors),
            "blocking_errors": blocking_errors,
            "summary": {
                "line_count": len(lines),
                "ship_count": len({line.ship_no for line in lines}),
                "task_count": sum(len(line["tasks"]) for line in line_payloads),
                "shortage_line_count": sum(
                    Decimal(line["shortage_quantity"]) > 0 for line in line_payloads
                ),
                "total_required_quantity": _decimal_text(total_required),
                "total_allocated_quantity": _decimal_text(total_allocated),
                "total_shortage_quantity": _decimal_text(
                    max(total_required - total_allocated, Decimal(0))
                ),
            },
        },
    }


def list_outbound_manifests(
    db,
    *,
    warehouse_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            SELECT m.manifest_id, m.manifest_no, m.factory_shipment_no,
                   m.source_file_name, m.status, m.created_by, m.created_at,
                   COUNT(l.line_id) AS line_count,
                   COALESCE(SUM(l.demand_quantity), 0) AS total_quantity,
                   o.outbound_order_id, o.outbound_order_no, o.status
                       AS outbound_order_status
              FROM shipyard_outbound_manifests m
              LEFT JOIN shipyard_outbound_manifest_lines l
                ON l.manifest_id = m.manifest_id
              LEFT JOIN shipyard_outbound_orders o
                ON o.manifest_id = m.manifest_id
             WHERE m.warehouse_id = :warehouse_id
             GROUP BY m.manifest_id, o.outbound_order_id
             ORDER BY m.created_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": max(1, min(limit, 200))},
    ).fetchall()
    return {
        "manifests": [
            {
                "manifest_id": str(row.manifest_id),
                "manifest_no": row.manifest_no,
                "factory_shipment_no": row.factory_shipment_no,
                "source_file_name": row.source_file_name,
                "status": row.status,
                "line_count": row.line_count,
                "total_quantity": _decimal_text(row.total_quantity),
                "outbound_order_id": (
                    str(row.outbound_order_id) if row.outbound_order_id else None
                ),
                "outbound_order_no": row.outbound_order_no,
                "outbound_order_status": row.outbound_order_status,
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    }


def create_outbound_manifest_preview(
    db,
    *,
    warehouse_id: int,
    manifest_no: str | None,
    factory_shipment_no: str | None,
    source_file_name: str,
    content_type: str,
    content: bytes,
    created_by: str,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    parsed = parse_outbound_workbook(
        content,
        source_file_name=source_file_name,
    )
    resolved_factory_no = (factory_shipment_no or "").strip() or (
        parsed.factory_shipment_no or ""
    ).strip()
    if not resolved_factory_no:
        raise OutboundManifestStateError(
            "无法识别船厂出货单号",
            {"error_code": "FACTORY_SHIPMENT_NO_REQUIRED"},
        )
    resolved_manifest_no = (
        manifest_no or ""
    ).strip() or f"OB-{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    file_sha = _sha256(content)
    duplicate = db.execute(
        text(
            """
            SELECT manifest_id
              FROM shipyard_outbound_manifests
             WHERE warehouse_id = :warehouse_id
               AND source_file_sha256 = :file_sha
               AND status <> :void
             LIMIT 1
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "file_sha": file_sha,
            "void": SHIPYARD_OUTBOUND_MANIFEST_VOID,
        },
    ).fetchone()
    if duplicate is not None:
        raise DuplicateOutboundManifestFileError(
            "该出库清单文件已经导入",
            {"manifest_id": str(duplicate.manifest_id)},
        )
    duplicate_no = db.execute(
        text(
            """
            SELECT manifest_id
              FROM shipyard_outbound_manifests
             WHERE warehouse_id = :warehouse_id
               AND manifest_no = :manifest_no
               AND status <> :void
             LIMIT 1
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "manifest_no": resolved_manifest_no,
            "void": SHIPYARD_OUTBOUND_MANIFEST_VOID,
        },
    ).fetchone()
    if duplicate_no is not None:
        raise DuplicateOutboundManifestFileError(
            "该出库清单号已经导入",
            {"manifest_id": str(duplicate_no.manifest_id)},
        )

    manifest = db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_manifests
                (warehouse_id, manifest_no, factory_shipment_no,
                 source_file_name, source_file_sha256,
                 source_template_version, status, metadata, created_by)
            VALUES
                (:warehouse_id, :manifest_no, :factory_shipment_no,
                 :source_file_name, :source_file_sha256,
                 :template_version, :status, :metadata, :created_by)
            RETURNING manifest_id
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "manifest_no": resolved_manifest_no[:128],
            "factory_shipment_no": resolved_factory_no[:128],
            "source_file_name": source_file_name[:255],
            "source_file_sha256": file_sha,
            "template_version": parsed.metadata["parser_version"][:32],
            "status": SHIPYARD_OUTBOUND_MANIFEST_PARSED,
            "metadata": Json(parsed.metadata),
            "created_by": created_by,
        },
    ).fetchone()
    manifest_id = manifest.manifest_id
    db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_manifest_files
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
            "file_sha256": file_sha,
            "file_bytes": content,
        },
    )
    for line in parsed.lines:
        db.execute(
            text(
                """
                INSERT INTO shipyard_outbound_manifest_lines
                    (manifest_id, source_row_no, ticket_no, ship_no,
                     engineering_no, production_drawing_no,
                     manufacturer_name, material_code, item_name, item_spec,
                     demand_quantity, unit, demand_date,
                     expected_pallet_count, expected_tag_suffixes, status,
                     raw_data)
                VALUES
                    (:manifest_id, :source_row_no, :ticket_no, :ship_no,
                     :engineering_no, :production_drawing_no,
                     :manufacturer_name, :material_code, :item_name,
                     :item_spec, :demand_quantity, :unit, :demand_date,
                     :expected_pallet_count, :expected_tag_suffixes,
                     :status, :raw_data)
                """
            ),
            {
                "manifest_id": manifest_id,
                "source_row_no": line.source_row_no,
                "ticket_no": line.ticket_no,
                "ship_no": line.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": line.production_drawing_no,
                "manufacturer_name": line.manufacturer_name,
                "material_code": line.material_code,
                "item_name": line.item_name,
                "item_spec": line.item_spec,
                "demand_quantity": line.demand_quantity,
                "unit": line.unit,
                "demand_date": line.demand_date,
                "expected_pallet_count": line.expected_pallet_count,
                "expected_tag_suffixes": list(line.expected_tag_suffixes),
                "status": SHIPYARD_OUTBOUND_LINE_PENDING,
                "raw_data": Json(line.raw_data),
            },
        )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_OUTBOUND_MANIFEST_PARSED,
        entity_type="shipyard_outbound_manifest",
        entity_id=0,
        user_id=created_by,
        warehouse_id=warehouse_id,
        details={
            "manifest_id": str(manifest_id),
            "manifest_no": resolved_manifest_no,
            "factory_shipment_no": resolved_factory_no,
            "source_file_name": source_file_name,
            "line_count": len(parsed.lines),
        },
    )
    db.commit()
    return get_outbound_manifest_payload(db, manifest_id=str(manifest_id))


def _new_outbound_order_no() -> str:
    return f"OUT-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3).upper()}"


def confirm_outbound_manifest(
    db,
    *,
    manifest_id: str,
    confirmed_by: str,
) -> dict[str, Any]:
    manifest = _manifest_header(db, manifest_id, for_update=True)
    if manifest is None:
        raise OutboundManifestNotFoundError("正式出库清单不存在")
    existing = _existing_order_summary(db, manifest_id)
    if existing is not None:
        return get_outbound_manifest_payload(db, manifest_id=manifest_id)
    if manifest.status != SHIPYARD_OUTBOUND_MANIFEST_PARSED:
        raise OutboundManifestStateError(
            "当前出库清单状态不能生成正式出库单",
            {"status": manifest.status},
        )

    lines = _manifest_lines(db, manifest_id)
    bindings = _bindings_for_ships(
        db,
        sorted({line.ship_no for line in lines}),
    )
    binding_errors = []
    resolutions = {}
    for line in lines:
        binding = _binding_for_line(line, bindings)
        if binding["status"] != SHIPYARD_BINDING_MATCHED:
            binding_errors.append(
                {
                    "source_row_no": line.source_row_no,
                    "ship_no": line.ship_no,
                    **binding,
                }
            )
        resolutions[line.line_id] = binding
    if binding_errors:
        raise BindingConfigurationError(
            "船号/工程号/制作图绑定未配置、冲突或不唯一，不能生成出库单",
            {"lines": binding_errors},
        )

    used_ticket_ids: set[str] = set()
    allocations = {}
    for line in lines:
        allocations[line.line_id] = _allocate_line_tasks(
            db,
            warehouse_id=manifest.warehouse_id,
            line=line,
            binding=resolutions[line.line_id],
            used_ticket_ids=used_ticket_ids,
            for_update=True,
        )

    outbound_order_no = _new_outbound_order_no()
    order = db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_orders
                (warehouse_id, outbound_order_no, manifest_id,
                 factory_shipment_no, status, confirmed_by, metadata)
            VALUES
                (:warehouse_id, :outbound_order_no, :manifest_id,
                 :factory_shipment_no, :status, :confirmed_by, :metadata)
            RETURNING outbound_order_id
            """
        ),
        {
            "warehouse_id": manifest.warehouse_id,
            "outbound_order_no": outbound_order_no,
            "manifest_id": manifest_id,
            "factory_shipment_no": manifest.factory_shipment_no,
            "status": SHIPYARD_OUTBOUND_ORDER_PENDING,
            "confirmed_by": confirmed_by,
            "metadata": Json(
                {
                    "manifest_no": manifest.manifest_no,
                    "allocation_policy": "whole_ticket_fifo",
                }
            ),
        },
    ).fetchone()
    outbound_order_id = order.outbound_order_id

    for line in lines:
        allocation = allocations[line.line_id]
        allocated = allocation["allocated_quantity"]
        binding_id = resolutions[line.line_id].get("binding_id")
        line_status = (
            SHIPYARD_OUTBOUND_LINE_ALLOCATED
            if allocated > 0
            else SHIPYARD_OUTBOUND_LINE_SHORTAGE
        )
        order_line = db.execute(
            text(
                """
                INSERT INTO shipyard_outbound_order_lines
                    (outbound_order_id, manifest_line_id, source_row_no,
                     ticket_no, ship_no, engineering_no,
                     production_drawing_no, manufacturer_name,
                     material_code, item_name, item_spec, demand_quantity,
                     unit, allocated_quantity, fulfilled_quantity, status,
                     demand_date)
                VALUES
                    (:outbound_order_id, :manifest_line_id, :source_row_no,
                     :ticket_no, :ship_no, :engineering_no,
                     :production_drawing_no, :manufacturer_name,
                     :material_code, :item_name, :item_spec,
                     :demand_quantity, :unit, :allocated_quantity, 0,
                     :status, :demand_date)
                RETURNING order_line_id
                """
            ),
            {
                "outbound_order_id": outbound_order_id,
                "manifest_line_id": line.line_id,
                "source_row_no": line.source_row_no,
                "ticket_no": line.ticket_no,
                "ship_no": line.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": line.production_drawing_no,
                "manufacturer_name": allocation["binding"].get(
                    "manufacturer_name",
                    line.manufacturer_name,
                ),
                "material_code": line.material_code,
                "item_name": line.item_name,
                "item_spec": line.item_spec,
                "demand_quantity": line.demand_quantity,
                "unit": line.unit,
                "allocated_quantity": allocated,
                "status": line_status,
                "demand_date": line.demand_date,
            },
        ).fetchone()
        for task in allocation["tasks"]:
            task_no = (f"{outbound_order_no}-{line.source_row_no}-{task['ticket_no']}")[
                :160
            ]
            db.execute(
                text(
                    """
                    INSERT INTO shipyard_outbound_tasks
                        (task_no, outbound_order_id, order_line_id,
                         ticket_id, planned_quantity, expected_pallet_count,
                         expected_tag_suffixes, location_path_snapshot,
                         material_snapshot, status)
                    VALUES
                        (:task_no, :outbound_order_id, :order_line_id,
                         :ticket_id, :planned_quantity,
                         :expected_pallet_count, :expected_tag_suffixes,
                         :location_path_snapshot, :material_snapshot,
                         :status)
                    """
                ),
                {
                    "task_no": task_no,
                    "outbound_order_id": outbound_order_id,
                    "order_line_id": order_line.order_line_id,
                    "ticket_id": task["ticket_id"],
                    "planned_quantity": task["_quantity"],
                    "expected_pallet_count": task["expected_pallet_count"],
                    "expected_tag_suffixes": task["tag_suffixes"],
                    "location_path_snapshot": task["location_path_snapshot"],
                    "material_snapshot": Json(
                        {
                            key: value
                            for key, value in task.items()
                            if not key.startswith("_")
                        }
                    ),
                    "status": SHIPYARD_OUTBOUND_TASK_PENDING,
                },
            )
            db.execute(
                text(
                    """
                    UPDATE shipyard_cargo_tickets
                       SET status = :allocated,
                           updated_at = NOW()
                     WHERE ticket_id = :ticket_id
                       AND status IN (:in_stock, :partial)
                    """
                ),
                {
                    "allocated": SHIPYARD_TICKET_ALLOCATED,
                    "ticket_id": task["ticket_id"],
                    "in_stock": SHIPYARD_TICKET_IN_STOCK,
                    "partial": SHIPYARD_TICKET_PARTIAL,
                },
            )
        db.execute(
            text(
                """
                UPDATE shipyard_outbound_manifest_lines
                   SET project_binding_id = :binding_id,
                       status = :status,
                       updated_at = NOW()
                 WHERE line_id = :line_id
                """
            ),
            {
                "binding_id": binding_id,
                "status": (
                    SHIPYARD_OUTBOUND_LINE_ALLOCATED
                    if allocated > 0
                    else SHIPYARD_OUTBOUND_LINE_SHORTAGE
                ),
                "line_id": line.line_id,
            },
        )

    db.execute(
        text(
            """
            UPDATE shipyard_outbound_manifests
               SET status = :status, updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {
            "status": SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED,
            "manifest_id": manifest_id,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED,
        entity_type="shipyard_outbound_order",
        entity_id=0,
        user_id=confirmed_by,
        warehouse_id=manifest.warehouse_id,
        details={
            "manifest_id": manifest_id,
            "outbound_order_id": str(outbound_order_id),
            "outbound_order_no": outbound_order_no,
            "line_count": len(lines),
            "task_count": sum(
                len(allocation["tasks"]) for allocation in allocations.values()
            ),
        },
    )
    db.commit()
    return get_outbound_order_payload(
        db,
        outbound_order_id=str(outbound_order_id),
    )


def void_outbound_manifest(
    db,
    *,
    manifest_id: str,
    reason: str,
    voided_by: str,
) -> dict[str, Any]:
    manifest = _manifest_header(db, manifest_id, for_update=True)
    if manifest is None:
        raise OutboundManifestNotFoundError("正式出库清单不存在")
    if manifest.status != SHIPYARD_OUTBOUND_MANIFEST_PARSED:
        raise OutboundManifestStateError(
            "只有尚未生成出库单的清单可以作废",
            {"status": manifest.status},
        )
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_manifests
               SET status = :status,
                   metadata = metadata || :metadata,
                   updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {
            "status": SHIPYARD_OUTBOUND_MANIFEST_VOID,
            "metadata": Json(
                {
                    "void_reason": reason,
                    "voided_by": voided_by,
                    "voided_at": datetime.now(UTC).isoformat(),
                }
            ),
            "manifest_id": manifest_id,
        },
    )
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_manifest_lines
               SET status = 'VOID', updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_OUTBOUND_MANIFEST_VOIDED,
        entity_type="shipyard_outbound_manifest",
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
    return get_outbound_manifest_payload(db, manifest_id=manifest_id)


def get_outbound_manifest_file(db, *, manifest_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT f.file_name, f.content_type, f.file_size,
                   f.file_sha256, f.file_bytes, m.warehouse_id
              FROM shipyard_outbound_manifest_files f
              JOIN shipyard_outbound_manifests m
                ON m.manifest_id = f.manifest_id
             WHERE f.manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()
    if row is None:
        raise OutboundManifestNotFoundError("出库清单原文件不存在")
    return {
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_sha256": row.file_sha256,
        "file_bytes": bytes(row.file_bytes),
        "warehouse_id": row.warehouse_id,
    }


def _order_header(db, outbound_order_id: str, *, for_update: bool = False):
    lock = "FOR UPDATE OF o" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT o.outbound_order_id, o.warehouse_id, o.outbound_order_no,
                   o.manifest_id, o.factory_shipment_no, o.status,
                   o.confirmed_by, o.confirmed_at, o.metadata,
                   o.created_at, o.updated_at,
                   m.manifest_no, m.source_file_name
              FROM shipyard_outbound_orders o
              JOIN shipyard_outbound_manifests m
                ON m.manifest_id = o.manifest_id
             WHERE o.outbound_order_id = :outbound_order_id
             {lock}
            """  # nosec B608
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchone()


def _order_tasks(db, outbound_order_id: str):
    return db.execute(
        text(
            """
            SELECT t.task_id, t.task_no, t.order_line_id,
                   t.ticket_id, t.planned_quantity,
                   t.expected_pallet_count, t.expected_tag_suffixes,
                   t.location_path_snapshot, t.material_snapshot, t.status,
                   t.created_at, t.updated_at,
                   l.outbound_order_id, l.source_row_no, l.ticket_no,
                   l.ship_no, l.engineering_no, l.production_drawing_no,
                   l.manufacturer_name, l.material_code, l.item_name,
                   l.item_spec, l.demand_quantity, l.unit,
                   l.allocated_quantity, l.fulfilled_quantity,
                   oc.confirmation_id, oc.outbound_by, oc.outbound_at,
                   oc.location_path_snapshot AS outbound_location_snapshot,
                   lc.loading_confirmation_id, lc.loaded_by, lc.loaded_at
              FROM shipyard_outbound_tasks t
              JOIN shipyard_outbound_order_lines l
                ON l.order_line_id = t.order_line_id
              LEFT JOIN shipyard_outbound_confirmations oc
                ON oc.task_id = t.task_id
              LEFT JOIN shipyard_outbound_loading_confirmations lc
                ON lc.outbound_order_id = l.outbound_order_id
             WHERE t.outbound_order_id = :outbound_order_id
             ORDER BY l.source_row_no, t.created_at, t.task_no
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchall()


def get_outbound_order_payload(
    db,
    *,
    outbound_order_id: str,
) -> dict[str, Any]:
    order = _order_header(db, outbound_order_id)
    if order is None:
        raise OutboundOrderNotFoundError("正式出库单不存在")
    lines = db.execute(
        text(
            """
            SELECT order_line_id, source_row_no, ticket_no, ship_no,
                   engineering_no, production_drawing_no,
                   manufacturer_name, material_code, item_name, item_spec,
                   demand_quantity, unit, allocated_quantity,
                   fulfilled_quantity, status, demand_date, created_at
              FROM shipyard_outbound_order_lines
             WHERE outbound_order_id = :outbound_order_id
             ORDER BY source_row_no
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchall()
    tasks = _order_tasks(db, outbound_order_id)
    task_payloads = []
    for task in tasks:
        pallets = db.execute(
            text(
                """
                SELECT p.pallet_id, p.tag_suffix, p.batch_no,
                       p.factory_pallet_no, p.installation_pallet_no,
                       p.status, ph.photo_id,
                       ph.created_at AS photo_created_at
                  FROM shipyard_pallet_units p
                  LEFT JOIN LATERAL (
                      SELECT photo_id, created_at
                        FROM shipyard_photos
                       WHERE outbound_task_id = :task_id
                         AND pallet_id = p.pallet_id
                         AND photo_kind = 'PALLET'
                       ORDER BY created_at DESC, photo_id
                       LIMIT 1
                  ) ph ON TRUE
                 WHERE p.ticket_id = :ticket_id
                 ORDER BY p.tag_suffix NULLS FIRST
                """
            ),
            {
                "task_id": str(task.task_id),
                "ticket_id": str(task.ticket_id),
            },
        ).fetchall()
        task_payloads.append(
            {
                "task_id": str(task.task_id),
                "task_no": task.task_no,
                "order_line_id": str(task.order_line_id),
                "source_row_no": task.source_row_no,
                "ticket_no": task.ticket_no,
                "ticket_id": str(task.ticket_id),
                "ship_no": task.ship_no,
                "engineering_no": task.engineering_no,
                "production_drawing_no": task.production_drawing_no,
                "manufacturer_name": task.manufacturer_name,
                "material_code": task.material_code,
                "item_name": task.item_name,
                "item_spec": task.item_spec,
                "planned_quantity": _decimal_text(task.planned_quantity),
                "expected_pallet_count": task.expected_pallet_count,
                "expected_tag_suffixes": list(task.expected_tag_suffixes),
                "location_path_snapshot": task.location_path_snapshot,
                "status": task.status,
                "confirmation": (
                    {
                        "confirmation_id": str(task.confirmation_id),
                        "outbound_by": task.outbound_by,
                        "outbound_at": _iso(task.outbound_at),
                        "location_path_snapshot": (task.outbound_location_snapshot),
                    }
                    if task.confirmation_id
                    else None
                ),
                "pallets": [
                    {
                        "pallet_id": str(pallet.pallet_id),
                        "tag_suffix": pallet.tag_suffix,
                        "batch_no": pallet.batch_no,
                        "factory_pallet_no": pallet.factory_pallet_no,
                        "installation_pallet_no": (pallet.installation_pallet_no),
                        "status": pallet.status,
                        "photo_id": (str(pallet.photo_id) if pallet.photo_id else None),
                        "photo_created_at": _iso(pallet.photo_created_at),
                    }
                    for pallet in pallets
                ],
            }
        )
    line_payloads = [
        {
            "order_line_id": str(line.order_line_id),
            "source_row_no": line.source_row_no,
            "ticket_no": line.ticket_no,
            "ship_no": line.ship_no,
            "engineering_no": line.engineering_no,
            "production_drawing_no": line.production_drawing_no,
            "manufacturer_name": line.manufacturer_name,
            "material_code": line.material_code,
            "item_name": line.item_name,
            "item_spec": line.item_spec,
            "demand_quantity": _decimal_text(line.demand_quantity),
            "unit": line.unit,
            "allocated_quantity": _decimal_text(line.allocated_quantity),
            "fulfilled_quantity": _decimal_text(line.fulfilled_quantity),
            "shortage_quantity": _decimal_text(
                max(
                    Decimal(line.demand_quantity) - Decimal(line.allocated_quantity),
                    Decimal(0),
                )
            ),
            "status": line.status,
            "demand_date": _iso(line.demand_date),
            "tasks": [
                task
                for task in task_payloads
                if task["order_line_id"] == str(line.order_line_id)
            ],
        }
        for line in lines
    ]
    total_required = sum(
        (Decimal(line.demand_quantity) for line in lines),
        Decimal(0),
    )
    total_allocated = sum(
        (Decimal(line.allocated_quantity) for line in lines),
        Decimal(0),
    )
    total_fulfilled = sum(
        (Decimal(line.fulfilled_quantity) for line in lines),
        Decimal(0),
    )
    loading = db.execute(
        text(
            """
            SELECT loading_confirmation_id, notes, client_device_id,
                   loaded_by, loaded_at
              FROM shipyard_outbound_loading_confirmations
             WHERE outbound_order_id = :outbound_order_id
            """
        ),
        {"outbound_order_id": outbound_order_id},
    ).fetchone()
    return {
        "order": {
            "outbound_order_id": str(order.outbound_order_id),
            "warehouse_id": order.warehouse_id,
            "outbound_order_no": order.outbound_order_no,
            "manifest_id": str(order.manifest_id),
            "manifest_no": order.manifest_no,
            "factory_shipment_no": order.factory_shipment_no,
            "source_file_name": order.source_file_name,
            "status": order.status,
            "confirmed_by": order.confirmed_by,
            "confirmed_at": _iso(order.confirmed_at),
            "created_at": _iso(order.created_at),
            "updated_at": _iso(order.updated_at),
            "loading": (
                {
                    "loading_confirmation_id": str(loading.loading_confirmation_id),
                    "notes": loading.notes,
                    "client_device_id": loading.client_device_id,
                    "loaded_by": loading.loaded_by,
                    "loaded_at": _iso(loading.loaded_at),
                }
                if loading
                else None
            ),
        },
        "lines": line_payloads,
        "tasks": task_payloads,
        "summary": {
            "line_count": len(lines),
            "task_count": len(tasks),
            "pending_task_count": sum(
                task["status"] == SHIPYARD_OUTBOUND_TASK_PENDING
                for task in task_payloads
            ),
            "outbound_task_count": sum(
                task["status"] == SHIPYARD_OUTBOUND_TASK_OUTBOUND
                for task in task_payloads
            ),
            "shortage_line_count": sum(
                Decimal(line["shortage_quantity"]) > 0 for line in line_payloads
            ),
            "total_required_quantity": _decimal_text(total_required),
            "total_allocated_quantity": _decimal_text(total_allocated),
            "total_fulfilled_quantity": _decimal_text(total_fulfilled),
        },
    }


def list_outbound_orders(
    db,
    *,
    warehouse_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            WITH line_agg AS (
                SELECT outbound_order_id,
                       COUNT(*) AS line_count,
                       COALESCE(SUM(demand_quantity), 0)
                           AS total_required_quantity,
                       COALESCE(SUM(fulfilled_quantity), 0)
                           AS total_fulfilled_quantity
                  FROM shipyard_outbound_order_lines
                 GROUP BY outbound_order_id
            ),
            task_agg AS (
                SELECT outbound_order_id,
                       COUNT(*) AS task_count,
                       COUNT(*) FILTER (WHERE status = 'PENDING')
                           AS pending_task_count
                  FROM shipyard_outbound_tasks
                 GROUP BY outbound_order_id
            )
            SELECT o.outbound_order_id, o.outbound_order_no,
                   o.factory_shipment_no, o.status, o.confirmed_by,
                   o.confirmed_at, o.created_at,
                   COALESCE(la.line_count, 0) AS line_count,
                   COALESCE(ta.task_count, 0) AS task_count,
                   COALESCE(la.total_required_quantity, 0)
                       AS total_required_quantity,
                   COALESCE(la.total_fulfilled_quantity, 0)
                       AS total_fulfilled_quantity,
                   COALESCE(ta.pending_task_count, 0)
                       AS pending_task_count
              FROM shipyard_outbound_orders o
              LEFT JOIN line_agg la
                ON la.outbound_order_id = o.outbound_order_id
              LEFT JOIN task_agg ta
                ON ta.outbound_order_id = o.outbound_order_id
             WHERE o.warehouse_id = :warehouse_id
             ORDER BY o.confirmed_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": max(1, min(limit, 200))},
    ).fetchall()
    return {
        "orders": [
            {
                "outbound_order_id": str(row.outbound_order_id),
                "outbound_order_no": row.outbound_order_no,
                "factory_shipment_no": row.factory_shipment_no,
                "status": row.status,
                "line_count": row.line_count,
                "task_count": row.task_count,
                "pending_task_count": row.pending_task_count,
                "total_required_quantity": _decimal_text(row.total_required_quantity),
                "total_fulfilled_quantity": _decimal_text(row.total_fulfilled_quantity),
                "confirmed_by": row.confirmed_by,
                "confirmed_at": _iso(row.confirmed_at),
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    }
