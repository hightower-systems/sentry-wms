"""Admin connector management endpoints.

Provides CRUD for connector credentials and connection testing.
All endpoints require admin authentication. Credential values are
NEVER returned in plaintext -- only masked keys are shown.
"""

from flask import g, jsonify

from connectors import registry
from connectors.url_guard import BlockedDestinationError
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.connectors import DeleteCredentialsRequest, SaveCredentialsRequest, TestConnectionRequest
from services import credential_vault
from services import sync_state_service
from utils.validation import validate_body

VALID_SYNC_TYPES = ("orders", "items", "inventory", "fulfillment")


@admin_bp.route("/connectors", methods=["GET"])
@require_auth
@require_role("ADMIN")
def list_connectors():
    """List all registered connectors with their config schemas and capabilities."""
    connectors = registry.list_all()
    result = []
    for name, cls in connectors.items():
        # Instantiate with empty config just to read schema/capabilities
        instance = cls(config={})
        result.append({
            "name": name,
            "config_schema": instance.get_config_schema(),
            "capabilities": instance.get_capabilities(),
        })
    return jsonify({"connectors": result})


@admin_bp.route("/connectors/<connector_name>/config-schema", methods=["GET"])
@require_auth
@require_role("ADMIN")
def get_config_schema(connector_name):
    """Get the required credential fields for a specific connector."""
    try:
        cls = registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    instance = cls(config={})
    return jsonify({
        "name": connector_name,
        "config_schema": instance.get_config_schema(),
        "capabilities": instance.get_capabilities(),
    })


@admin_bp.route("/connectors/<connector_name>/credentials", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(SaveCredentialsRequest)
@with_db
def save_credentials(connector_name, validated):
    """Save credentials for a connector+warehouse. Encrypts each value before storing."""
    try:
        registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    warehouse_id = validated.warehouse_id
    for key, value in validated.credentials.items():
        credential_vault.store_credential(connector_name, warehouse_id, key, value)

    g.db.commit()
    return jsonify({
        "message": "Credentials saved",
        "connector": connector_name,
        "warehouse_id": warehouse_id,
        "keys": list(validated.credentials.keys()),
    })


@admin_bp.route("/connectors/<connector_name>/credentials", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def get_credentials(connector_name):
    """List stored credential keys for a connector+warehouse. Values are masked."""
    from flask import request
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id query parameter is required"}), 400

    try:
        registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    from sqlalchemy import text
    rows = g.db.execute(
        text("""
            SELECT credential_key, created_at, updated_at FROM connector_credentials
            WHERE connector_name = :name AND warehouse_id = :wid
        """),
        {"name": connector_name, "wid": warehouse_id},
    ).fetchall()

    return jsonify({
        "connector": connector_name,
        "warehouse_id": warehouse_id,
        "credentials": [
            {
                "key": row.credential_key,
                "value": "****",
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
    })


@admin_bp.route("/connectors/<connector_name>/test", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(TestConnectionRequest)
@with_db
def test_connection(connector_name, validated):
    """Test connection using stored credentials."""
    try:
        cls = registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    warehouse_id = validated.warehouse_id
    credentials = credential_vault.get_all_credentials(connector_name, warehouse_id)

    connector = cls(config=credentials)
    try:
        result = connector.test_connection()
    except BlockedDestinationError as exc:
        # Admin configured a base_url pointing at an internal or private
        # address. Surface as 400, not 500, so the admin UI shows a
        # friendly error instead of a generic failure.
        return jsonify({
            "connector": connector_name,
            "warehouse_id": warehouse_id,
            "connected": False,
            "error": "blocked_destination",
            "message": str(exc),
        }), 400

    return jsonify({
        "connector": connector_name,
        "warehouse_id": warehouse_id,
        "connected": result.connected,
        "message": result.message,
    })


@admin_bp.route("/connectors/<connector_name>/credentials", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@validate_body(DeleteCredentialsRequest)
@with_db
def remove_credentials(connector_name, validated):
    """Remove all credentials for a connector+warehouse."""
    try:
        registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    credential_vault.delete_all_credentials(connector_name, validated.warehouse_id)
    g.db.commit()

    return jsonify({
        "message": "Credentials deleted",
        "connector": connector_name,
        "warehouse_id": validated.warehouse_id,
    })


# ---------------------------------------------------------------------------
# Sync state endpoints (Phase 4)
# ---------------------------------------------------------------------------


@admin_bp.route("/connectors/<connector_name>/sync-status", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def get_sync_status(connector_name):
    """Return sync state for all sync types for this connector+warehouse.

    Query param: warehouse_id (required)
    """
    from flask import request
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id query parameter is required"}), 400

    try:
        registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    states = sync_state_service.get_all_sync_states(connector_name, warehouse_id)
    return jsonify({
        "connector": connector_name,
        "warehouse_id": warehouse_id,
        "sync_states": states,
    })


@admin_bp.route("/connectors/<connector_name>/sync-reset", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def reset_sync_state(connector_name):
    """V-012: clear stuck 'running' rows for this connector+warehouse.

    Body: {"warehouse_id": int, "sync_type": "orders" | ... | null}
    If sync_type is omitted or null, all sync types are reset.
    Returns the count of rows moved from 'running' to 'idle'.
    """
    from flask import request
    data = request.get_json(silent=True) or {}
    warehouse_id = data.get("warehouse_id")
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 400
    sync_type = data.get("sync_type")
    if sync_type is not None and sync_type not in VALID_SYNC_TYPES:
        return jsonify({
            "error": f"Invalid sync_type '{sync_type}'. Must be one of: {', '.join(VALID_SYNC_TYPES)}",
        }), 400

    try:
        registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    count = sync_state_service.reset_running(connector_name, int(warehouse_id), sync_type)
    g.db.commit()

    return jsonify({
        "message": "Reset",
        "connector": connector_name,
        "warehouse_id": int(warehouse_id),
        "sync_type": sync_type,
        "rows_reset": count,
    })


@admin_bp.route("/connectors/<connector_name>/sync/<sync_type>", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def trigger_sync(connector_name, sync_type):
    """Queue a manual sync for the specified connector+warehouse+type.

    Returns 202 Accepted with the Celery task ID on success, or 409 Conflict
    if a sync of that type is already running.
    """
    from flask import request
    data = request.get_json(silent=True) or {}
    warehouse_id = data.get("warehouse_id") or request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 400

    if sync_type not in VALID_SYNC_TYPES:
        return jsonify({
            "error": f"Invalid sync_type '{sync_type}'. Must be one of: {', '.join(VALID_SYNC_TYPES)}",
        }), 400

    try:
        registry.get(connector_name)
    except KeyError:
        return jsonify({"error": f"Connector '{connector_name}' not found"}), 404

    # Check if already running before queuing - avoids a useless task that just raises Ignore.
    # V-012: a 'running' row whose running_since is stale (older than
    # RUNNING_TIMEOUT) is treated as freeable so a crashed worker doesn't
    # block manual triggers indefinitely.
    current = sync_state_service.get_sync_state(connector_name, warehouse_id, sync_type)
    if current and current.get("sync_status") == "running":
        from sqlalchemy import text as _text
        row = g.db.execute(
            _text(
                "SELECT running_since FROM sync_state "
                "WHERE connector_name = :n AND warehouse_id = :w AND sync_type = :t"
            ),
            {"n": connector_name, "w": warehouse_id, "t": sync_type},
        ).fetchone()
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc) - sync_state_service.RUNNING_TIMEOUT
        is_stale = (
            row is not None
            and row.running_since is not None
            and row.running_since < cutoff
        )
        if not is_stale:
            return jsonify({
                "error": "Sync already running",
                "connector": connector_name,
                "warehouse_id": warehouse_id,
                "sync_type": sync_type,
            }), 409

    # Queue the task. Fulfillment manual triggers run a health check
    # (verifies connector is reachable) since actual pushes happen
    # automatically when orders ship.
    from jobs.sync_tasks import sync_orders, sync_items, sync_inventory, fulfillment_health_check
    task_map = {
        "orders": sync_orders,
        "items": sync_items,
        "inventory": sync_inventory,
        "fulfillment": fulfillment_health_check,
    }
    task = task_map[sync_type]
    async_result = task.delay(connector_name, warehouse_id)

    return jsonify({
        "message": "Sync queued",
        "task_id": async_result.id,
        "connector": connector_name,
        "warehouse_id": warehouse_id,
        "sync_type": sync_type,
    }), 202
