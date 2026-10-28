"""Ship/project bindings and zone-to-ship placement rules."""

from flask import g, jsonify, request

from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from schemas.shipyard import (
    CreateProjectBindingRequest,
    CreateZoneShipMappingRequest,
    UpdateProjectBindingRequest,
    UpdateZoneShipMappingRequest,
)
from services.shipyard.config_service import (
    create_project_binding as create_project_binding_record,
)
from services.shipyard.config_service import (
    create_zone_ship_mapping as create_zone_ship_mapping_record,
)
from services.shipyard.config_service import (
    get_zone_ship_mapping_warehouse_id,
)
from services.shipyard.config_service import (
    list_project_bindings as list_project_binding_records,
)
from services.shipyard.config_service import (
    list_storage_location_options as list_storage_location_option_records,
)
from services.shipyard.config_service import (
    list_zone_ship_mappings as list_zone_ship_mapping_records,
)
from services.shipyard.config_service import (
    update_project_binding as update_project_binding_record,
)
from services.shipyard.config_service import (
    update_zone_ship_mapping as update_zone_ship_mapping_record,
)
from utils.validation import validate_body

from . import require_shipyard_roles, shipyard_bp

CONFIG_PAGE = "shipyard-settings"
CONFIG_ROLES = ("SHIPYARD_MANAGER",)


@shipyard_bp.get("/bindings")
@require_auth
@require_admin_or_page_permission(CONFIG_PAGE)
@with_db
def list_project_bindings():
    bindings = list_project_binding_records(
        g.db,
        ship_no=(request.args.get("ship_no") or "").strip(),
        active_only=request.args.get("active_only", "true").lower() != "false",
    )
    return jsonify({"bindings": bindings})


@shipyard_bp.post("/bindings")
@require_auth
@require_shipyard_roles(*CONFIG_ROLES)
@require_admin_or_page_permission(CONFIG_PAGE)
@validate_body(CreateProjectBindingRequest)
@with_db
def create_project_binding(validated):
    binding = create_project_binding_record(
        g.db,
        validated=validated,
        actor=g.current_user["username"],
    )
    return jsonify({"binding": binding}), 201


@shipyard_bp.put("/bindings/<int:binding_id>")
@require_auth
@require_shipyard_roles(*CONFIG_ROLES)
@require_admin_or_page_permission(CONFIG_PAGE)
@validate_body(UpdateProjectBindingRequest)
@with_db
def update_project_binding(binding_id, validated):
    binding = update_project_binding_record(
        g.db,
        binding_id=binding_id,
        validated=validated,
        actor=g.current_user["username"],
    )
    return jsonify({"binding": binding})


@shipyard_bp.get("/zone-mappings")
@require_auth
@require_admin_or_page_permission(CONFIG_PAGE)
@with_db
def list_zone_ship_mappings():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id is required"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    mappings = list_zone_ship_mapping_records(
        g.db,
        warehouse_id=warehouse_id,
        ship_no=(request.args.get("ship_no") or "").strip(),
        active_only=request.args.get("active_only", "true").lower() != "false",
    )
    return jsonify({"mappings": mappings})


@shipyard_bp.get("/zone-mapping-options")
@require_auth
@require_admin_or_page_permission(CONFIG_PAGE)
@with_db
def list_zone_mapping_options():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id is required"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        {
            "locations": list_storage_location_option_records(
                g.db,
                warehouse_id=warehouse_id,
            )
        }
    )


@shipyard_bp.post("/zone-mappings")
@require_auth
@require_shipyard_roles(*CONFIG_ROLES)
@require_admin_or_page_permission(CONFIG_PAGE)
@validate_body(CreateZoneShipMappingRequest)
@with_db
def create_zone_ship_mapping(validated):
    ok, denied = check_warehouse_access(validated.warehouse_id)
    if not ok:
        return denied
    mapping = create_zone_ship_mapping_record(
        g.db,
        validated=validated,
        actor=g.current_user["username"],
    )
    return jsonify({"mapping": mapping}), 201


@shipyard_bp.put("/zone-mappings/<int:mapping_id>")
@require_auth
@require_shipyard_roles(*CONFIG_ROLES)
@require_admin_or_page_permission(CONFIG_PAGE)
@validate_body(UpdateZoneShipMappingRequest)
@with_db
def update_zone_ship_mapping(mapping_id, validated):
    warehouse_id = get_zone_ship_mapping_warehouse_id(
        g.db,
        mapping_id=mapping_id,
    )
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    mapping = update_zone_ship_mapping_record(
        g.db,
        mapping_id=mapping_id,
        validated=validated,
        actor=g.current_user["username"],
    )
    return jsonify({"mapping": mapping})
