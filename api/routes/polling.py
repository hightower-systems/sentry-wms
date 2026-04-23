"""GET /api/v1/events polling endpoint (v1.5.0 #122).

First route under the /api/v1/* surface. Connectors poll this with a
plain int64 ``after`` cursor (client-cursor mode) or a ``consumer_group``
identifier (consumer-group mode); the two modes are mutually exclusive
and sending both returns 400. Scope enforcement is strict subset per
plan Decision H: a request that asks for anything outside the token's
warehouse_ids or event_types returns 403, never a silent intersection.

Wire shape (plan 2.2, pinned):
- ``next_cursor`` is a plain int64. No base64, no opaque format.
- No ``has_more`` field. Full page (events.length == limit) implies
  more; partial page implies caught up. Connectors compute this
  themselves.
- Visibility gate is hardcoded: visible_at IS NOT NULL AND
  visible_at <= NOW() - INTERVAL '2 seconds'. Not configurable, not a
  query param; rooted in the migration 020 trigger contract.
- aggregate_external_id is read directly from integration_events per
  Decision J. No join to the aggregate table, no wire view.
- Rate limit 120/minute per token (plan 2.1), applied via
  services.rate_limit._rate_limit_key which prefers g.current_token.
"""

import json
import time
from typing import Dict, List, Optional

from flask import Blueprint, Response, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.polling import AckBody, PollQuery
from services import events_schema_registry
from services.rate_limit import limiter
from utils.validation import validate_body


polling_bp = Blueprint("polling", __name__)


# Per-process in-memory dict used by consumer-group mode to throttle
# last_heartbeat UPDATEs to once per 30s per group (Decision T). A
# connector polling at default cadence would otherwise double the
# write amplification on every request.
_HEARTBEAT_THROTTLE_SECONDS = 30
_last_heartbeat_write: Dict[str, float] = {}


def _scope_violation(request_values, allowed_values) -> bool:
    """Return True iff any value in ``request_values`` is outside the
    token's ``allowed_values``. Strict subset per Decision H."""
    if not request_values:
        return False
    allowed = set(allowed_values or [])
    return any(v not in allowed for v in request_values)


def _parse_types_csv(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _build_events_query(
    after: int,
    allowed_warehouses: List[int],
    allowed_event_types: List[str],
    request_warehouse_id: Optional[int],
    request_types: List[str],
    subscription_warehouse_ids: Optional[List[int]],
    subscription_event_types: Optional[List[str]],
    limit: int,
):
    """Produce the SELECT + params pair. Filters are conjunctive:
    token scope ∧ request filter ∧ subscription filter. Empty token
    scope arrays match no rows (plan 3.1: ``empty = no access``).
    """
    clauses = [
        "event_id > :after",
        "visible_at IS NOT NULL",
        "visible_at <= NOW() - INTERVAL '2 seconds'",
        "warehouse_id = ANY(:allowed_warehouses)",
        "event_type = ANY(:allowed_event_types)",
    ]
    params = {
        "after": after,
        "allowed_warehouses": allowed_warehouses,
        "allowed_event_types": allowed_event_types,
        "limit": limit,
    }
    if request_warehouse_id is not None:
        clauses.append("warehouse_id = :request_warehouse_id")
        params["request_warehouse_id"] = request_warehouse_id
    if request_types:
        clauses.append("event_type = ANY(:request_types)")
        params["request_types"] = request_types
    if subscription_warehouse_ids:
        clauses.append("warehouse_id = ANY(:sub_warehouse_ids)")
        params["sub_warehouse_ids"] = subscription_warehouse_ids
    if subscription_event_types:
        clauses.append("event_type = ANY(:sub_event_types)")
        params["sub_event_types"] = subscription_event_types
    sql = (
        "SELECT event_id, event_type, event_version, event_timestamp, "
        "       aggregate_type, aggregate_external_id, warehouse_id, "
        "       source_txn_id, payload "
        "  FROM integration_events "
        " WHERE " + " AND ".join(clauses) + " "
        " ORDER BY event_id ASC "
        " LIMIT :limit"
    )
    return sql, params


def _row_to_envelope(row) -> dict:
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "event_version": row.event_version,
        "event_timestamp": row.event_timestamp.isoformat(),
        "aggregate_type": row.aggregate_type,
        # Plan Decision J: the wire carries the external_id directly,
        # read from integration_events (no join to the aggregate table).
        "aggregate_id": str(row.aggregate_external_id),
        "warehouse_id": row.warehouse_id,
        "source_txn_id": str(row.source_txn_id),
        "data": payload,
    }


def _load_consumer_group(db, consumer_group_id: str):
    return db.execute(
        text(
            "SELECT consumer_group_id, last_cursor, subscription "
            "  FROM consumer_groups WHERE consumer_group_id = :cgid"
        ),
        {"cgid": consumer_group_id},
    ).fetchone()


def _maybe_write_heartbeat(db, consumer_group_id: str) -> bool:
    """Update consumer_groups.last_heartbeat when the per-process
    throttle permits. Returns True on write, False when throttled."""
    now = time.monotonic()
    last_write = _last_heartbeat_write.get(consumer_group_id, 0.0)
    if (now - last_write) < _HEARTBEAT_THROTTLE_SECONDS:
        return False
    db.execute(
        text(
            "UPDATE consumer_groups SET last_heartbeat = NOW() "
            " WHERE consumer_group_id = :cgid"
        ),
        {"cgid": consumer_group_id},
    )
    _last_heartbeat_write[consumer_group_id] = now
    return True


# Blueprint is mounted at /api/v1/events so "/" serves GET /api/v1/events
# and future siblings land at "/ack", "/types", "/schema/<type>/<ver>".
@polling_bp.route("/", methods=["GET"], strict_slashes=False)
@require_wms_token
@limiter.limit("120 per minute")
@with_db
def poll_events():
    """Serve one page of integration_events to the caller.

    Two modes (mutually exclusive):
    - client-cursor: ``after=<int>`` [, types, warehouse_id, limit]
    - consumer-group: ``consumer_group=<id>`` [, types, warehouse_id, limit]

    The consumer-group mode reads the group's persisted last_cursor as
    the effective ``after`` and applies the group's subscription
    JSONB as additional filtering.
    """
    try:
        query = PollQuery(
            after=request.args.get("after", type=int),
            consumer_group=request.args.get("consumer_group") or None,
            types=request.args.get("types") or None,
            warehouse_id=request.args.get("warehouse_id", type=int),
            limit=request.args.get("limit", default=500, type=int),
        )
    except Exception as e:  # noqa: BLE001 -- Pydantic validation error
        return jsonify({"error": str(e)}), 400

    token = g.current_token
    allowed_warehouses = list(token.get("warehouse_ids") or [])
    allowed_event_types = list(token.get("event_types") or [])
    request_types = _parse_types_csv(query.types)

    # Strict-subset scope check BEFORE running the query so a scope
    # violation returns 403 with a clear error body rather than an
    # empty events array that looks like "caught up".
    if query.warehouse_id is not None and _scope_violation(
        [query.warehouse_id], allowed_warehouses
    ):
        return jsonify({"error": "scope_violation", "field": "warehouse_id"}), 403
    if request_types and _scope_violation(request_types, allowed_event_types):
        return jsonify({"error": "scope_violation", "field": "types"}), 403

    subscription_warehouse_ids: Optional[List[int]] = None
    subscription_event_types: Optional[List[str]] = None
    after = query.after or 0

    if query.consumer_group is not None:
        cg_row = _load_consumer_group(g.db, query.consumer_group)
        if cg_row is None:
            return jsonify({"error": "consumer_group_not_found"}), 404
        after = cg_row.last_cursor
        subscription = cg_row.subscription or {}
        if isinstance(subscription, str):
            subscription = json.loads(subscription)
        # v1.5.1 V-204 (#145): the admin endpoints reject malformed
        # subscriptions at write time, but a pre-v1.5.1 row may still
        # carry a shape the handler cannot parse (e.g. warehouse_ids
        # as a string). Return 409 subscription_invalid so the
        # caller sees a recoverable contract error instead of a 500.
        if not isinstance(subscription, dict):
            return jsonify({"error": "subscription_invalid"}), 409
        sub_wh = subscription.get("warehouse_ids")
        sub_et = subscription.get("event_types")
        try:
            if sub_wh:
                subscription_warehouse_ids = [int(w) for w in sub_wh]
            if sub_et:
                subscription_event_types = [str(t) for t in sub_et]
        except (TypeError, ValueError):
            return jsonify({"error": "subscription_invalid"}), 409

    sql, params = _build_events_query(
        after=after,
        allowed_warehouses=allowed_warehouses,
        allowed_event_types=allowed_event_types,
        request_warehouse_id=query.warehouse_id,
        request_types=request_types,
        subscription_warehouse_ids=subscription_warehouse_ids,
        subscription_event_types=subscription_event_types,
        limit=query.limit,
    )
    rows = g.db.execute(text(sql), params).fetchall()
    events = [_row_to_envelope(r) for r in rows]
    # Plain int64 next_cursor (plan 2.2, pinned). When no rows land the
    # cursor echoes the input so the next poll does not regress.
    next_cursor = events[-1]["event_id"] if events else after

    if query.consumer_group is not None:
        _maybe_write_heartbeat(g.db, query.consumer_group)
        g.db.commit()

    return jsonify({"events": events, "next_cursor": next_cursor})


@polling_bp.route("/ack", methods=["POST"])
@require_wms_token
@limiter.limit("120 per minute")
@validate_body(AckBody)
@with_db
def ack_cursor(validated: AckBody):
    """Advance a consumer group's cursor atomically.

    Semantics:
    - 404 if the consumer group does not exist.
    - 403 if the token has a connector_id that does not match the
      group's connector_id (cross-connector isolation).
    - v1.5.1 V-202 (#143): 400 when the requested cursor exceeds the
      greatest event_id in the outbox (``cursor_beyond_horizon``).
      Impossible-future cursors were previously accepted and advanced
      the group past every future event, causing silent data loss.
    - v1.5.1 V-202 (#143): 403 ``ack_scope_violation`` when any event
      in (last_cursor, cursor] falls outside the token's warehouse_ids
      or event_types scope. "You can only ack what you can read"
      applies on both axes; a NULL-connector admin token cannot use
      ack to jump past events it could not have polled.
    - UPDATE with ``WHERE last_cursor <= :cursor`` so an out-of-order
      ack lower than the current stored value is a no-op. The
      response always returns the row's current ``last_cursor`` so
      the client can see whether its ack advanced the pointer.
    """
    cg_row = g.db.execute(
        text(
            "SELECT consumer_group_id, connector_id, last_cursor "
            "  FROM consumer_groups WHERE consumer_group_id = :cgid"
        ),
        {"cgid": validated.consumer_group},
    ).fetchone()
    if cg_row is None:
        return jsonify({"error": "consumer_group_not_found"}), 404

    token_connector = g.current_token.get("connector_id")
    if token_connector and token_connector != cg_row.connector_id:
        # Cross-connector isolation: a token bound to connector A must
        # not ack groups owned by connector B. Tokens without a
        # connector_id (admin / legacy) can ack any group.
        return jsonify({"error": "consumer_group_scope_violation"}), 403

    # v1.5.1 V-202 (#143): only advancing acks can cause data loss or
    # scope skip; a backwards ack is a pure no-op via the existing
    # ``WHERE last_cursor <= :cursor`` clause. Skip the extra round
    # trips when cursor <= last_cursor.
    if validated.cursor > cg_row.last_cursor:
        horizon = g.db.execute(
            text(
                "SELECT COALESCE(MAX(event_id), 0) AS max_id "
                "  FROM integration_events"
            )
        ).scalar()
        if validated.cursor > horizon:
            return jsonify({"error": "cursor_beyond_horizon"}), 400

        allowed_warehouses = list(g.current_token.get("warehouse_ids") or [])
        allowed_event_types = list(g.current_token.get("event_types") or [])
        # "You can only ack what you can read": if any event in
        # (last_cursor, cursor] falls outside the token's scope on
        # either axis, the ack would implicitly claim the consumer
        # processed an event it could not have polled. Reject 403.
        # Empty scope arrays match no rows on ANY(...) so the check
        # naturally triggers for deny-all tokens.
        out_of_scope = g.db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM integration_events
                     WHERE event_id > :last_cursor
                       AND event_id <= :cursor
                       AND NOT (
                             warehouse_id = ANY(:allowed_warehouses)
                         AND event_type  = ANY(:allowed_event_types)
                       )
                )
                """
            ),
            {
                "last_cursor": cg_row.last_cursor,
                "cursor": validated.cursor,
                "allowed_warehouses": allowed_warehouses,
                "allowed_event_types": allowed_event_types,
            },
        ).scalar()
        if out_of_scope:
            return jsonify({"error": "ack_scope_violation"}), 403

    g.db.execute(
        text(
            "UPDATE consumer_groups SET last_cursor = :cursor, updated_at = NOW() "
            " WHERE consumer_group_id = :cgid AND last_cursor <= :cursor"
        ),
        {"cgid": validated.consumer_group, "cursor": validated.cursor},
    )
    refreshed = g.db.execute(
        text(
            "SELECT last_cursor FROM consumer_groups WHERE consumer_group_id = :cgid"
        ),
        {"cgid": validated.consumer_group},
    ).fetchone()
    g.db.commit()
    return jsonify(
        {
            "consumer_group": validated.consumer_group,
            "last_cursor": refreshed.last_cursor,
        }
    )


@polling_bp.route("/types", methods=["GET"])
@require_wms_token
@limiter.limit("120 per minute")
def list_event_types():
    """Serve the v1.5.0 event catalog from the in-process registry,
    filtered by the caller's token scope.

    No DB, no per-request filesystem read; the registry is loaded once
    at ``create_app`` time (#110) and catalog queries are O(7).

    v1.5.1 V-212 (#151): the response is scoped to the token's
    ``event_types`` list so a token cannot enumerate event types it
    has no read access for. Pre-v1.5.1 the full catalog leaked to
    every caller; aids reconnaissance for later pivots ("cycle_count.adjusted
    events I cannot see, worth finding a broader token for").
    """
    allowed = list(g.current_token.get("event_types") or [])
    return jsonify(
        {"types": events_schema_registry.known_types(event_types_filter=allowed)}
    )


@polling_bp.route("/schema/<event_type>/<int:version>", methods=["GET"])
@require_wms_token
@limiter.limit("120 per minute")
def serve_schema(event_type, version):
    """Stream the raw JSON Schema file as application/schema+json.

    404 when the pair is not in V150_CATALOG; the registry always has
    a loadable file for every catalog entry so a ``(type, version)``
    that is not registered is an unknown-pair error, not a file-missing
    error.
    """
    if not any(
        e == event_type and v == version
        for e, v, _ in events_schema_registry.V150_CATALOG
    ):
        return jsonify({"error": "unknown_event_type_or_version"}), 404
    path = events_schema_registry.schema_path(event_type, version)
    # Reading the file each call is cheap (tens of kB) and the
    # registry-at-boot validation guarantees the file is present and
    # well-formed; no need to cache-and-reserialise.
    with open(path, "rb") as f:
        body = f.read()
    return Response(body, mimetype="application/schema+json")
