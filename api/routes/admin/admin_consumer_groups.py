"""Admin CRUD for v1.5.0 connectors + consumer_groups (#125).

Two closely paired resources:

1. ``connector-registry`` - the v1.5.0 ``connectors`` table, a PK +
   display_name + timestamps. Distinct from /api/admin/connectors,
   which serves the legacy v1.3 connector_credentials vault. Named
   explicitly so the paths do not collide while the two concepts
   converge in v1.9.

2. ``consumer-groups`` - per-connector cursor state for GET
   /api/v1/events polling. Groups reference connectors via the FK
   set up in migration 021.

All endpoints require ADMIN role via cookie auth (Decision I: v1.5.0
group provisioning is admin-panel only; connector self-registration
via X-WMS-Token is v1.9).
"""

import json

import psycopg2
from flask import g, jsonify
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.consumer_groups import (
    ConnectorCreateRequest,
    ConsumerGroupCreateRequest,
    ConsumerGroupUpdateRequest,
)
from utils.validation import validate_body


def _row_to_connector(row) -> dict:
    return {
        "connector_id": row.connector_id,
        "display_name": row.display_name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _row_to_group(row) -> dict:
    subscription = row.subscription or {}
    if isinstance(subscription, str):
        subscription = json.loads(subscription)
    return {
        "consumer_group_id": row.consumer_group_id,
        "connector_id": row.connector_id,
        "last_cursor": row.last_cursor,
        "last_heartbeat": row.last_heartbeat.isoformat() if row.last_heartbeat else None,
        "subscription": subscription,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ── Connector registry (v1.5.0 connectors table) ────────────────────────


@admin_bp.route("/connector-registry", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(ConnectorCreateRequest)
@with_db
def create_registered_connector(validated):
    try:
        row = g.db.execute(
            text(
                "INSERT INTO connectors (connector_id, display_name) "
                "VALUES (:cid, :name) RETURNING connector_id, display_name, "
                "created_at, updated_at"
            ),
            {"cid": validated.connector_id, "name": validated.display_name},
        ).fetchone()
    except IntegrityError:
        g.db.rollback()
        return jsonify({"error": "duplicate_connector_id"}), 409
    g.db.commit()
    return jsonify(_row_to_connector(row)), 201


@admin_bp.route("/connector-registry", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_registered_connectors():
    rows = g.db.execute(
        text(
            "SELECT connector_id, display_name, created_at, updated_at "
            "  FROM connectors ORDER BY created_at DESC"
        )
    ).fetchall()
    return jsonify({"connectors": [_row_to_connector(r) for r in rows]})


# ── Consumer groups ─────────────────────────────────────────────────────


@admin_bp.route("/consumer-groups", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(ConsumerGroupCreateRequest)
@with_db
def create_consumer_group(validated):
    try:
        row = g.db.execute(
            text(
                """
                INSERT INTO consumer_groups
                    (consumer_group_id, connector_id, subscription)
                VALUES (:cgid, :cid, CAST(:sub AS JSONB))
                RETURNING consumer_group_id, connector_id, last_cursor,
                          last_heartbeat, subscription, created_at, updated_at
                """
            ),
            {
                "cgid": validated.consumer_group_id,
                "cid": validated.connector_id,
                "sub": json.dumps(validated.subscription),
            },
        ).fetchone()
    except IntegrityError as e:
        g.db.rollback()
        cause = getattr(e, "orig", None)
        # Distinguish between duplicate consumer_group_id and unknown
        # connector_id. psycopg2 maps UNIQUE violations to pgcode
        # '23505' and FK violations to '23503'.
        pgcode = getattr(cause, "pgcode", None) if cause is not None else None
        if pgcode == "23505":
            return jsonify({"error": "duplicate_consumer_group_id"}), 409
        if pgcode == "23503":
            return jsonify({"error": "unknown_connector_id"}), 400
        raise
    g.db.commit()
    return jsonify(_row_to_group(row)), 201


@admin_bp.route("/consumer-groups", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_consumer_groups():
    rows = g.db.execute(
        text(
            "SELECT consumer_group_id, connector_id, last_cursor, "
            "       last_heartbeat, subscription, created_at, updated_at "
            "  FROM consumer_groups "
            " ORDER BY created_at DESC"
        )
    ).fetchall()
    return jsonify({"consumer_groups": [_row_to_group(r) for r in rows]})


@admin_bp.route("/consumer-groups/<consumer_group_id>", methods=["PATCH"])
@require_auth
@require_role("ADMIN")
@validate_body(ConsumerGroupUpdateRequest)
@with_db
def update_consumer_group(validated, consumer_group_id):
    updates = []
    params = {"cgid": consumer_group_id}
    if validated.subscription is not None:
        updates.append("subscription = CAST(:sub AS JSONB)")
        params["sub"] = json.dumps(validated.subscription)
    if not updates:
        return jsonify({"error": "no_fields_to_update"}), 400
    updates.append("updated_at = NOW()")
    sql = (
        "UPDATE consumer_groups SET " + ", ".join(updates)
        + " WHERE consumer_group_id = :cgid "
          "RETURNING consumer_group_id, connector_id, last_cursor, "
                    "last_heartbeat, subscription, created_at, updated_at"
    )
    row = g.db.execute(text(sql), params).fetchone()
    if row is None:
        return jsonify({"error": "consumer_group_not_found"}), 404
    g.db.commit()
    return jsonify(_row_to_group(row))


@admin_bp.route("/consumer-groups/<consumer_group_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_consumer_group(consumer_group_id):
    row = g.db.execute(
        text(
            "DELETE FROM consumer_groups WHERE consumer_group_id = :cgid "
            "RETURNING consumer_group_id"
        ),
        {"cgid": consumer_group_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "consumer_group_not_found"}), 404
    g.db.commit()
    return ("", 204)
