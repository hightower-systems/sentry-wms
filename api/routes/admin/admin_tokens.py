"""Admin CRUD + rotate + revoke for wms_tokens (v1.5.0 #129).

All endpoints require ADMIN role via cookie auth. The plaintext token
value is generated server-side from ``secrets.token_urlsafe(32)``,
hashed with SENTRY_TOKEN_PEPPER per Decision Q, and returned to the
admin exactly once in the response body for issuance and rotation.
Storage is hash-only (Decision P): a lost plaintext means the admin
rotates, no recovery path.

Rotation-age badge is computed server-side so the threshold lives in
one place. 0-74 days: no badge. 75-89 days: "recommended". 90+: "overdue".
The admin UI renders whichever the server returns; no client-side
threshold logic.
"""

import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from flask import g, jsonify, request
from sqlalchemy import text

from constants import (
    ACTION_TOKEN_ISSUE,
    ACTION_TOKEN_ROTATE,
    ACTION_TOKEN_REVOKE,
    ACTION_TOKEN_DELETE,
)
from middleware.auth_middleware import (
    require_auth,
    require_role,
    validate_pepper_config,
)
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.tokens import CreateTokenRequest, UpdateTokenRequest
from services import token_cache
from services.audit_service import write_audit_log
from utils.validation import validate_body

# Match the plan's thresholds; admin panel renders whatever status the
# server returns so no code dupes the boundary values.
_RECOMMENDED_DAYS = 75
_OVERDUE_DAYS = 90


def _hash_for_storage(plaintext: str) -> str:
    """SHA256(pepper || plaintext).hexdigest() per Decision Q.

    v1.5.1 V-201 (#142): routed through the shared validator so a
    weak pepper (short, whitespace-only, placeholder) cannot
    silently produce a weakly-peppered hash at issuance time even
    if the boot guard was bypassed.
    """
    pepper_bytes = validate_pepper_config(os.environ.get("SENTRY_TOKEN_PEPPER"))
    return hashlib.sha256(pepper_bytes + plaintext.encode("utf-8")).hexdigest()


def _rotation_status(rotated_at: datetime) -> str:
    days = (datetime.now(timezone.utc) - rotated_at).days
    if days >= _OVERDUE_DAYS:
        return "overdue"
    if days >= _RECOMMENDED_DAYS:
        return "recommended"
    return "none"


def _row_to_listing(row) -> dict:
    """Serialise a wms_tokens row for the admin listing (no plaintext)."""
    rotated_at = row.rotated_at
    return {
        "token_id": row.token_id,
        "token_name": row.token_name,
        "warehouse_ids": list(row.warehouse_ids) if row.warehouse_ids else [],
        "event_types": list(row.event_types) if row.event_types else [],
        "endpoints": list(row.endpoints) if row.endpoints else [],
        "connector_id": row.connector_id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "rotated_at": rotated_at.isoformat() if rotated_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "rotation_status": _rotation_status(rotated_at) if rotated_at else "none",
    }


@admin_bp.route("/tokens", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(CreateTokenRequest)
@with_db
def create_token(validated):
    """Issue a new inbound API token. Returns plaintext once."""
    plaintext = secrets.token_urlsafe(32)
    token_hash = _hash_for_storage(plaintext)

    if validated.expires_at is not None:
        result = g.db.execute(
            text(
                """
                INSERT INTO wms_tokens (
                    token_name, token_hash,
                    warehouse_ids, event_types, endpoints,
                    connector_id, expires_at
                ) VALUES (
                    :name, :hash,
                    :wh_ids, :ev_types, :endpoints,
                    :connector_id, :expires_at
                )
                RETURNING token_id, created_at, rotated_at, expires_at, status
                """
            ),
            {
                "name": validated.token_name,
                "hash": token_hash,
                "wh_ids": validated.warehouse_ids,
                "ev_types": validated.event_types,
                "endpoints": validated.endpoints,
                "connector_id": validated.connector_id,
                "expires_at": validated.expires_at,
            },
        )
    else:
        result = g.db.execute(
            text(
                """
                INSERT INTO wms_tokens (
                    token_name, token_hash,
                    warehouse_ids, event_types, endpoints,
                    connector_id
                ) VALUES (
                    :name, :hash,
                    :wh_ids, :ev_types, :endpoints,
                    :connector_id
                )
                RETURNING token_id, created_at, rotated_at, expires_at, status
                """
            ),
            {
                "name": validated.token_name,
                "hash": token_hash,
                "wh_ids": validated.warehouse_ids,
                "ev_types": validated.event_types,
                "endpoints": validated.endpoints,
                "connector_id": validated.connector_id,
            },
        )
    row = result.fetchone()
    # v1.5.1 V-208 (#141): one audit row per issuance. Scope snapshot
    # in details so a later delete does not erase forensic context.
    # Plaintext never appears here; the stored hash lives in wms_tokens
    # and does not need to be duplicated to audit_log.
    write_audit_log(
        g.db,
        action_type=ACTION_TOKEN_ISSUE,
        entity_type="WMS_TOKEN",
        entity_id=row.token_id,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "token_name": validated.token_name,
            "warehouse_ids": list(validated.warehouse_ids),
            "event_types": list(validated.event_types),
            "endpoints": list(validated.endpoints),
            "connector_id": validated.connector_id,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        },
    )
    g.db.commit()
    return (
        jsonify(
            {
                "token_id": row.token_id,
                "token_name": validated.token_name,
                "token": plaintext,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "rotated_at": row.rotated_at.isoformat(),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }
        ),
        201,
    )


@admin_bp.route("/tokens", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_tokens():
    """Return every wms_tokens row with its rotation-age status. No plaintext."""
    rows = g.db.execute(
        text(
            """
            SELECT token_id, token_name, warehouse_ids, event_types, endpoints,
                   connector_id, status, created_at, rotated_at, expires_at,
                   revoked_at, last_used_at
              FROM wms_tokens
             ORDER BY created_at DESC
            """
        )
    ).fetchall()
    return jsonify({"tokens": [_row_to_listing(r) for r in rows]})


@admin_bp.route("/tokens/<int:token_id>/rotate", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def rotate_token(token_id):
    """Issue a new plaintext, replace the hash, bump rotated_at. Plaintext once."""
    existing = g.db.execute(
        text(
            "SELECT token_id, token_name, status FROM wms_tokens "
            "WHERE token_id = :tid FOR UPDATE"
        ),
        {"tid": token_id},
    ).fetchone()
    if not existing:
        return jsonify({"error": "Token not found"}), 404
    if existing.status == "revoked":
        return jsonify({"error": "Cannot rotate a revoked token"}), 400

    plaintext = secrets.token_urlsafe(32)
    new_hash = _hash_for_storage(plaintext)
    row = g.db.execute(
        text(
            """
            UPDATE wms_tokens
               SET token_hash = :h,
                   rotated_at = NOW(),
                   status     = 'active'
             WHERE token_id = :tid
             RETURNING rotated_at, expires_at, status
            """
        ),
        {"h": new_hash, "tid": token_id},
    ).fetchone()
    # v1.5.1 V-208 (#141): audit the rotation. No scope change on
    # rotate, so details captures only the affected token.
    write_audit_log(
        g.db,
        action_type=ACTION_TOKEN_ROTATE,
        entity_type="WMS_TOKEN",
        entity_id=token_id,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={"token_name": existing.token_name},
    )
    g.db.commit()

    # v1.5.1 V-205 (#146): targeted cross-worker invalidation. Evicts
    # the entry on this worker AND publishes a Redis pubsub message
    # that every other worker's subscriber thread evicts on within
    # one round-trip, replacing the v1.5.0 up-to-60s per-worker
    # revocation window. The 60s TTL remains as a backstop.
    token_cache.invalidate(token_id)

    return jsonify(
        {
            "token_id": token_id,
            "token_name": existing.token_name,
            "token": plaintext,
            "status": row.status,
            "rotated_at": row.rotated_at.isoformat(),
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }
    )


@admin_bp.route("/tokens/<int:token_id>/revoke", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def revoke_token(token_id):
    """Flip status to revoked + stamp revoked_at. The cache TTL means
    the revocation takes effect within 60s across workers."""
    row = g.db.execute(
        text(
            """
            UPDATE wms_tokens
               SET status = 'revoked',
                   revoked_at = NOW()
             WHERE token_id = :tid
             RETURNING token_id, token_name, status, revoked_at
            """
        ),
        {"tid": token_id},
    ).fetchone()
    if not row:
        return jsonify({"error": "Token not found"}), 404
    # v1.5.1 V-208 (#141): audit the revocation.
    write_audit_log(
        g.db,
        action_type=ACTION_TOKEN_REVOKE,
        entity_type="WMS_TOKEN",
        entity_id=row.token_id,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={"token_name": row.token_name},
    )
    g.db.commit()
    # v1.5.1 V-205 (#146): targeted cross-worker invalidation.
    token_cache.invalidate(row.token_id)
    return jsonify(
        {
            "token_id": row.token_id,
            "status": row.status,
            "revoked_at": row.revoked_at.isoformat(),
        }
    )


@admin_bp.route("/tokens/<int:token_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_token(token_id):
    """Hard delete a token row. The decorator already rejects the hash
    on the next request; deletion removes the row from the admin list."""
    # v1.5.1 V-208 (#141): RETURNING the scope snapshot before the row
    # disappears so the audit trail survives the delete. Plaintext is
    # not stored so nothing sensitive beyond what the admin already
    # configured gets logged.
    result = g.db.execute(
        text(
            "DELETE FROM wms_tokens WHERE token_id = :tid "
            "RETURNING token_id, token_name, warehouse_ids, event_types, "
            "endpoints, connector_id, status"
        ),
        {"tid": token_id},
    ).fetchone()
    if not result:
        return jsonify({"error": "Token not found"}), 404
    write_audit_log(
        g.db,
        action_type=ACTION_TOKEN_DELETE,
        entity_type="WMS_TOKEN",
        entity_id=result.token_id,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "token_name": result.token_name,
            "previous_scope": {
                "warehouse_ids": list(result.warehouse_ids) if result.warehouse_ids else [],
                "event_types": list(result.event_types) if result.event_types else [],
                "endpoints": list(result.endpoints) if result.endpoints else [],
                "connector_id": result.connector_id,
                "status_at_delete": result.status,
            },
        },
    )
    g.db.commit()
    # v1.5.1 V-205 (#146): targeted cross-worker invalidation.
    token_cache.invalidate(result.token_id)
    return ("", 204)
