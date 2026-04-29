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

from constants import ACTION_WEBHOOK_SUBSCRIPTION_CREATE
from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.webhooks import CreateWebhookRequest
from services.audit_service import write_audit_log
from services.events_schema_registry import V150_CATALOG
from services.webhook_dispatcher import env_validator as dispatcher_env
from services.webhook_dispatcher import signing as dispatcher_signing
from services.webhook_dispatcher import ssrf_guard
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
