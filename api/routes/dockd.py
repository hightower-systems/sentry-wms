"""v1.9.0 dockd shipping surface.

Endpoints (this commit):

    GET  /api/v1/dockd/orders/<so_number>        -- load-on-scan
    POST /api/v1/dockd/orders/<so_number>/ship   -- record a ship

The remaining POST route (void-ship) lands in v1.9 #6.

Per-request shape:
- @require_wms_token: validates X-WMS-Token, refuses cross-direction
  bridging, refuses tokens without dockd.dispatch in endpoints. The
  decorator's V190 dispatcher branch is what gates this surface.
- @limiter.limit per route; replays (X-Idempotent-Replay: true) do
  NOT count against the budget on POST routes.
- @with_db opens the request-scoped SQLAlchemy session.
- Every response (success and failure) carries
  X-Sentry-Canonical-Model: DRAFT-v1 so consumers can detect schema-
  stability stage on each response, including 4xx.
- Standard error body shape:
      {"error_kind": str, "message": str, "details": {}}

Path parameter validation:
- so_number is matched against ^[A-Za-z0-9_\\-#.]+$ length 1..128 before
  any DB query so a malformed value never reaches the parameterized
  binding. The regex is defense-in-depth on top of SQLAlchemy's bind
  parameter handling.
"""

import re
from datetime import timezone

from flask import Blueprint, g, jsonify, make_response, request
from psycopg2.errors import LockNotAvailable, QueryCanceled
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from constants import SO_PICKED, SO_PACKED, SO_SHIPPED
from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.dockd import ShipBody, VoidShipBody
from services.dockd_service import canonical_body_sha256, get_max_body_kb
from services.events_service import get_user_external_id
from services.rate_limit import limiter
from services.shipping_service import (
    record_ship,
    record_void_ship,
    require_packing_before_shipping,
)


dockd_bp = Blueprint("dockd", __name__)


# Path-parameter regex. Mirrors the existing inbound so_number Pydantic
# regex; the dockd surface accepts the same shape so a Fabric-pushed
# order number always loads on a dockd scan.
_SO_NUMBER_RE = re.compile(r"^[A-Za-z0-9_\-#.]+$")
_SO_NUMBER_MAX_LEN = 128


def _draft_response(body, status_code=200, extra_headers=None):
    """Build a Flask response carrying the dockd canonical-model header
    on every response (success or failure)."""
    response = make_response(jsonify(body), status_code)
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    if extra_headers:
        for k, v in extra_headers.items():
            response.headers[k] = v
    return response


def _err(error_kind, message, status_code, details=None):
    return _draft_response(
        {
            "error_kind": error_kind,
            "message": message,
            "details": details or {},
        },
        status_code,
    )


def _validate_so_number(so_number):
    """Returns None if valid, otherwise an error response."""
    if not so_number or len(so_number) > _SO_NUMBER_MAX_LEN:
        return _err("invalid_so_number", "so_number length out of range", 422)
    if not _SO_NUMBER_RE.match(so_number):
        return _err("invalid_so_number", "so_number contains disallowed characters", 422)
    return None


@dockd_bp.route("/orders/<so_number>", methods=["GET"])
@require_wms_token
@limiter.limit("60 per minute")
@with_db
def get_order(so_number):
    """Load-on-scan dockd read.

    Returns the SO + structured shipping address + items + ship-state
    fields so dockd's UI can render either the "ready to ship" or the
    "already shipped, want to void?" branch off a single call.

    404 not_found is returned for both genuinely-unknown so_numbers AND
    for orders outside the token's warehouse scope. Conflating the two
    prevents an enumeration oracle (the token cannot tell a missing
    order from one in a sibling warehouse).
    """
    err = _validate_so_number(so_number)
    if err is not None:
        return err

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])
    if not token_warehouse_ids:
        return _err("not_found", "order not found", 404)

    so = g.db.execute(
        text(
            """
            SELECT so_id, so_number, external_id, status, warehouse_id,
                   customer_name, customer_phone, ship_method, memo,
                   shipping_address_name, shipping_address_line1,
                   shipping_address_line2, shipping_address_city,
                   shipping_address_state, shipping_address_postal_code,
                   shipping_address_country, shipping_address_phone,
                   order_total, customer_shipping_paid, created_by,
                   order_date, created_at, shipped_at, carrier,
                   tracking_number
              FROM sales_orders
             WHERE so_number = :so_number
               AND warehouse_id = ANY(:wh_ids)
             LIMIT 1
            """
        ),
        {"so_number": so_number, "wh_ids": token_warehouse_ids},
    ).fetchone()

    if not so:
        return _err("not_found", "order not found", 404)

    items = g.db.execute(
        text(
            """
            SELECT i.external_id AS item_external_id, i.sku, i.item_name,
                   i.upc, sol.quantity_picked AS qty
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :so_id
             ORDER BY sol.line_number
            """
        ),
        {"so_id": so.so_id},
    ).fetchall()

    packing_required = require_packing_before_shipping(g.db)
    shippable_from_statuses = (
        [SO_PACKED] if packing_required else [SO_PICKED, SO_PACKED]
    )
    shippable = so.status in shippable_from_statuses

    shipped_by = None
    if so.status == SO_SHIPPED:
        ff = g.db.execute(
            text(
                """
                SELECT shipped_by
                  FROM item_fulfillments
                 WHERE so_id = :so_id AND status = 'SHIPPED'
                 ORDER BY shipped_at DESC NULLS LAST
                 LIMIT 1
                """
            ),
            {"so_id": so.so_id},
        ).fetchone()
        if ff:
            shipped_by = ff.shipped_by

    body = {
        "so_number": so.so_number,
        "external_id": str(so.external_id),
        "status": so.status,
        "warehouse_id": so.warehouse_id,
        "customer_name": so.customer_name,
        "customer_phone": so.customer_phone,
        "memo": so.memo,
        "shipping_address": {
            "name": so.shipping_address_name,
            "line1": so.shipping_address_line1,
            "line2": so.shipping_address_line2,
            "city": so.shipping_address_city,
            "state": so.shipping_address_state,
            "postal_code": so.shipping_address_postal_code,
            "country": so.shipping_address_country,
            "phone": so.shipping_address_phone,
        },
        "ship_method": so.ship_method,
        "items": [
            {
                "external_id": str(it.item_external_id),
                "sku": it.sku,
                "display_name": it.item_name,
                "upc": it.upc,
                "qty": it.qty,
            }
            for it in items
        ],
        "order_total": float(so.order_total) if so.order_total is not None else None,
        "customer_shipping_paid": (
            float(so.customer_shipping_paid)
            if so.customer_shipping_paid is not None
            else None
        ),
        "marketplace": so.created_by,
        "order_date": so.order_date.isoformat() if so.order_date else None,
        "ff_created_at": so.created_at.isoformat() if so.created_at else None,
        "shippable": shippable,
        "shippable_from_statuses": shippable_from_statuses,
        "shipped_by": shipped_by,
        "tracking_number": so.tracking_number,
        "carrier": so.carrier,
        "shipped_at": so.shipped_at.isoformat() if so.shipped_at else None,
        # station_label is a UX aspiration documented in the dockd plan;
        # the queryable path (token-id JOIN on item_fulfillments) is not
        # yet wired. v1.9 returns null; the dockd UI degrades the
        # "Shipped by Mike at Pack Station 3" line to "Shipped by Mike"
        # until a follow-up commit lands the column.
        "station_label": None,
    }
    return _draft_response(body, 200)


# ----------------------------------------------------------------------
# POST /api/v1/dockd/orders/<so_number>/ship
# ----------------------------------------------------------------------


_SHIP_ENDPOINT = "ship"


def _replay_response(cached_row, headers=None):
    """Return the cached ship response with X-Idempotent-Replay: true.
    Used both by the warm-cache short-circuit (step 0) and by the
    racing-peer-committed branch inside the transaction."""
    extra = {"X-Idempotent-Replay": "true"}
    if headers:
        extra.update(headers)
    return _draft_response(
        cached_row.response_body,
        cached_row.response_status,
        extra_headers=extra,
    )


@dockd_bp.route("/orders/<so_number>/ship", methods=["POST"])
@require_wms_token
@limiter.limit("30 per minute", exempt_when=lambda: getattr(g, "_dockd_replay_hit", False))
@with_db
def ship_order(so_number):
    """Record a successful ship.

    See sentry-dockd-integration-sentry-side.md ("POST /ship") for the
    full contract. Order of operations:

      0. Body-cap check + Pydantic parse + body-hash compute.
      0a. Out-of-transaction replay short-circuit on warm cache hit.
      1. BEGIN; SET LOCAL lock_timeout = '5s'.
      2. Sentinel INSERT into dockd_idempotency ON CONFLICT DO NOTHING.
         Conflict + body matches -> replay; conflict + body differs -> 409.
      3. SELECT ... FOR UPDATE on sales_orders.
      4. Status gate (PICKED/PACKED only, depending on packing setting).
      5. Hand off to shipping_service.record_ship which performs the
         shared writes (fulfillment, lines, SO update, audit, outbox).
      6. UPDATE dockd_idempotency with response_body / response_status.
      7. COMMIT.
    """
    err = _validate_so_number(so_number)
    if err is not None:
        return err

    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        return _err(
            "body_too_large",
            "request body exceeds SENTRY_DOCKD_MAX_BODY_KB",
            413,
            {"max_body_kb": get_max_body_kb()},
        )

    try:
        body = ShipBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ()))
        return _err(
            "invalid_body",
            "body failed schema validation",
            422,
            {"field": field, "reason": first.get("type", "value_error")},
        )
    except Exception:
        return _err("invalid_body", "body is not valid JSON", 422)

    body_dict = body.model_dump(mode="json")
    body_hash = canonical_body_sha256(body_dict)
    idempotency_key_str = body_dict["idempotency_key"]

    token_id = g.current_token["token_id"]
    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # 0a. Warm-cache replay short-circuit. dockd_idempotency rows are
    # immutable once committed; read-committed semantics are sound.
    cached = g.db.execute(
        text(
            """
            SELECT endpoint, request_body_sha256, response_body, response_status,
                   so_number AS cached_so_number
              FROM dockd_idempotency
             WHERE token_id = :token_id AND idempotency_key = :key
             LIMIT 1
            """
        ),
        {"token_id": token_id, "key": idempotency_key_str},
    ).fetchone()
    if cached is not None:
        if cached.endpoint != _SHIP_ENDPOINT or cached.request_body_sha256 != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key was previously used with a different body or endpoint",
                409,
            )
        if cached.response_body is None:
            # Sentinel row from a still-in-flight peer; fall through to the
            # explicit-transaction path which will block on the ON CONFLICT
            # path until the peer commits or aborts.
            pass
        else:
            g._dockd_replay_hit = True
            return _replay_response(cached)

    # Resolve operator BEFORE the transaction so an unknown operator
    # doesn't claim a sentinel row that then gets rolled back.
    operator_external_id = get_user_external_id(g.db, body.operator_username)
    if operator_external_id is None:
        return _err(
            "unknown_operator",
            "operator_username does not resolve to a Sentry users row",
            422,
            {"field": "operator_username"},
        )

    station_label = g.current_token.get("token_name")

    try:
        g.db.execute(text("SET LOCAL lock_timeout = '5s'"))
    except OperationalError:
        # SQLite or a non-Postgres engine in some tests would reject
        # SET LOCAL; the production engine is Postgres. Swallow so unit
        # tests against an in-memory engine still work.
        pass

    try:
        # Step 1: sentinel INSERT.
        sentinel = g.db.execute(
            text(
                """
                INSERT INTO dockd_idempotency
                    (token_id, idempotency_key, endpoint, so_number,
                     request_body_sha256, response_body, response_status, created_at)
                VALUES (:token_id, :key, :endpoint, :so_number,
                        :body_hash, NULL, NULL, NOW())
                ON CONFLICT (token_id, idempotency_key) DO NOTHING
                RETURNING token_id
                """
            ),
            {
                "token_id": token_id,
                "key": idempotency_key_str,
                "endpoint": _SHIP_ENDPOINT,
                "so_number": so_number,
                "body_hash": body_hash,
            },
        ).fetchone()
    except OperationalError as exc:
        # lock_timeout fires here when a concurrent transaction holds the
        # PK constraint conflict (sentinel insert) longer than 5s.
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _err(
                "idempotency_lock_timeout",
                "concurrent ship in flight; retry with the same key",
                503,
            )
        raise

    if sentinel is None:
        # Peer committed during the wait. Re-read and replay or 409.
        peer = g.db.execute(
            text(
                """
                SELECT endpoint, request_body_sha256, response_body, response_status
                  FROM dockd_idempotency
                 WHERE token_id = :token_id AND idempotency_key = :key
                """
            ),
            {"token_id": token_id, "key": idempotency_key_str},
        ).fetchone()
        g.db.commit()
        if peer is None:
            # Should not happen (peer aborted between our INSERT and SELECT);
            # treat as if our INSERT had succeeded by falling through to a
            # 503 and letting dockd retry.
            return _err(
                "idempotency_lock_timeout",
                "could not claim idempotency sentinel; retry",
                503,
            )
        if peer.endpoint != _SHIP_ENDPOINT or peer.request_body_sha256 != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key was previously used with a different body or endpoint",
                409,
            )
        g._dockd_replay_hit = True
        return _replay_response(peer)

    # Step 2: SELECT ... FOR UPDATE on sales_orders.
    if not token_warehouse_ids:
        g.db.rollback()
        return _err("not_found", "order not found", 404)

    so = g.db.execute(
        text(
            """
            SELECT so_id, so_number, external_id, status, warehouse_id,
                   tracking_number, carrier, shipped_at
              FROM sales_orders
             WHERE so_number = :so_number
               AND warehouse_id = ANY(:wh_ids)
             FOR UPDATE
            """
        ),
        {"so_number": so_number, "wh_ids": token_warehouse_ids},
    ).fetchone()

    if so is None:
        g.db.rollback()
        return _err("not_found", "order not found", 404)

    # Step 3: status gate.
    if so.status == SO_SHIPPED:
        # Pull shipped_by from the latest fulfillment for the response.
        ff = g.db.execute(
            text(
                """
                SELECT shipped_by FROM item_fulfillments
                 WHERE so_id = :so_id AND status = 'SHIPPED'
                 ORDER BY shipped_at DESC NULLS LAST LIMIT 1
                """
            ),
            {"so_id": so.so_id},
        ).fetchone()
        existing_shipped_by = ff.shipped_by if ff else None
        g.db.rollback()
        return _err(
            "already_shipped",
            "order has already been shipped",
            409,
            {
                "existing_tracking": so.tracking_number,
                "carrier": so.carrier,
                "shipped_at": so.shipped_at.isoformat() if so.shipped_at else None,
                "shipped_by": existing_shipped_by,
                "station_label": None,
            },
        )

    packing_required = require_packing_before_shipping(g.db)
    allowed_statuses = (
        [SO_PACKED] if packing_required else [SO_PICKED, SO_PACKED]
    )
    if so.status not in allowed_statuses:
        g.db.rollback()
        return _err(
            "not_in_shippable_status",
            "order is not in a shippable status",
            410,
            {
                "current_status": so.status,
                "allowed_statuses": allowed_statuses,
            },
        )

    # Step 4: hand off to the shared shipping service. record_ship runs
    # the fulfillment INSERT, per-line writes, SO status UPDATE, audit
    # log write, and the ship.confirmed/1 outbox emit. source_txn_id =
    # idempotency_key ties outbox dedup (mig 020 UNIQUE on
    # (aggregate_type, aggregate_id, event_type, source_txn_id)) to the
    # HTTP-level dedup so a successful retry cannot double-emit.
    audit_extra = {
        "station_label": station_label,
        "manual_link": body.manual_link,
        "idempotency_key": idempotency_key_str,
        "operator_username": body.operator_username,
    }
    if body.weight is not None:
        audit_extra["weight"] = float(body.weight)
    if body.dims is not None:
        audit_extra["dims"] = {
            "l": float(body.dims.l), "w": float(body.dims.w), "h": float(body.dims.h),
        }

    result = record_ship(
        g.db,
        so_id=so.so_id,
        so_number=so.so_number,
        so_external_id=so.external_id,
        warehouse_id=so.warehouse_id,
        tracking_number=body.tracking,
        carrier=body.carrier,
        ship_method=body.ship_method,
        username=body.operator_username,
        source_txn_id=idempotency_key_str,
        pre_ship_status=so.status,
        shipping_cost=body.shipping_cost,
        audit_details_extra=audit_extra,
    )

    response_body_dict = {
        "status": SO_SHIPPED,
        "tracking": body.tracking,
        "shipped_at": result["shipped_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fulfillment_id": result["fulfillment_id"],
        "audit_log_id": result["audit_log_id"],
    }

    # Step 5: cache the response so a subsequent retry inside the 72h
    # window short-circuits at step 0a.
    import json as _json
    g.db.execute(
        text(
            """
            UPDATE dockd_idempotency
               SET response_body = CAST(:body AS jsonb),
                   response_status = :status
             WHERE token_id = :token_id AND idempotency_key = :key
            """
        ),
        {
            "token_id": token_id,
            "key": idempotency_key_str,
            "body": _json.dumps(response_body_dict),
            "status": 200,
        },
    )

    g.db.commit()

    return _draft_response(response_body_dict, 200)


# ----------------------------------------------------------------------
# POST /api/v1/dockd/orders/<so_number>/void-ship
# ----------------------------------------------------------------------


_VOID_ENDPOINT = "void-ship"


@dockd_bp.route("/orders/<so_number>/void-ship", methods=["POST"])
@require_wms_token
@limiter.limit("10 per minute", exempt_when=lambda: getattr(g, "_dockd_replay_hit", False))
@with_db
def void_ship(so_number):
    """Reverse a previously-successful ship.

    Same idempotency model as POST /ship: warm-cache replay short-circuit,
    then sentinel ON CONFLICT inside the transaction. SELECT...FOR UPDATE
    on sales_orders, status must be SHIPPED (else 409 not_shipped). The
    fulfillment row's pre_ship_status is the revert target; mig 054
    backfilled it to PICKED for legacy ships and v1.9 #5's record_ship
    populates it on every new ship, so a NULL here is a data-integrity
    bug and surfaces as a 500 by design.
    """
    err = _validate_so_number(so_number)
    if err is not None:
        return err

    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        return _err(
            "body_too_large",
            "request body exceeds SENTRY_DOCKD_MAX_BODY_KB",
            413,
            {"max_body_kb": get_max_body_kb()},
        )

    try:
        body = VoidShipBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ()))
        return _err(
            "invalid_body",
            "body failed schema validation",
            422,
            {"field": field, "reason": first.get("type", "value_error")},
        )
    except Exception:
        return _err("invalid_body", "body is not valid JSON", 422)

    body_dict = body.model_dump(mode="json")
    body_hash = canonical_body_sha256(body_dict)
    idempotency_key_str = body_dict["idempotency_key"]

    token_id = g.current_token["token_id"]
    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Warm-cache replay short-circuit.
    cached = g.db.execute(
        text(
            """
            SELECT endpoint, request_body_sha256, response_body, response_status
              FROM dockd_idempotency
             WHERE token_id = :token_id AND idempotency_key = :key
             LIMIT 1
            """
        ),
        {"token_id": token_id, "key": idempotency_key_str},
    ).fetchone()
    if cached is not None:
        if cached.endpoint != _VOID_ENDPOINT or cached.request_body_sha256 != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key was previously used with a different body or endpoint",
                409,
            )
        if cached.response_body is not None:
            g._dockd_replay_hit = True
            return _replay_response(cached)

    operator_external_id = get_user_external_id(g.db, body.operator_username)
    if operator_external_id is None:
        return _err(
            "unknown_operator",
            "operator_username does not resolve to a Sentry users row",
            422,
            {"field": "operator_username"},
        )

    station_label = g.current_token.get("token_name")

    try:
        g.db.execute(text("SET LOCAL lock_timeout = '5s'"))
    except OperationalError:
        pass

    try:
        sentinel = g.db.execute(
            text(
                """
                INSERT INTO dockd_idempotency
                    (token_id, idempotency_key, endpoint, so_number,
                     request_body_sha256, response_body, response_status, created_at)
                VALUES (:token_id, :key, :endpoint, :so_number,
                        :body_hash, NULL, NULL, NOW())
                ON CONFLICT (token_id, idempotency_key) DO NOTHING
                RETURNING token_id
                """
            ),
            {
                "token_id": token_id,
                "key": idempotency_key_str,
                "endpoint": _VOID_ENDPOINT,
                "so_number": so_number,
                "body_hash": body_hash,
            },
        ).fetchone()
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _err(
                "idempotency_lock_timeout",
                "concurrent void in flight; retry with the same key",
                503,
            )
        raise

    if sentinel is None:
        peer = g.db.execute(
            text(
                """
                SELECT endpoint, request_body_sha256, response_body, response_status
                  FROM dockd_idempotency
                 WHERE token_id = :token_id AND idempotency_key = :key
                """
            ),
            {"token_id": token_id, "key": idempotency_key_str},
        ).fetchone()
        g.db.commit()
        if peer is None:
            return _err(
                "idempotency_lock_timeout",
                "could not claim idempotency sentinel; retry",
                503,
            )
        if peer.endpoint != _VOID_ENDPOINT or peer.request_body_sha256 != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key was previously used with a different body or endpoint",
                409,
            )
        g._dockd_replay_hit = True
        return _replay_response(peer)

    if not token_warehouse_ids:
        g.db.rollback()
        return _err("not_found", "order not found", 404)

    so = g.db.execute(
        text(
            """
            SELECT so_id, so_number, external_id, status, warehouse_id
              FROM sales_orders
             WHERE so_number = :so_number
               AND warehouse_id = ANY(:wh_ids)
             FOR UPDATE
            """
        ),
        {"so_number": so_number, "wh_ids": token_warehouse_ids},
    ).fetchone()

    if so is None:
        g.db.rollback()
        return _err("not_found", "order not found", 404)

    if so.status != SO_SHIPPED:
        g.db.rollback()
        return _err(
            "not_shipped",
            "order is not in SHIPPED status; cannot void",
            409,
            {"current_status": so.status},
        )

    # Pick the SHIPPED fulfillment to void. Sentry creates one fulfillment
    # per SO today; if a future split-shipment lands, this query keeps the
    # latest-by-shipped_at semantic.
    ff = g.db.execute(
        text(
            """
            SELECT fulfillment_id, pre_ship_status
              FROM item_fulfillments
             WHERE so_id = :so_id AND status = 'SHIPPED'
             ORDER BY shipped_at DESC NULLS LAST, fulfillment_id DESC
             LIMIT 1
            """
        ),
        {"so_id": so.so_id},
    ).fetchone()

    if ff is None:
        # Defensive: SO claims SHIPPED but no SHIPPED fulfillment row.
        # Treat as a data-integrity surprise; rollback and surface a
        # generic 409 so dockd does not retry blindly.
        g.db.rollback()
        return _err(
            "not_shipped",
            "no SHIPPED fulfillment found for this order",
            409,
            {"current_status": so.status},
        )

    audit_extra = {
        "station_label": station_label,
        "idempotency_key": idempotency_key_str,
        "operator_username": body.operator_username,
    }

    result = record_void_ship(
        g.db,
        so_id=so.so_id,
        so_number=so.so_number,
        so_external_id=so.external_id,
        warehouse_id=so.warehouse_id,
        fulfillment_id=ff.fulfillment_id,
        pre_ship_status=ff.pre_ship_status,
        operator_username=body.operator_username,
        operator_external_id=operator_external_id,
        reason=body.reason,
        source_txn_id=idempotency_key_str,
        audit_details_extra=audit_extra,
    )

    response_body_dict = {
        "status": result["reverted_to_status"],
        "voided_at": result["voided_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "audit_log_id": result["audit_log_id"],
    }

    import json as _json
    g.db.execute(
        text(
            """
            UPDATE dockd_idempotency
               SET response_body = CAST(:body AS jsonb),
                   response_status = :status
             WHERE token_id = :token_id AND idempotency_key = :key
            """
        ),
        {
            "token_id": token_id,
            "key": idempotency_key_str,
            "body": _json.dumps(response_body_dict),
            "status": 200,
        },
    )

    g.db.commit()

    return _draft_response(response_body_dict, 200)
