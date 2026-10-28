"""Business logic for ship/project bindings and zone placement rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from constants import (
    ACTION_SHIPYARD_PROJECT_BINDING_CREATED,
    ACTION_SHIPYARD_PROJECT_BINDING_UPDATED,
    ACTION_SHIPYARD_ZONE_SHIP_MAPPING_CREATED,
    ACTION_SHIPYARD_ZONE_SHIP_MAPPING_UPDATED,
    SHIPYARD_STATUS_ACTIVE,
    SHIPYARD_STATUS_INACTIVE,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    ConfigConflictError,
    ShipyardNotFoundError,
    ShipyardValidationError,
)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _binding_payload(row) -> dict[str, Any]:
    return {
        "binding_id": row.binding_id,
        "ship_no": row.ship_no,
        "engineering_no": row.engineering_no,
        "production_drawing_no": row.production_drawing_no,
        "manufacturer_name": row.manufacturer_name,
        "status": row.status,
        "effective_from": _iso(row.effective_from),
        "effective_to": _iso(row.effective_to),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _mapping_payload(row) -> dict[str, Any]:
    return {
        "mapping_id": row.mapping_id,
        "warehouse_id": row.warehouse_id,
        "bin_id": row.bin_id,
        "bin_code": row.bin_code,
        "zone_id": row.zone_id,
        "zone_code": row.zone_code,
        "zone_name": row.zone_name,
        "location_path": " / ".join(
            value for value in (row.zone_code, row.bin_code) if value
        ),
        "ship_no": row.ship_no,
        "is_primary": row.is_primary,
        "status": row.status,
        "effective_from": _iso(row.effective_from),
        "effective_to": _iso(row.effective_to),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _constraint_name(exc: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diagnostic, "constraint_name", None) if diagnostic else None


def _raise_config_conflict(db, exc: IntegrityError):
    db.rollback()
    constraint = _constraint_name(exc)
    raise ConfigConflictError(
        "配置与现有生效区间或唯一规则冲突",
        {"constraint": constraint},
    ) from exc


def list_project_bindings(
    db,
    *,
    ship_no: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT binding_id, ship_no, engineering_no,
                   production_drawing_no, manufacturer_name, status,
                   effective_from, effective_to, created_at, updated_at
              FROM shipyard_project_bindings
             WHERE (:ship_no IS NULL OR ship_no = :ship_no)
               AND (:active_only = FALSE OR status = :active_status)
             ORDER BY ship_no, engineering_no, production_drawing_no,
                      manufacturer_name, effective_from DESC
            """
        ),
        {
            "ship_no": ship_no or None,
            "active_only": active_only,
            "active_status": SHIPYARD_STATUS_ACTIVE,
        },
    ).fetchall()
    return [_binding_payload(row) for row in rows]


def create_project_binding(db, *, validated, actor: str) -> dict[str, Any]:
    data = validated.model_dump()
    try:
        row = db.execute(
            text(
                """
                INSERT INTO shipyard_project_bindings
                    (ship_no, engineering_no, production_drawing_no,
                     manufacturer_name, status, effective_from)
                VALUES
                    (:ship_no, :engineering_no, :production_drawing_no,
                     :manufacturer_name, :status,
                     COALESCE(:effective_from, NOW()))
                RETURNING binding_id, ship_no, engineering_no,
                          production_drawing_no, manufacturer_name, status,
                          effective_from, effective_to, created_at, updated_at
                """
            ),
            {
                **data,
                "status": SHIPYARD_STATUS_ACTIVE,
            },
        ).fetchone()
    except IntegrityError as exc:
        _raise_config_conflict(db, exc)

    payload = _binding_payload(row)
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PROJECT_BINDING_CREATED,
        entity_type="shipyard_project_binding",
        entity_id=row.binding_id,
        user_id=actor,
        warehouse_id=None,
        details={"after": payload},
    )
    db.commit()
    return payload


def update_project_binding(
    db,
    *,
    binding_id: int,
    validated,
    actor: str,
) -> dict[str, Any]:
    current = db.execute(
        text(
            """
            SELECT binding_id, ship_no, engineering_no,
                   production_drawing_no, manufacturer_name, status,
                   effective_from, effective_to, created_at, updated_at
              FROM shipyard_project_bindings
             WHERE binding_id = :binding_id
             FOR UPDATE
            """
        ),
        {"binding_id": binding_id},
    ).fetchone()
    if current is None:
        raise ShipyardNotFoundError("船号绑定不存在")

    changes = validated.model_dump(exclude_unset=True)
    if "effective_from" in changes and changes["effective_from"] is None:
        raise ShipyardValidationError("effective_from 不能为空")
    if (
        changes.get("status") == SHIPYARD_STATUS_INACTIVE
        and "effective_to" not in changes
    ):
        changes["effective_to"] = datetime.now(UTC)

    effective_from = changes.get("effective_from", current.effective_from)
    effective_to = changes.get("effective_to", current.effective_to)
    if effective_to is not None and effective_to <= effective_from:
        raise ShipyardValidationError("生效结束时间必须晚于开始时间")
    if not changes:
        return _binding_payload(current)

    set_effective_from = "effective_from" in changes
    set_effective_to = "effective_to" in changes
    try:
        row = db.execute(
            text(
                """
                UPDATE shipyard_project_bindings
                   SET ship_no = COALESCE(:ship_no, ship_no),
                       engineering_no = COALESCE(:engineering_no, engineering_no),
                       production_drawing_no = COALESCE(
                           :production_drawing_no, production_drawing_no
                       ),
                       manufacturer_name = COALESCE(
                           :manufacturer_name, manufacturer_name
                       ),
                       status = COALESCE(:status, status),
                       effective_from = CASE
                           WHEN :set_effective_from
                           THEN :effective_from
                           ELSE effective_from
                       END,
                       effective_to = CASE
                           WHEN :set_effective_to
                           THEN :effective_to
                           ELSE effective_to
                       END,
                       updated_at = NOW()
                 WHERE binding_id = :binding_id
                RETURNING binding_id, ship_no, engineering_no,
                          production_drawing_no, manufacturer_name, status,
                          effective_from, effective_to, created_at, updated_at
                """
            ),
            {
                "binding_id": binding_id,
                "ship_no": changes.get("ship_no"),
                "engineering_no": changes.get("engineering_no"),
                "production_drawing_no": changes.get("production_drawing_no"),
                "manufacturer_name": changes.get("manufacturer_name"),
                "status": changes.get("status"),
                "set_effective_from": set_effective_from,
                "effective_from": changes.get("effective_from"),
                "set_effective_to": set_effective_to,
                "effective_to": changes.get("effective_to"),
            },
        ).fetchone()
    except IntegrityError as exc:
        _raise_config_conflict(db, exc)

    after = _binding_payload(row)
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PROJECT_BINDING_UPDATED,
        entity_type="shipyard_project_binding",
        entity_id=binding_id,
        user_id=actor,
        warehouse_id=None,
        details={
            "before": _binding_payload(current),
            "after": after,
        },
    )
    db.commit()
    return after


def list_zone_ship_mappings(
    db,
    *,
    warehouse_id: int,
    ship_no: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT m.mapping_id, b.warehouse_id, m.bin_id, b.bin_code,
                   z.zone_id, z.zone_code, z.zone_name, m.ship_no,
                   m.is_primary, m.status, m.effective_from, m.effective_to,
                   m.created_at, m.updated_at
              FROM shipyard_zone_ship_mappings m
              JOIN bins b ON b.bin_id = m.bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
             WHERE b.warehouse_id = :warehouse_id
               AND (:ship_no IS NULL OR m.ship_no = :ship_no)
               AND (:active_only = FALSE OR m.status = :active_status)
             ORDER BY m.ship_no, z.zone_code, b.bin_code,
                      m.effective_from DESC
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "ship_no": ship_no or None,
            "active_only": active_only,
            "active_status": SHIPYARD_STATUS_ACTIVE,
        },
    ).fetchall()
    return [_mapping_payload(row) for row in rows]


def list_storage_location_options(
    db,
    *,
    warehouse_id: int,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT b.bin_id, b.bin_code, b.bin_type,
                   z.zone_id, z.zone_code, z.zone_name, z.zone_type
              FROM bins b
              JOIN zones z ON z.zone_id = b.zone_id
             WHERE b.warehouse_id = :warehouse_id
               AND COALESCE(z.is_active, TRUE) = TRUE
             ORDER BY z.zone_code, b.bin_code
            """
        ),
        {"warehouse_id": warehouse_id},
    ).fetchall()
    return [
        {
            "bin_id": row.bin_id,
            "bin_code": row.bin_code,
            "bin_type": row.bin_type,
            "zone_id": row.zone_id,
            "zone_code": row.zone_code,
            "zone_name": row.zone_name,
            "zone_type": row.zone_type,
            "location_path": f"{row.zone_code} / {row.bin_code}",
        }
        for row in rows
    ]


def _bin_for_warehouse(db, bin_id: int, warehouse_id: int):
    return db.execute(
        text(
            """
            SELECT b.bin_id, b.bin_code, b.warehouse_id,
                   z.zone_id, z.zone_code, z.zone_name
              FROM bins b
              JOIN zones z ON z.zone_id = b.zone_id
             WHERE b.bin_id = :bin_id
               AND b.warehouse_id = :warehouse_id
            """
        ),
        {"bin_id": bin_id, "warehouse_id": warehouse_id},
    ).fetchone()


def _mapping_by_id(db, mapping_id: int):
    return db.execute(
        text(
            """
            SELECT m.mapping_id, b.warehouse_id, m.bin_id, b.bin_code,
                   z.zone_id, z.zone_code, z.zone_name, m.ship_no,
                   m.is_primary, m.status, m.effective_from, m.effective_to,
                   m.created_at, m.updated_at
              FROM shipyard_zone_ship_mappings m
              JOIN bins b ON b.bin_id = m.bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
             WHERE m.mapping_id = :mapping_id
            """
        ),
        {"mapping_id": mapping_id},
    ).fetchone()


def get_zone_ship_mapping_warehouse_id(db, *, mapping_id: int) -> int:
    row = db.execute(
        text(
            """
            SELECT b.warehouse_id
              FROM shipyard_zone_ship_mappings m
              JOIN bins b ON b.bin_id = m.bin_id
             WHERE m.mapping_id = :mapping_id
            """
        ),
        {"mapping_id": mapping_id},
    ).fetchone()
    if row is None:
        raise ShipyardNotFoundError("区域船号映射不存在")
    return row.warehouse_id


def create_zone_ship_mapping(db, *, validated, actor: str) -> dict[str, Any]:
    effective_from = validated.effective_from or datetime.now(UTC)
    if validated.effective_to is not None and validated.effective_to <= effective_from:
        raise ShipyardValidationError("生效结束时间必须晚于开始时间")
    if _bin_for_warehouse(db, validated.bin_id, validated.warehouse_id) is None:
        raise ShipyardValidationError(
            "库位不属于指定仓库",
            {"bin_id": validated.bin_id, "warehouse_id": validated.warehouse_id},
        )
    try:
        row = db.execute(
            text(
                """
                INSERT INTO shipyard_zone_ship_mappings
                    (bin_id, ship_no, is_primary, status,
                     effective_from, effective_to)
                VALUES
                    (:bin_id, :ship_no, :is_primary, :status,
                     COALESCE(:effective_from, NOW()), :effective_to)
                RETURNING mapping_id
                """
            ),
            {
                **validated.model_dump(),
                "status": SHIPYARD_STATUS_ACTIVE,
            },
        ).fetchone()
    except IntegrityError as exc:
        _raise_config_conflict(db, exc)

    payload = _mapping_payload(_mapping_by_id(db, row.mapping_id))
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_ZONE_SHIP_MAPPING_CREATED,
        entity_type="shipyard_zone_ship_mapping",
        entity_id=row.mapping_id,
        user_id=actor,
        warehouse_id=validated.warehouse_id,
        details={"after": payload},
    )
    db.commit()
    return payload


def update_zone_ship_mapping(
    db,
    *,
    mapping_id: int,
    validated,
    actor: str,
) -> dict[str, Any]:
    current = db.execute(
        text(
            """
            SELECT m.mapping_id, b.warehouse_id, m.bin_id, b.bin_code,
                   z.zone_id, z.zone_code, z.zone_name, m.ship_no,
                   m.is_primary, m.status, m.effective_from, m.effective_to,
                   m.created_at, m.updated_at
              FROM shipyard_zone_ship_mappings m
              JOIN bins b ON b.bin_id = m.bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
             WHERE m.mapping_id = :mapping_id
             FOR UPDATE OF m
            """
        ),
        {"mapping_id": mapping_id},
    ).fetchone()
    if current is None:
        raise ShipyardNotFoundError("区域船号映射不存在")

    changes = validated.model_dump(exclude_unset=True)
    if "effective_from" in changes and changes["effective_from"] is None:
        raise ShipyardValidationError("effective_from 不能为空")
    if (
        "bin_id" in changes
        and _bin_for_warehouse(db, changes["bin_id"], current.warehouse_id) is None
    ):
        raise ShipyardValidationError(
            "库位不属于指定仓库",
            {"bin_id": changes["bin_id"]},
        )
    if (
        changes.get("status") == SHIPYARD_STATUS_INACTIVE
        and "effective_to" not in changes
    ):
        changes["effective_to"] = datetime.now(UTC)

    effective_from = changes.get("effective_from", current.effective_from)
    effective_to = changes.get("effective_to", current.effective_to)
    if effective_to is not None and effective_to <= effective_from:
        raise ShipyardValidationError("生效结束时间必须晚于开始时间")
    if not changes:
        return _mapping_payload(current)

    set_effective_from = "effective_from" in changes
    set_effective_to = "effective_to" in changes
    try:
        db.execute(
            text(
                """
                UPDATE shipyard_zone_ship_mappings
                   SET bin_id = COALESCE(:bin_id, bin_id),
                       ship_no = COALESCE(:ship_no, ship_no),
                       is_primary = COALESCE(:is_primary, is_primary),
                       status = COALESCE(:status, status),
                       effective_from = CASE
                           WHEN :set_effective_from
                           THEN :effective_from
                           ELSE effective_from
                       END,
                       effective_to = CASE
                           WHEN :set_effective_to
                           THEN :effective_to
                           ELSE effective_to
                       END,
                       updated_at = NOW()
                 WHERE mapping_id = :mapping_id
                """
            ),
            {
                "mapping_id": mapping_id,
                "bin_id": changes.get("bin_id"),
                "ship_no": changes.get("ship_no"),
                "is_primary": changes.get("is_primary"),
                "status": changes.get("status"),
                "set_effective_from": set_effective_from,
                "effective_from": changes.get("effective_from"),
                "set_effective_to": set_effective_to,
                "effective_to": changes.get("effective_to"),
            },
        )
    except IntegrityError as exc:
        _raise_config_conflict(db, exc)

    after_row = _mapping_by_id(db, mapping_id)
    after = _mapping_payload(after_row)
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_ZONE_SHIP_MAPPING_UPDATED,
        entity_type="shipyard_zone_ship_mapping",
        entity_id=mapping_id,
        user_id=actor,
        warehouse_id=current.warehouse_id,
        details={
            "before": _mapping_payload(current),
            "after": after,
        },
    )
    db.commit()
    return after
