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

from constants import (
    ACTION_CONNECTOR_REGISTRY_CREATE,
    ACTION_CONNECTOR_REGISTRY_DELETE,
    ACTION_CONNECTOR_REGISTRY_UPDATE,
    ACTION_CONSUMER_GROUP_CREATE,
    ACTION_CONSUMER_GROUP_UPDATE,
    ACTION_CONSUMER_GROUP_DELETE,
)
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.consumer_groups import (
    ConnectorCreateRequest,
    ConnectorUpdateRequest,
    ConsumerGroupCreateRequest,
    ConsumerGroupUpdateRequest,
)
from services.audit_service import write_audit_log
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
    # v1.5.1 V-221 (#154): audit the registry write. entity_id=0
    # sentinel because connector_id is string; the real id lives in
    # details so the trail remains bindable.
    write_audit_log(
        g.db,
        action_type=ACTION_CONNECTOR_REGISTRY_CREATE,
        entity_type="CONNECTOR_REGISTRY",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "connector_id": validated.connector_id,
            "display_name": validated.display_name,
        },
    )
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


@admin_bp.route("/connector-registry/<connector_id>", methods=["PATCH"])
@require_auth
@require_role("ADMIN")
@validate_body(ConnectorUpdateRequest)
@with_db
def update_registered_connector(validated, connector_id):
    """Update a connector's display_name. connector_id itself is the
    FK target from consumer_groups + webhook_subscriptions; renaming
    would orphan rows so the schema does not accept it."""
    current = g.db.execute(
        text(
            "SELECT connector_id, display_name, created_at, updated_at "
            "  FROM connectors WHERE connector_id = :cid"
        ),
        {"cid": connector_id},
    ).fetchone()
    if current is None:
        return jsonify({"error": "connector_not_found"}), 404

    if validated.display_name == current.display_name:
        # No-op: do not write an audit row for an empty diff.
        return jsonify(_row_to_connector(current))

    g.db.execute(
        text(
            "UPDATE connectors SET display_name = :dn, updated_at = NOW() "
            " WHERE connector_id = :cid"
        ),
        {"dn": validated.display_name, "cid": connector_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_CONNECTOR_REGISTRY_UPDATE,
        entity_type="CONNECTOR",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "connector_id": connector_id,
            "diff": {
                "display_name": {
                    "before": current.display_name,
                    "after": validated.display_name,
                }
            },
        },
    )
    g.db.commit()

    refreshed = g.db.execute(
        text(
            "SELECT connector_id, display_name, created_at, updated_at "
            "  FROM connectors WHERE connector_id = :cid"
        ),
        {"cid": connector_id},
    ).fetchone()
    return jsonify(_row_to_connector(refreshed))


@admin_bp.route("/connector-registry/<connector_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_registered_connector(connector_id):
    """Delete a connector when no consumer_groups or
    webhook_subscriptions reference it. The FK targets are
    distinct (one ON DELETE CASCADE'd consumer_groups path would
    silently drop cursor state; we refuse instead so the operator
    explicitly migrates dependents off first)."""
    current = g.db.execute(
        text(
            "SELECT connector_id, display_name FROM connectors "
            " WHERE connector_id = :cid"
        ),
        {"cid": connector_id},
    ).fetchone()
    if current is None:
        return jsonify({"error": "connector_not_found"}), 404

    cg_count = int(
        g.db.execute(
            text(
                "SELECT COUNT(*) AS n FROM consumer_groups "
                " WHERE connector_id = :cid"
            ),
            {"cid": connector_id},
        ).fetchone().n
        or 0
    )
    ws_count = int(
        g.db.execute(
            text(
                "SELECT COUNT(*) AS n FROM webhook_subscriptions "
                " WHERE connector_id = :cid"
            ),
            {"cid": connector_id},
        ).fetchone().n
        or 0
    )
    if cg_count > 0 or ws_count > 0:
        return (
            jsonify(
                {
                    "error": "connector_in_use",
                    "consumer_groups": cg_count,
                    "webhook_subscriptions": ws_count,
                    "detail": (
                        "the connector still has consumer_groups or "
                        "webhook_subscriptions referencing it; migrate "
                        "or delete dependents before retrying."
                    ),
                }
            ),
            409,
        )

    g.db.execute(
        text("DELETE FROM connectors WHERE connector_id = :cid"),
        {"cid": connector_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_CONNECTOR_REGISTRY_DELETE,
        entity_type="CONNECTOR",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "connector_id": connector_id,
            "display_name_at_delete": current.display_name,
        },
    )
    g.db.commit()
    return jsonify({"connector_id": connector_id, "deleted": True})


# ── Consumer groups ─────────────────────────────────────────────────────


@admin_bp.route("/consumer-groups", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(ConsumerGroupCreateRequest)
@with_db
def create_consumer_group(validated):
    # v1.5.1 V-207 (#148): check for a tombstone from a prior
    # deletion under the same consumer_group_id. Recreating would
    # reset last_cursor to 0 and replay every event since the
    # outbox dawn; force the admin to acknowledge the gap
    # explicitly before we proceed.
    tombstone = g.db.execute(
        text(
            "SELECT last_cursor_at_delete, connector_id, deleted_at, deleted_by "
            "  FROM consumer_groups_tombstones "
            " WHERE consumer_group_id = :cgid"
        ),
        {"cgid": validated.consumer_group_id},
    ).fetchone()
    if tombstone is not None and not validated.acknowledge_replay:
        return (
            jsonify(
                {
                    "error": "replay_would_skip_history",
                    "consumer_group_id": validated.consumer_group_id,
                    "last_cursor_at_delete": tombstone.last_cursor_at_delete,
                    "deleted_at": tombstone.deleted_at.isoformat(),
                    "deleted_by": tombstone.deleted_by,
                    "message": (
                        "This consumer_group_id was deleted at "
                        f"last_cursor={tombstone.last_cursor_at_delete}. "
                        "Recreating it starts a fresh scan from event_id=0 "
                        "and replays every event in the outbox. If that "
                        "is intended, resubmit with "
                        "{\"acknowledge_replay\": true}. To avoid replay "
                        "entirely, pick a new consumer_group_id."
                    ),
                }
            ),
            409,
        )

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
                # v1.5.1 V-204 (#145): exclude_none so an empty
                # SubscriptionFilter persists as {} not
                # {"event_types": null, "warehouse_ids": null},
                # matching the pre-v1.5.1 storage shape.
                "sub": json.dumps(
                    validated.subscription.model_dump(exclude_none=True)
                ),
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

    # v1.5.1 V-207 (#148): an acknowledged-replay create clears the
    # tombstone so a subsequent DELETE of this group starts a new
    # tombstone cycle cleanly.
    if tombstone is not None:
        g.db.execute(
            text(
                "DELETE FROM consumer_groups_tombstones "
                " WHERE consumer_group_id = :cgid"
            ),
            {"cgid": validated.consumer_group_id},
        )

    # v1.5.1 V-221 (#154): audit the create. acknowledged_replay
    # records whether this create came out of an acknowledged
    # tombstone so post-incident reviewers can spot replays at a
    # glance.
    write_audit_log(
        g.db,
        action_type=ACTION_CONSUMER_GROUP_CREATE,
        entity_type="CONSUMER_GROUP",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "consumer_group_id": validated.consumer_group_id,
            "connector_id": validated.connector_id,
            "subscription": validated.subscription.model_dump(exclude_none=True),
            "acknowledged_replay": tombstone is not None,
        },
    )

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
        params["sub"] = json.dumps(
            validated.subscription.model_dump(exclude_none=True)
        )
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
    # v1.5.1 V-221 (#154): audit the patch. Only subscription is
    # mutable in v1.5 (last_cursor + connector_id are explicitly
    # not operator-editable from the wire); the audit row captures
    # the new subscription so forensics can diff against the prior
    # CREATE / UPDATE entries.
    write_audit_log(
        g.db,
        action_type=ACTION_CONSUMER_GROUP_UPDATE,
        entity_type="CONSUMER_GROUP",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "consumer_group_id": consumer_group_id,
            "subscription": (
                validated.subscription.model_dump(exclude_none=True)
                if validated.subscription is not None
                else None
            ),
        },
    )
    g.db.commit()
    return jsonify(_row_to_group(row))


@admin_bp.route("/consumer-groups/<consumer_group_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_consumer_group(consumer_group_id):
    # v1.5.1 V-207 (#148): RETURNING the connector_id + last_cursor so
    # the tombstone UPSERT below has the values it needs to build a
    # useful 409 response if the admin later recreates this group.
    # v1.5.1 V-221 (#154): also RETURN subscription so the audit
    # row captures the shape being erased without a second SELECT.
    row = g.db.execute(
        text(
            "DELETE FROM consumer_groups WHERE consumer_group_id = :cgid "
            "RETURNING consumer_group_id, connector_id, last_cursor, "
            "subscription"
        ),
        {"cgid": consumer_group_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "consumer_group_not_found"}), 404

    # Tombstone UPSERT: repeated delete cycles on the same id always
    # reflect the most recent cursor at deletion so the 409 response
    # on recreate is accurate regardless of how many times this has
    # happened. deleted_at + deleted_by refresh on every delete.
    g.db.execute(
        text(
            """
            INSERT INTO consumer_groups_tombstones
                (consumer_group_id, last_cursor_at_delete,
                 connector_id, deleted_by)
            VALUES (:cgid, :lc, :cid, :deleted_by)
            ON CONFLICT (consumer_group_id) DO UPDATE
               SET last_cursor_at_delete = EXCLUDED.last_cursor_at_delete,
                   connector_id          = EXCLUDED.connector_id,
                   deleted_at            = NOW(),
                   deleted_by            = EXCLUDED.deleted_by
            """
        ),
        {
            "cgid": row.consumer_group_id,
            "lc": row.last_cursor,
            "cid": row.connector_id,
            "deleted_by": g.current_user["username"],
        },
    )

    # v1.5.1 V-221 (#154): audit the delete with the full state
    # snapshot before the row disappears. Mirrors the V-208 delete
    # pattern for wms_tokens. The subscription snapshot lets a
    # reviewer reconstruct "what filter did this group carry at
    # deletion" without joining to tombstones or audit history.
    subscription_snapshot = row.subscription or {}
    if isinstance(subscription_snapshot, str):
        subscription_snapshot = json.loads(subscription_snapshot)
    write_audit_log(
        g.db,
        action_type=ACTION_CONSUMER_GROUP_DELETE,
        entity_type="CONSUMER_GROUP",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "consumer_group_id": row.consumer_group_id,
            "connector_id": row.connector_id,
            "last_cursor_at_delete": row.last_cursor,
            "subscription_at_delete": subscription_snapshot,
        },
    )

    g.db.commit()
    return ("", 204)
