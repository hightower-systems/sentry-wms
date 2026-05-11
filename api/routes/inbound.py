"""v1.7.0 Pipe B inbound surface.

Endpoints:

    GET  /api/v1/inbound/mapping-schema   -- documentation aid (unauthed)
    POST /api/v1/inbound/sales_orders     -- v1.7 first resource

The four remaining resource endpoints (items / customers / vendors /
purchase_orders) land in subsequent commits and reuse the same
register_inbound_resource() helper below.

Per-request shape:
- @require_wms_token: validates X-WMS-Token, refuses cross-direction
  bridging (V-200 + v1.7 cross_direction_scope_violation), checks the
  resource is in the token's inbound_resources scope.
- 413 Payload Too Large at request boundary if Content-Length exceeds
  SENTRY_INBOUND_MAX_BODY_KB (16-4096 KB; default 256).
- 422 on Pydantic body validation failure (extra='forbid' rejects
  typos at the wire).
- handle_inbound() in services.inbound_service runs the 10-step flow
  and returns a HandlerResult; the wrapper serialises into Flask
  responses with the X-Sentry-Canonical-Model: DRAFT-v1 header on
  every response (success or failure).
"""

import uuid
from datetime import timezone

from flask import Blueprint, current_app, g, jsonify, make_response, request
from psycopg2.errors import IntegrityError as _PsycopgIntegrityError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as _SAIntegrityError

from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.inbound import InboundBody
from services.audit_service import write_audit_log
from services.events_service import emit_event, resolve_source_external_id
from services.inbound_service import (
    HandlerError,
    HandlerOK,
    get_max_body_kb,
    handle_inbound,
)
from services.inventory_service import add_inventory
from services.mapping_loader import MappingDocument
from services.rate_limit import limiter
from constants import ACTION_ADJUST, ADJ_APPROVED


inbound_bp = Blueprint("inbound", __name__)


# ----------------------------------------------------------------------
# GET /api/v1/inbound/mapping-schema (documentation aid; unauthed)
# ----------------------------------------------------------------------


@inbound_bp.route("/mapping-schema", methods=["GET"])
def mapping_schema():
    response = make_response(jsonify(build_mapping_schema()))
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    return response


def build_mapping_schema() -> dict:
    """Returns the JSON Schema for the mapping document. Used by the
    route above and by the schema-regeneration script
    (tools/scripts/regenerate-mapping-schema.py) so the wire-served
    version and the committed file at
    docs/api/mapping-document-schema.json cannot drift."""
    schema = MappingDocument.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "SentryWMS Inbound Mapping Document"
    return schema


# ----------------------------------------------------------------------
# POST handler shared by every resource route
# ----------------------------------------------------------------------


def _serialise(result) -> "Response":
    """Translate HandlerResult into a Flask response. Always carries the
    DRAFT-v1 header so consumers can detect the schema-stability stage
    on every response, including 4xx."""
    if isinstance(result, HandlerOK):
        body = jsonify(result.body)
        response = make_response(body, result.status_code)
    elif isinstance(result, HandlerError):
        body = jsonify(result.body)
        response = make_response(body, result.status_code)
        if result.headers:
            for k, v in result.headers.items():
                response.headers[k] = v
    else:
        raise RuntimeError(f"unknown handler result: {result!r}")
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    return response


def _resource_post(resource_key: str):
    """Returns a Flask handler for POST /api/v1/inbound/<resource_key>.

    Body-size cap, Pydantic validation, transaction commit/rollback,
    and the DRAFT-v1 header live here so each per-resource route below
    is just a one-liner registering the right URL and Flask endpoint
    name (Flask endpoint name is what V170_INBOUND_RESOURCE_BY_ENDPOINT
    in the decorator dispatches on)."""

    def handler():
        cap_bytes = get_max_body_kb() * 1024
        if request.content_length is not None and request.content_length > cap_bytes:
            response = make_response(
                jsonify({
                    "error_kind": "body_too_large",
                    "max_body_kb": get_max_body_kb(),
                }),
                413,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response

        try:
            body = InboundBody.model_validate(request.get_json(silent=False))
        except ValidationError as exc:
            response = make_response(
                jsonify({
                    "error_kind": "validation_error",
                    "details": exc.errors(include_url=False),
                }),
                422,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response

        registry = current_app.config.get("MAPPING_REGISTRY")
        try:
            result = handle_inbound(
                db=g.db,
                resource_key=resource_key,
                body=body.model_dump(),
                token=g.current_token,
                registry=registry,
                source_txn_id=getattr(g, "source_txn_id", None),
            )
        except _SAIntegrityError as exc:
            g.db.rollback()
            response = make_response(
                jsonify({
                    "error_kind": "canonical_constraint_violation",
                    "message": _trim(str(exc.orig if exc.orig else exc)),
                }),
                422,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response
        except _PsycopgIntegrityError as exc:
            g.db.rollback()
            response = make_response(
                jsonify({
                    "error_kind": "canonical_constraint_violation",
                    "message": _trim(str(exc)),
                }),
                422,
            )
            response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
            return response
        except Exception:
            g.db.rollback()
            raise

        if isinstance(result, HandlerOK):
            g.db.commit()
        else:
            g.db.rollback()
        return _serialise(result)

    return handler


def _trim(msg: str, n: int = 400) -> str:
    s = msg.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "..."


def register_inbound_resource(resource_key: str, endpoint_suffix: str) -> None:
    """Register POST /api/v1/inbound/<resource_key> with the wms_token
    decorator + per-token rate limit. Future per-resource commits add
    one line here per resource.

    Rate limit is env-configurable via INBOUND_RATE_LIMIT_PER_MINUTE (default 500).
    Sentry can sustain much higher than the original "100 per minute" default —
    bulk pre-cutover pushes (item master, inventory seed, customer migration)
    need higher ceilings without compromising steady-state safety. Single-
    threaded pushers max out at ~12 req/sec (RTT-limited) so 500/min gives
    ~30% headroom. Bump to 1000-2000 only if running multiple concurrent
    pushers. Lower it again per-environment if you ever see resource pressure.
    """
    import os
    rate_limit = os.getenv("INBOUND_RATE_LIMIT_PER_MINUTE", "500")
    handler = _resource_post(resource_key)
    handler = with_db(handler)
    handler = require_wms_token(handler)
    handler = limiter.limit(f"{rate_limit} per minute")(handler)
    inbound_bp.add_url_rule(
        f"/{resource_key}",
        endpoint=f"post_{endpoint_suffix}",
        view_func=handler,
        methods=["POST"],
    )


register_inbound_resource("sales_orders", "sales_orders")
register_inbound_resource("items", "items")
register_inbound_resource("customers", "customers")


# ----------------------------------------------------------------------
# POST /api/v1/inbound/inventory_update
# ----------------------------------------------------------------------
#
# Token-authed bulk inventory STATE-SYNC endpoint. Used for:
#   - Cutover-day initial seed (~21K AFC + ~377 FBA + ~99 STORE)
#   - Continuous Amazon SP-API → Sentry FBA mirror
#   - Drift correction (when GL/Fabric and Sentry disagree, push our state in)
#   - Bulk corrections post-cutover
#
# State-based semantics: caller sends the DESIRED final quantity. Server
# computes the delta vs current quantity_on_hand and applies it as an
# inventory_adjustments row (so the audit trail still tells the story).
# Idempotent: pushing the same target_quantity twice is a 0-delta no-op.
#
# Body shape: standard InboundBody with source_payload containing:
#   {
#     "item_external_id": "<our SKU>",      // resolved via cross_system_mappings
#     "bin_code": "PICK-BULK",               // resolved within warehouse_id
#     "warehouse_id": 1,                     // 1=AFC, 2=FBA, 3=STORE
#     "target_quantity": 50,                 // absolute; the desired state
#     "reason_code": "cutover_seed"          // free-form, recorded in inventory_adjustments
#   }
#
# Returns 201 on apply (delta != 0) with { canonical_id, applied_delta, ... }
# Returns 200 on no-op (delta == 0) with { applied_delta: 0, current_quantity, ... }
#
# Bidirectional sync architecture:
#   GL → Sentry: this endpoint (state-based)
#   Sentry → GL: existing adjustment.applied webhook events (delta-based)


def _inventory_update_post():
    """POST /api/v1/inbound/inventory_update — token-authed state-based sync."""
    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        response = make_response(
            jsonify({"error_kind": "body_too_large", "max_body_kb": get_max_body_kb()}),
            413,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    try:
        body = InboundBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        response = make_response(
            jsonify({"error_kind": "validation_error", "details": exc.errors(include_url=False)}),
            422,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    sp = body.source_payload or {}
    required = ("item_external_id", "bin_code", "warehouse_id", "target_quantity", "reason_code")
    missing = [k for k in required if k not in sp]
    if missing:
        response = make_response(
            jsonify({"error_kind": "missing_source_payload_fields", "missing": missing}),
            422,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    try:
        item_external_id = str(sp["item_external_id"])
        bin_code = str(sp["bin_code"])
        warehouse_id = int(sp["warehouse_id"])
        target_quantity = int(sp["target_quantity"])
        reason_code = str(sp["reason_code"])
    except (TypeError, ValueError) as exc:
        response = make_response(
            jsonify({"error_kind": "invalid_payload_types", "message": str(exc)[:200]}),
            422,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    if target_quantity < 0:
        response = make_response(
            jsonify({"error_kind": "negative_target_quantity", "target_quantity": target_quantity}),
            422,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    # Resolve our SKU to Sentry's item_id via cross_system_mappings → items.
    # Token's source_system tells us which mapping namespace to look in.
    source_system = g.current_token.get("source_system") if g.current_token else None
    if not source_system:
        response = make_response(jsonify({"error_kind": "token_missing_source_system"}), 401)
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    item_row = g.db.execute(
        text("""
            SELECT i.item_id, i.sku, i.external_id
            FROM cross_system_mappings csm
            JOIN items i ON i.external_id = csm.canonical_id
            WHERE csm.source_system = :ss
              AND csm.source_type = 'item'
              AND csm.source_id = :sid
        """),
        {"ss": source_system, "sid": item_external_id},
    ).fetchone()
    if not item_row:
        response = make_response(
            jsonify({
                "error_kind": "item_not_found",
                "item_external_id": item_external_id,
                "source_system": source_system,
            }),
            404,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response
    item_id = item_row.item_id

    # Resolve bin via (warehouse_id, bin_code).
    bin_row = g.db.execute(
        text("SELECT bin_id, bin_code, external_id FROM bins WHERE warehouse_id = :wh AND bin_code = :bc"),
        {"wh": warehouse_id, "bc": bin_code},
    ).fetchone()
    if not bin_row:
        response = make_response(
            jsonify({
                "error_kind": "bin_not_found",
                "warehouse_id": warehouse_id,
                "bin_code": bin_code,
            }),
            404,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response
    bin_id = bin_row.bin_id

    # Look up current state for this (item, bin) — needed to compute delta.
    inv_row = g.db.execute(
        text("""
            SELECT inventory_id, quantity_on_hand FROM inventory
            WHERE item_id = :iid AND bin_id = :bid FOR UPDATE
        """),
        {"iid": item_id, "bid": bin_id},
    ).fetchone()
    current_quantity = inv_row.quantity_on_hand if inv_row else 0
    quantity_change = target_quantity - current_quantity

    # Idempotent no-op: target already matches current. Return 200 + current
    # state without writing an adjustment row or emitting an event.
    if quantity_change == 0:
        response = make_response(
            jsonify({
                "applied_delta": 0,
                "current_quantity": current_quantity,
                "target_quantity": target_quantity,
                "warehouse_id": warehouse_id,
                "bin_id": bin_id,
                "item_id": item_id,
                "noop": True,
            }),
            200,
        )
        response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
        return response

    # Apply ADD or REMOVE for the computed delta.
    try:
        if quantity_change > 0:
            adjustment_type = "ADD"
            add_inventory(g.db, item_id, bin_id, warehouse_id, quantity_change)
        else:
            adjustment_type = "REMOVE"
            qty_remove = abs(quantity_change)
            new_qty = current_quantity - qty_remove
            if new_qty == 0:
                g.db.execute(
                    text("DELETE FROM inventory WHERE inventory_id = :inv_id"),
                    {"inv_id": inv_row.inventory_id},
                )
            else:
                g.db.execute(
                    text("UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"),
                    {"qty": new_qty, "inv_id": inv_row.inventory_id},
                )
    except Exception:
        g.db.rollback()
        raise

    # Create the adjustment record (status=APPROVED — auto-applied via Pipe B).
    # adjusted_by is NOT NULL on inventory_adjustments. Pipe B WMS tokens are
    # system-driven and don't carry a user_id — fall back to the token's
    # creating user_id (g.current_token.get('created_by_user_id')), then to
    # 1 (admin) as last resort. Audit trail still rich because token_name +
    # source_external_id are recorded in audit_log.details.
    adjusted_by_user_id = (
        g.current_token.get("user_id")
        or g.current_token.get("created_by_user_id")
        or 1
    ) if g.current_token else 1
    adj_row = g.db.execute(
        text("""
            INSERT INTO inventory_adjustments (
                item_id, bin_id, warehouse_id, quantity_change,
                reason_code, reason_detail, status,
                adjusted_by, adjusted_at, external_id
            )
            VALUES (
                :iid, :bid, :wid, :qty_change,
                :reason_code, :reason_detail, :status,
                :user_id, NOW(), :ext_id
            )
            RETURNING adjustment_id, adjusted_at, external_id
        """),
        {
            "iid": item_id, "bid": bin_id, "wid": warehouse_id,
            "qty_change": quantity_change,
            "reason_code": reason_code[:60],
            "reason_detail": f"Pipe B inbound adjustment from upstream external_id={body.external_id}",
            "status": ADJ_APPROVED,
            "user_id": adjusted_by_user_id,
            "ext_id": str(uuid.uuid4()),
        },
    ).fetchone()

    write_audit_log(
        g.db, ACTION_ADJUST, "ITEM", item_id,
        user_id=adjusted_by_user_id,
        warehouse_id=warehouse_id,
        details={
            "adjustment_id": adj_row.adjustment_id,
            "adjustment_type": adjustment_type,
            "bin_id": bin_id,
            "quantity": quantity_change,
            "reason_code": reason_code,
            "source_external_id": body.external_id,
            "source_system": source_system,
            "token_name": g.current_token.get("token_name") if g.current_token else None,
        },
    )

    item_source_external_id = (
        resolve_source_external_id(g.db, "item", item_row.external_id)
        or str(item_row.external_id)
    )
    emit_event(
        g.db,
        event_type="adjustment.applied",
        event_version=1,
        aggregate_type="inventory_adjustment",
        aggregate_id=adj_row.adjustment_id,
        aggregate_external_id=adj_row.external_id,
        warehouse_id=warehouse_id,
        source_txn_id=getattr(g, "source_txn_id", None),
        payload={
            "adjustment_external_id": str(adj_row.external_id),
            "item_external_id": item_source_external_id,
            "bin_external_id": str(bin_row.external_id),
            "quantity_delta": quantity_change,
            "reason_code": reason_code,
            "applied_by_user_external_id": None,  # Pipe B path: no Sentry user identity
            "applied_at": adj_row.adjusted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    g.db.commit()

    response = make_response(
        jsonify({
            "canonical_id": str(adj_row.external_id),
            "adjustment_id": adj_row.adjustment_id,
            "applied_delta": quantity_change,
            "current_quantity": target_quantity,  # post-apply state
            "target_quantity": target_quantity,
            "warehouse_id": warehouse_id,
            "bin_id": bin_id,
            "item_id": item_id,
            "noop": False,
        }),
        201,
    )
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    return response


def register_inventory_update_route() -> None:
    """Register POST /api/v1/inbound/inventory_update — state-based sync."""
    import os
    rate_limit = os.getenv("INBOUND_RATE_LIMIT_PER_MINUTE", "500")
    handler = _inventory_update_post
    handler = with_db(handler)
    handler = require_wms_token(handler)
    handler = limiter.limit(f"{rate_limit} per minute")(handler)
    inbound_bp.add_url_rule(
        "/inventory_update",
        endpoint="post_inventory_update",
        view_func=handler,
        methods=["POST"],
    )


register_inventory_update_route()
register_inbound_resource("vendors", "vendors")
register_inbound_resource("purchase_orders", "purchase_orders")
