"""Admin CRUD for outbound webhook subscriptions (v1.6.0 #185).

This commit lands the create endpoint only; companion list / detail /
PATCH / DELETE / rotate-secret endpoints follow as separate
stepping stones. All endpoints require ADMIN role via cookie auth.

The subscription's HMAC plaintext is generated server-side, encrypted
via Fernet (SENTRY_ENCRYPTION_KEY), and stored at
``webhook_secrets.secret_ciphertext``. The plaintext is returned in
the response body exactly once; a lost plaintext means the admin
calls the rotate endpoint, no recovery path.

URL-reuse gate: an unacknowledged tombstone with the same
``delivery_url_at_delete`` as the request's ``delivery_url`` returns
409 with ``X-Sentry-URL-Reuse-Tombstone`` header listing the
tombstone_id. The admin re-submits with ``acknowledge_url_reuse:
true`` to bypass; the gate then marks the tombstone acknowledged in
the same transaction as the create. Mirrors the consumer_groups
tombstone gate pattern from v1.5.1.
"""

import os
import secrets
import uuid
from urllib.parse import urlparse

from flask import g, jsonify, request
from sqlalchemy import text

from constants import (
    ACTION_WEBHOOK_DELIVERY_REPLAY_SINGLE,
    ACTION_WEBHOOK_SECRET_ROTATE,
    ACTION_WEBHOOK_SUBSCRIPTION_CREATE,
    ACTION_WEBHOOK_SUBSCRIPTION_DELETE_HARD,
    ACTION_WEBHOOK_SUBSCRIPTION_DELETE_SOFT,
    ACTION_WEBHOOK_SUBSCRIPTION_UPDATE,
)
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.webhooks import CreateWebhookRequest, UpdateWebhookRequest
from services.audit_service import write_audit_log
from services.events_schema_registry import V150_CATALOG
from services.webhook_dispatcher import env_validator as dispatcher_env
from services.webhook_dispatcher import signing as dispatcher_signing
from services.webhook_dispatcher import ssrf_guard
from services.webhook_dispatcher import wake as dispatcher_wake
from utils.validation import validate_body


_KNOWN_EVENT_TYPES = {entry[0] for entry in V150_CATALOG}


def _row_to_listing(row, stats: dict) -> dict:
    """Serialize a webhook_subscriptions row plus its stats block
    for the admin list / detail endpoints. No plaintext secret
    material."""
    sub_filter = row.subscription_filter
    if sub_filter is None:
        sub_filter = {}
    return {
        "subscription_id": str(row.subscription_id),
        "connector_id": row.connector_id,
        "display_name": row.display_name,
        "delivery_url": row.delivery_url,
        "subscription_filter": sub_filter,
        "status": row.status,
        "pause_reason": row.pause_reason,
        "rate_limit_per_second": row.rate_limit_per_second,
        "pending_ceiling": row.pending_ceiling,
        "dlq_ceiling": row.dlq_ceiling,
        "last_delivered_event_id": row.last_delivered_event_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "stats": stats,
    }


_STATS_QUERY = text(
    """
    SELECT
        COUNT(*) FILTER (
            WHERE attempted_at >= NOW() - INTERVAL '24 hours'
        ) AS attempts_24h,
        COUNT(*) FILTER (
            WHERE attempted_at >= NOW() - INTERVAL '24 hours'
              AND status = 'succeeded'
        ) AS succeeded_24h,
        COUNT(*) FILTER (
            WHERE attempted_at >= NOW() - INTERVAL '24 hours'
              AND status = 'failed'
        ) AS failed_24h,
        COUNT(*) FILTER (
            WHERE attempted_at >= NOW() - INTERVAL '24 hours'
              AND status = 'dlq'
        ) AS dlq_24h,
        COUNT(*) FILTER (
            WHERE status IN ('pending', 'in_flight')
        ) AS pending_count
      FROM webhook_deliveries
     WHERE subscription_id = :sid
    """
)


def _stats_for(subscription_id: str) -> dict:
    row = g.db.execute(_STATS_QUERY, {"sid": subscription_id}).fetchone()
    attempts = int(row.attempts_24h or 0)
    succeeded = int(row.succeeded_24h or 0)
    success_rate = (succeeded / attempts) if attempts else None
    return {
        "attempts_24h": attempts,
        "succeeded_24h": succeeded,
        "failed_24h": int(row.failed_24h or 0),
        "dlq_24h": int(row.dlq_24h or 0),
        "success_rate_24h": success_rate,
        "pending_count": int(row.pending_count or 0),
    }


_LIST_FIELDS = """
    subscription_id, connector_id, display_name, delivery_url,
    subscription_filter, status, pause_reason, rate_limit_per_second,
    pending_ceiling, dlq_ceiling, last_delivered_event_id,
    created_at, updated_at
"""


def _http_webhooks_allowed() -> bool:
    """Mirrors the dispatcher's bool_var: only the literal 'true'
    relaxes the HTTPS-only gate. A typo cannot silently engage the
    opt-out."""
    return os.environ.get("SENTRY_ALLOW_HTTP_WEBHOOKS", "").lower() == "true"


@admin_bp.route("/webhooks", methods=["POST"])
@require_auth
@require_role("ADMIN")
@validate_body(CreateWebhookRequest)
@with_db
def create_webhook(validated):
    """Create a webhook subscription. Returns plaintext HMAC
    secret exactly once."""

    parsed_url = urlparse(validated.delivery_url)
    if parsed_url.scheme not in ("http", "https"):
        return jsonify({"error": "delivery_url must be http or https"}), 400
    if parsed_url.scheme == "http" and not _http_webhooks_allowed():
        return (
            jsonify(
                {
                    "error": "https_required",
                    "detail": (
                        "delivery_url must use https. Set "
                        "SENTRY_ALLOW_HTTP_WEBHOOKS=true to relax this in "
                        "dev / CI; production refuses the opt-out."
                    ),
                }
            ),
            400,
        )

    try:
        ssrf_guard.assert_url_safe(validated.delivery_url)
    except ssrf_guard.SsrfRejected as exc:
        return (
            jsonify(
                {
                    "error": "private_destination",
                    "detail": str(exc),
                }
            ),
            400,
        )

    pending_cap = dispatcher_env.int_var("DISPATCHER_MAX_PENDING_HARD_CAP")
    dlq_cap = dispatcher_env.int_var("DISPATCHER_MAX_DLQ_HARD_CAP")
    if validated.pending_ceiling > pending_cap:
        return (
            jsonify(
                {
                    "error": "pending_ceiling_above_hard_cap",
                    "hard_cap": pending_cap,
                }
            ),
            400,
        )
    if validated.dlq_ceiling > dlq_cap:
        return (
            jsonify(
                {
                    "error": "dlq_ceiling_above_hard_cap",
                    "hard_cap": dlq_cap,
                }
            ),
            400,
        )

    connector_row = g.db.execute(
        text("SELECT connector_id FROM connectors WHERE connector_id = :cid"),
        {"cid": validated.connector_id},
    ).fetchone()
    if connector_row is None:
        return (
            jsonify({"error": "connector_not_found", "connector_id": validated.connector_id}),
            400,
        )

    sub_filter = validated.subscription_filter
    if sub_filter.event_types:
        unknown = sorted(set(sub_filter.event_types) - _KNOWN_EVENT_TYPES)
        if unknown:
            return (
                jsonify(
                    {
                        "error": "unknown_event_types",
                        "unknown": unknown,
                        "valid": sorted(_KNOWN_EVENT_TYPES),
                    }
                ),
                400,
            )
    if sub_filter.warehouse_ids:
        rows = g.db.execute(
            text(
                "SELECT warehouse_id FROM warehouses "
                " WHERE warehouse_id = ANY(:ids)"
            ),
            {"ids": list(sub_filter.warehouse_ids)},
        ).fetchall()
        found = {r.warehouse_id for r in rows}
        missing = sorted(set(sub_filter.warehouse_ids) - found)
        if missing:
            return (
                jsonify(
                    {
                        "error": "unknown_warehouse_ids",
                        "missing": missing,
                    }
                ),
                400,
            )

    tombstone_row = g.db.execute(
        text(
            """
            SELECT tombstone_id
              FROM webhook_subscriptions_tombstones
             WHERE delivery_url_at_delete = :url
               AND acknowledged_at IS NULL
             ORDER BY tombstone_id DESC
             LIMIT 1
            """
        ),
        {"url": validated.delivery_url},
    ).fetchone()
    if tombstone_row is not None and not validated.acknowledge_url_reuse:
        response = jsonify(
            {
                "error": "url_reuse_tombstone",
                "tombstone_id": int(tombstone_row.tombstone_id),
                "detail": (
                    "this delivery_url was associated with a previously-"
                    "deleted subscription. Re-submit with "
                    "acknowledge_url_reuse=true to confirm intentional "
                    "reuse."
                ),
            }
        )
        response.status_code = 409
        response.headers["X-Sentry-URL-Reuse-Tombstone"] = str(
            int(tombstone_row.tombstone_id)
        )
        return response

    plaintext = secrets.token_urlsafe(32).encode("utf-8")
    fernet = dispatcher_signing._get_fernet()  # noqa: SLF001
    ciphertext = fernet.encrypt(plaintext)

    filter_json = sub_filter.model_dump_json(exclude_none=True)

    inserted = g.db.execute(
        text(
            """
            INSERT INTO webhook_subscriptions
                (connector_id, display_name, delivery_url,
                 subscription_filter, rate_limit_per_second,
                 pending_ceiling, dlq_ceiling)
            VALUES (:connector_id, :display_name, :delivery_url,
                    CAST(:subscription_filter AS jsonb),
                    :rate, :pending_ceiling, :dlq_ceiling)
            RETURNING subscription_id, status, created_at
            """
        ),
        {
            "connector_id": validated.connector_id,
            "display_name": validated.display_name,
            "delivery_url": validated.delivery_url,
            "subscription_filter": filter_json,
            "rate": validated.rate_limit_per_second,
            "pending_ceiling": validated.pending_ceiling,
            "dlq_ceiling": validated.dlq_ceiling,
        },
    ).fetchone()
    subscription_id = str(inserted.subscription_id)

    g.db.execute(
        text(
            """
            INSERT INTO webhook_secrets
                (subscription_id, generation, secret_ciphertext)
            VALUES (:sid, 1, :ciphertext)
            """
        ),
        {"sid": subscription_id, "ciphertext": ciphertext},
    )

    if tombstone_row is not None:
        g.db.execute(
            text(
                """
                UPDATE webhook_subscriptions_tombstones
                   SET acknowledged_at = NOW(),
                       acknowledged_by = :uid
                 WHERE tombstone_id = :tid
                """
            ),
            {
                "uid": g.current_user["user_id"],
                "tid": int(tombstone_row.tombstone_id),
            },
        )

    write_audit_log(
        g.db,
        action_type=ACTION_WEBHOOK_SUBSCRIPTION_CREATE,
        entity_type="WEBHOOK_SUBSCRIPTION",
        entity_id=0,  # entity_id is INT; the UUID lives in details.
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "subscription_id": subscription_id,
            "connector_id": validated.connector_id,
            "display_name": validated.display_name,
            "delivery_url": validated.delivery_url,
            "subscription_filter": sub_filter.model_dump(
                mode="json", exclude_none=True
            ),
            "rate_limit_per_second": validated.rate_limit_per_second,
            "pending_ceiling": validated.pending_ceiling,
            "dlq_ceiling": validated.dlq_ceiling,
            "acknowledged_url_reuse_tombstone_id": (
                int(tombstone_row.tombstone_id) if tombstone_row else None
            ),
        },
    )

    g.db.commit()
    return (
        jsonify(
            {
                "subscription_id": subscription_id,
                "connector_id": validated.connector_id,
                "display_name": validated.display_name,
                "delivery_url": validated.delivery_url,
                "status": inserted.status,
                "created_at": inserted.created_at.isoformat(),
                "rate_limit_per_second": validated.rate_limit_per_second,
                "pending_ceiling": validated.pending_ceiling,
                "dlq_ceiling": validated.dlq_ceiling,
                "secret": plaintext.decode("utf-8"),
                "secret_generation": 1,
            }
        ),
        201,
    )


@admin_bp.route("/webhooks", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_webhooks():
    """List every webhook subscription with a 24h stats rollup."""
    rows = g.db.execute(
        text(
            f"""
            SELECT {_LIST_FIELDS}
              FROM webhook_subscriptions
             ORDER BY created_at DESC
            """
        )
    ).fetchall()
    return jsonify(
        {
            "webhooks": [
                _row_to_listing(row, _stats_for(str(row.subscription_id)))
                for row in rows
            ]
        }
    )


@admin_bp.route("/webhooks/<subscription_id>", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def get_webhook(subscription_id):
    """Detail view for a single webhook subscription."""
    try:
        uuid.UUID(subscription_id)
    except ValueError:
        return jsonify({"error": "invalid_subscription_id"}), 400

    row = g.db.execute(
        text(
            f"""
            SELECT {_LIST_FIELDS}
              FROM webhook_subscriptions
             WHERE subscription_id = :sid
            """
        ),
        {"sid": subscription_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "subscription_not_found"}), 404
    return jsonify(_row_to_listing(row, _stats_for(subscription_id)))


@admin_bp.route("/webhooks/<subscription_id>", methods=["PATCH"])
@require_auth
@require_role("ADMIN")
@validate_body(UpdateWebhookRequest)
@with_db
def update_webhook(validated, subscription_id):
    """Partial update for a webhook subscription. Each mutated
    field that affects dispatch behavior publishes the matching
    event on the cross-worker pubsub channel after commit."""
    try:
        uuid.UUID(subscription_id)
    except ValueError:
        return jsonify({"error": "invalid_subscription_id"}), 400

    current = g.db.execute(
        text(
            f"""
            SELECT {_LIST_FIELDS}, pause_reason
              FROM webhook_subscriptions
             WHERE subscription_id = :sid
             FOR UPDATE
            """
        ),
        {"sid": subscription_id},
    ).fetchone()
    if current is None:
        return jsonify({"error": "subscription_not_found"}), 404

    if validated.delivery_url is not None:
        parsed = urlparse(validated.delivery_url)
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "delivery_url must be http or https"}), 400
        if parsed.scheme == "http" and not _http_webhooks_allowed():
            return jsonify({"error": "https_required"}), 400
        try:
            ssrf_guard.assert_url_safe(validated.delivery_url)
        except ssrf_guard.SsrfRejected as exc:
            return (
                jsonify({"error": "private_destination", "detail": str(exc)}),
                400,
            )

    if validated.pending_ceiling is not None:
        cap = dispatcher_env.int_var("DISPATCHER_MAX_PENDING_HARD_CAP")
        if validated.pending_ceiling > cap:
            return (
                jsonify(
                    {"error": "pending_ceiling_above_hard_cap", "hard_cap": cap}
                ),
                400,
            )
    if validated.dlq_ceiling is not None:
        cap = dispatcher_env.int_var("DISPATCHER_MAX_DLQ_HARD_CAP")
        if validated.dlq_ceiling > cap:
            return (
                jsonify(
                    {"error": "dlq_ceiling_above_hard_cap", "hard_cap": cap}
                ),
                400,
            )

    if validated.subscription_filter is not None:
        sub_filter = validated.subscription_filter
        if sub_filter.event_types:
            unknown = sorted(set(sub_filter.event_types) - _KNOWN_EVENT_TYPES)
            if unknown:
                return (
                    jsonify(
                        {
                            "error": "unknown_event_types",
                            "unknown": unknown,
                            "valid": sorted(_KNOWN_EVENT_TYPES),
                        }
                    ),
                    400,
                )
        if sub_filter.warehouse_ids:
            rows = g.db.execute(
                text(
                    "SELECT warehouse_id FROM warehouses "
                    " WHERE warehouse_id = ANY(:ids)"
                ),
                {"ids": list(sub_filter.warehouse_ids)},
            ).fetchall()
            found = {r.warehouse_id for r in rows}
            missing = sorted(set(sub_filter.warehouse_ids) - found)
            if missing:
                return (
                    jsonify({"error": "unknown_warehouse_ids", "missing": missing}),
                    400,
                )

    # Build the SET clause + params + diff for the audit log only
    # for fields the request actually included. Fields that are
    # absent from the body do not appear in the diff.
    set_clauses: list[str] = []
    params: dict = {"sid": subscription_id}
    diff: dict = {}
    pubsub_events: list[str] = []

    def _record(column: str, before, after):
        diff[column] = {"before": before, "after": after}

    if validated.display_name is not None and validated.display_name != current.display_name:
        set_clauses.append("display_name = :display_name")
        params["display_name"] = validated.display_name
        _record("display_name", current.display_name, validated.display_name)

    if validated.delivery_url is not None and validated.delivery_url != current.delivery_url:
        set_clauses.append("delivery_url = :delivery_url")
        params["delivery_url"] = validated.delivery_url
        _record("delivery_url", current.delivery_url, validated.delivery_url)
        pubsub_events.append("delivery_url_changed")

    if validated.subscription_filter is not None:
        new_filter_dump = validated.subscription_filter.model_dump(
            mode="json", exclude_none=True
        )
        old_filter = current.subscription_filter or {}
        if new_filter_dump != old_filter:
            set_clauses.append(
                "subscription_filter = CAST(:subscription_filter AS jsonb)"
            )
            params["subscription_filter"] = (
                validated.subscription_filter.model_dump_json(exclude_none=True)
            )
            _record("subscription_filter", old_filter, new_filter_dump)

    if (
        validated.rate_limit_per_second is not None
        and validated.rate_limit_per_second != current.rate_limit_per_second
    ):
        set_clauses.append("rate_limit_per_second = :rate")
        params["rate"] = validated.rate_limit_per_second
        _record(
            "rate_limit_per_second",
            current.rate_limit_per_second,
            validated.rate_limit_per_second,
        )
        pubsub_events.append("rate_limit_changed")

    if (
        validated.pending_ceiling is not None
        and validated.pending_ceiling != current.pending_ceiling
    ):
        set_clauses.append("pending_ceiling = :pending_ceiling")
        params["pending_ceiling"] = validated.pending_ceiling
        _record(
            "pending_ceiling",
            current.pending_ceiling,
            validated.pending_ceiling,
        )

    if (
        validated.dlq_ceiling is not None
        and validated.dlq_ceiling != current.dlq_ceiling
    ):
        set_clauses.append("dlq_ceiling = :dlq_ceiling")
        params["dlq_ceiling"] = validated.dlq_ceiling
        _record("dlq_ceiling", current.dlq_ceiling, validated.dlq_ceiling)

    if validated.status is not None and validated.status != current.status:
        if current.status == "revoked":
            return (
                jsonify(
                    {
                        "error": "cannot_modify_revoked_subscription",
                        "detail": (
                            "this subscription is revoked. Status changes "
                            "out of revoked are not supported via PATCH; "
                            "create a new subscription instead."
                        ),
                    }
                ),
                400,
            )
        set_clauses.append("status = :status")
        params["status"] = validated.status
        _record("status", current.status, validated.status)
        if validated.status == "paused":
            set_clauses.append("pause_reason = 'manual'")
            _record("pause_reason", current.pause_reason, "manual")
            pubsub_events.append("paused")
        else:  # 'active'
            set_clauses.append("pause_reason = NULL")
            _record("pause_reason", current.pause_reason, None)
            pubsub_events.append("resumed")

    if not set_clauses:
        # Empty body, or every supplied field already matched the
        # persisted value. No mutation, no audit row, no publish.
        return jsonify(_row_to_listing(current, _stats_for(subscription_id)))

    set_clauses.append("updated_at = NOW()")
    g.db.execute(
        text(
            f"""
            UPDATE webhook_subscriptions
               SET {", ".join(set_clauses)}
             WHERE subscription_id = :sid
            """
        ),
        params,
    )

    write_audit_log(
        g.db,
        action_type=ACTION_WEBHOOK_SUBSCRIPTION_UPDATE,
        entity_type="WEBHOOK_SUBSCRIPTION",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={"subscription_id": subscription_id, "diff": diff},
    )

    g.db.commit()

    redis_url = os.environ.get("REDIS_URL")
    for event in pubsub_events:
        dispatcher_wake.publish_subscription_event(
            redis_url, subscription_id, event
        )

    refreshed = g.db.execute(
        text(
            f"""
            SELECT {_LIST_FIELDS}
              FROM webhook_subscriptions
             WHERE subscription_id = :sid
            """
        ),
        {"sid": subscription_id},
    ).fetchone()
    return jsonify(_row_to_listing(refreshed, _stats_for(subscription_id)))


@admin_bp.route("/webhooks/<subscription_id>/rotate-secret", methods=["POST"])
@require_auth
@require_role("ADMIN")
@with_db
def rotate_webhook_secret(subscription_id):
    """Rotate the HMAC plaintext for a subscription. The previous
    primary becomes generation=2 with a 24h expires_at; the new
    plaintext lands as generation=1 and is returned exactly once.
    """
    try:
        uuid.UUID(subscription_id)
    except ValueError:
        return jsonify({"error": "invalid_subscription_id"}), 400

    sub_row = g.db.execute(
        text(
            "SELECT subscription_id, status FROM webhook_subscriptions "
            "WHERE subscription_id = :sid FOR UPDATE"
        ),
        {"sid": subscription_id},
    ).fetchone()
    if sub_row is None:
        return jsonify({"error": "subscription_not_found"}), 404
    if sub_row.status == "revoked":
        return (
            jsonify(
                {
                    "error": "cannot_rotate_revoked_subscription",
                    "detail": (
                        "the subscription is revoked. Resume / un-revoke "
                        "before rotating; a revoked subscription is not "
                        "actively dispatching."
                    ),
                }
            ),
            400,
        )

    # Step 1: drop the older "old" key. There can be at most one
    # gen=2 row per subscription (PK constraint); if it exists,
    # its 24h dual-accept window has already started and a second
    # rotation supersedes it.
    g.db.execute(
        text(
            "DELETE FROM webhook_secrets WHERE subscription_id = :sid "
            "AND generation = 2"
        ),
        {"sid": subscription_id},
    )

    # Step 2: demote the current primary (gen=1) to gen=2 with a
    # 24h dual-accept window. Consumers verify against either
    # generation until expires_at; the dispatcher signs with
    # gen=1 from now on.
    demoted_existed = g.db.execute(
        text(
            """
            UPDATE webhook_secrets
               SET generation = 2,
                   expires_at = NOW() + INTERVAL '24 hours'
             WHERE subscription_id = :sid
               AND generation = 1
            """
        ),
        {"sid": subscription_id},
    ).rowcount

    # Step 3: insert the new primary.
    plaintext = secrets.token_urlsafe(32).encode("utf-8")
    fernet = dispatcher_signing._get_fernet()  # noqa: SLF001
    ciphertext = fernet.encrypt(plaintext)
    g.db.execute(
        text(
            """
            INSERT INTO webhook_secrets
                (subscription_id, generation, secret_ciphertext, expires_at)
            VALUES (:sid, 1, :ciphertext, NULL)
            """
        ),
        {"sid": subscription_id, "ciphertext": ciphertext},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_WEBHOOK_SECRET_ROTATE,
        entity_type="WEBHOOK_SUBSCRIPTION",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "subscription_id": subscription_id,
            "demoted_prior_primary": bool(demoted_existed),
        },
    )

    g.db.commit()

    # Notify peer dispatcher workers so they reload secret material
    # for the next dispatch cycle. Soft-fail if Redis is unavailable;
    # workers also pick up the change on the 60s subscription
    # refresh cycle.
    dispatcher_wake.publish_subscription_event(
        os.environ.get("REDIS_URL"),
        subscription_id,
        "secret_rotated",
    )

    return (
        jsonify(
            {
                "subscription_id": subscription_id,
                "secret": plaintext.decode("utf-8"),
                "secret_generation": 1,
            }
        ),
        200,
    )


@admin_bp.route("/webhooks/<subscription_id>", methods=["DELETE"])
@require_auth
@require_role("ADMIN")
@with_db
def delete_webhook(subscription_id):
    """Soft delete (default) or hard delete (?purge=true).

    Soft delete flips status to 'revoked' and clears pause_reason;
    the row stays so historical webhook_deliveries keep their FK
    target. Hard delete removes the row and writes a tombstone;
    refuses with 409 when any pending / in_flight delivery exists
    so the RESTRICT FK on webhook_deliveries.subscription_id is
    not the failure surface.
    """
    try:
        uuid.UUID(subscription_id)
    except ValueError:
        return jsonify({"error": "invalid_subscription_id"}), 400

    purge = request.args.get("purge", "").lower() == "true"

    current = g.db.execute(
        text(
            """
            SELECT subscription_id, connector_id, delivery_url, status
              FROM webhook_subscriptions
             WHERE subscription_id = :sid
             FOR UPDATE
            """
        ),
        {"sid": subscription_id},
    ).fetchone()
    if current is None:
        return jsonify({"error": "subscription_not_found"}), 404

    redis_url = os.environ.get("REDIS_URL")

    if purge:
        live = g.db.execute(
            text(
                """
                SELECT COUNT(*) AS n
                  FROM webhook_deliveries
                 WHERE subscription_id = :sid
                   AND status IN ('pending', 'in_flight')
                """
            ),
            {"sid": subscription_id},
        ).fetchone()
        if int(live.n or 0) > 0:
            return (
                jsonify(
                    {
                        "error": "live_deliveries_block_hard_delete",
                        "live_count": int(live.n),
                        "detail": (
                            "the subscription has pending or in_flight "
                            "delivery rows; hard delete is refused while "
                            "any live row references it. Soft-delete to "
                            "stop dispatch, then re-issue with ?purge=true "
                            "after the deliveries terminate."
                        ),
                    }
                ),
                409,
            )

        tombstone_row = g.db.execute(
            text(
                """
                INSERT INTO webhook_subscriptions_tombstones
                    (subscription_id, delivery_url_at_delete,
                     connector_id, deleted_by)
                VALUES (:sid, :url, :connector_id, :uid)
                RETURNING tombstone_id
                """
            ),
            {
                "sid": subscription_id,
                "url": current.delivery_url,
                "connector_id": current.connector_id,
                "uid": g.current_user["user_id"],
            },
        ).fetchone()
        tombstone_id = int(tombstone_row.tombstone_id)

        g.db.execute(
            text(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = :sid"
            ),
            {"sid": subscription_id},
        )

        write_audit_log(
            g.db,
            action_type=ACTION_WEBHOOK_SUBSCRIPTION_DELETE_HARD,
            entity_type="WEBHOOK_SUBSCRIPTION",
            entity_id=0,
            user_id=g.current_user["username"],
            warehouse_id=None,
            details={
                "subscription_id": subscription_id,
                "delivery_url": current.delivery_url,
                "connector_id": current.connector_id,
                "status_before": current.status,
                "tombstone_id": tombstone_id,
            },
        )

        g.db.commit()

        dispatcher_wake.publish_subscription_event(
            redis_url, subscription_id, "deleted"
        )
        return jsonify(
            {
                "subscription_id": subscription_id,
                "purged": True,
                "tombstone_id": tombstone_id,
            }
        )

    # Soft delete path
    if current.status == "revoked":
        # Idempotent: a second soft delete on an already-revoked
        # subscription returns 200 without writing a new audit
        # row or publishing pubsub. The status is already terminal
        # and the dispatcher is already evicted.
        return jsonify(
            {"subscription_id": subscription_id, "purged": False, "status": "revoked"}
        )

    g.db.execute(
        text(
            """
            UPDATE webhook_subscriptions
               SET status = 'revoked',
                   pause_reason = NULL,
                   updated_at = NOW()
             WHERE subscription_id = :sid
            """
        ),
        {"sid": subscription_id},
    )

    write_audit_log(
        g.db,
        action_type=ACTION_WEBHOOK_SUBSCRIPTION_DELETE_SOFT,
        entity_type="WEBHOOK_SUBSCRIPTION",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "subscription_id": subscription_id,
            "delivery_url": current.delivery_url,
            "connector_id": current.connector_id,
            "status_before": current.status,
        },
    )

    g.db.commit()

    dispatcher_wake.publish_subscription_event(
        redis_url, subscription_id, "deleted"
    )
    return jsonify(
        {
            "subscription_id": subscription_id,
            "purged": False,
            "status": "revoked",
        }
    )


_DLQ_LIMIT_MAX = 500
_DLQ_LIMIT_DEFAULT = 50


@admin_bp.route("/webhooks/<subscription_id>/dlq", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_dlq(subscription_id):
    """Paginated DLQ viewer. Returns the dead-letter delivery
    rows for the subscription joined with the source
    integration_events context so the operator can read what
    payload failed without a second round-trip."""
    try:
        uuid.UUID(subscription_id)
    except ValueError:
        return jsonify({"error": "invalid_subscription_id"}), 400

    try:
        limit = int(request.args.get("limit", _DLQ_LIMIT_DEFAULT))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "invalid_pagination"}), 400
    if limit < 1 or limit > _DLQ_LIMIT_MAX or offset < 0:
        return (
            jsonify(
                {
                    "error": "invalid_pagination",
                    "detail": (
                        f"limit must be in [1, {_DLQ_LIMIT_MAX}]; "
                        f"offset must be >= 0"
                    ),
                }
            ),
            400,
        )

    sub_row = g.db.execute(
        text("SELECT 1 FROM webhook_subscriptions WHERE subscription_id = :sid"),
        {"sid": subscription_id},
    ).fetchone()
    if sub_row is None:
        return jsonify({"error": "subscription_not_found"}), 404

    total = int(
        g.db.execute(
            text(
                """
                SELECT COUNT(*) AS n
                  FROM webhook_deliveries
                 WHERE subscription_id = :sid
                   AND status = 'dlq'
                """
            ),
            {"sid": subscription_id},
        ).fetchone().n
        or 0
    )

    rows = g.db.execute(
        text(
            """
            SELECT d.delivery_id, d.event_id, d.attempt_number,
                   d.http_status, d.error_kind, d.error_detail,
                   d.attempted_at, d.completed_at, d.scheduled_at,
                   d.secret_generation,
                   e.event_type, e.event_timestamp,
                   e.aggregate_external_id, e.warehouse_id,
                   e.source_txn_id
              FROM webhook_deliveries d
              LEFT JOIN integration_events e ON e.event_id = d.event_id
             WHERE d.subscription_id = :sid
               AND d.status = 'dlq'
             ORDER BY d.completed_at DESC, d.delivery_id DESC
             LIMIT :limit OFFSET :offset
            """
        ),
        {"sid": subscription_id, "limit": limit, "offset": offset},
    ).fetchall()

    deliveries = [
        {
            "delivery_id": int(r.delivery_id),
            "event_id": int(r.event_id) if r.event_id is not None else None,
            "attempt_number": int(r.attempt_number),
            "http_status": (
                int(r.http_status) if r.http_status is not None else None
            ),
            "error_kind": r.error_kind,
            "error_detail": r.error_detail,
            "attempted_at": (
                r.attempted_at.isoformat() if r.attempted_at else None
            ),
            "completed_at": (
                r.completed_at.isoformat() if r.completed_at else None
            ),
            "scheduled_at": (
                r.scheduled_at.isoformat() if r.scheduled_at else None
            ),
            "secret_generation": int(r.secret_generation),
            "event": {
                "event_type": r.event_type,
                "event_timestamp": (
                    r.event_timestamp.isoformat() if r.event_timestamp else None
                ),
                "aggregate_external_id": (
                    str(r.aggregate_external_id)
                    if r.aggregate_external_id is not None
                    else None
                ),
                "warehouse_id": (
                    int(r.warehouse_id) if r.warehouse_id is not None else None
                ),
                "source_txn_id": r.source_txn_id,
            },
        }
        for r in rows
    ]

    return jsonify(
        {
            "subscription_id": subscription_id,
            "deliveries": deliveries,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@admin_bp.route(
    "/webhooks/<subscription_id>/replay/<int:delivery_id>", methods=["POST"]
)
@require_auth
@require_role("ADMIN")
@with_db
def replay_single(subscription_id, delivery_id):
    """Replay one delivery by INSERTing a fresh pending row that
    points at the original event_id. The original row stays put
    as the audit trail; the subscription cursor is NOT touched."""
    try:
        uuid.UUID(subscription_id)
    except ValueError:
        return jsonify({"error": "invalid_subscription_id"}), 400

    sub_row = g.db.execute(
        text(
            "SELECT subscription_id, status FROM webhook_subscriptions "
            "WHERE subscription_id = :sid"
        ),
        {"sid": subscription_id},
    ).fetchone()
    if sub_row is None:
        return jsonify({"error": "subscription_not_found"}), 404

    original = g.db.execute(
        text(
            """
            SELECT delivery_id, subscription_id, event_id, status
              FROM webhook_deliveries
             WHERE delivery_id = :did
            """
        ),
        {"did": delivery_id},
    ).fetchone()
    if original is None:
        return jsonify({"error": "delivery_not_found"}), 404

    if str(original.subscription_id) != subscription_id:
        # URL-tampering check: a delivery_id that exists but
        # belongs to a different subscription is rejected with
        # the same shape an admin would see for any cross-
        # subscription scope violation. Does not echo the actual
        # owner.
        return (
            jsonify(
                {
                    "error": "delivery_subscription_mismatch",
                    "detail": (
                        "delivery_id does not belong to the subscription "
                        "in the URL path."
                    ),
                }
            ),
            400,
        )

    if sub_row.status == "revoked":
        return (
            jsonify(
                {
                    "error": "cannot_replay_to_revoked_subscription",
                    "detail": (
                        "the subscription is revoked. Resume / un-revoke "
                        "before replaying; a revoked subscription is not "
                        "actively dispatching."
                    ),
                }
            ),
            400,
        )

    inserted = g.db.execute(
        text(
            """
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, secret_generation)
            VALUES (:sid, :event_id, 1, 'pending', NOW(), 1)
            RETURNING delivery_id
            """
        ),
        {"sid": subscription_id, "event_id": original.event_id},
    ).fetchone()

    write_audit_log(
        g.db,
        action_type=ACTION_WEBHOOK_DELIVERY_REPLAY_SINGLE,
        entity_type="WEBHOOK_SUBSCRIPTION",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "subscription_id": subscription_id,
            "original_delivery_id": int(original.delivery_id),
            "replayed_delivery_id": int(inserted.delivery_id),
            "event_id": (
                int(original.event_id) if original.event_id is not None else None
            ),
            "original_status": original.status,
        },
    )

    g.db.commit()
    return (
        jsonify(
            {
                "subscription_id": subscription_id,
                "original_delivery_id": int(original.delivery_id),
                "replayed_delivery_id": int(inserted.delivery_id),
            }
        ),
        201,
    )
