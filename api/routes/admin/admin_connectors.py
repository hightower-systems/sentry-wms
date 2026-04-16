"""Admin connector management endpoints.

Provides CRUD for connector credentials and connection testing.
All endpoints require admin authentication. Credential values are
NEVER returned in plaintext -- only masked keys are shown.
"""

from flask import g, jsonify

from connectors import registry
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.connectors import DeleteCredentialsRequest, SaveCredentialsRequest, TestConnectionRequest
from services import credential_vault
from utils.validation import validate_body


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
    result = connector.test_connection()

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
