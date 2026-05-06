"""Admin Inbound observability surface (v1.7.0 plan §4.2 + §4.3).

Read-only paths for the admin UI's Inbound activity page. Per plan
§4.3 there are no mutation endpoints here -- v1.7 inbound is
read-only after acceptance; manual fixes go through SQL with
audit_log. The pull will be strong to add "replay this row" or
"manual edit" UI; resist until v2.0+ once a real consumer
demonstrates what fix workflows are actually needed.

Endpoints:

    GET /api/admin/inbound/activity
        -> last N rows across all five inbound_<resource> staging
           tables, filterable by source_system / resource /
           date range / status. UNION ALL across the staging
           tables; ORDER BY received_at DESC LIMIT.

    GET /api/admin/inbound/activity/<resource>/<inbound_id>
        -> detail view of one inbound row: source_payload +
           canonical_payload + ingested_via_token_id + status +
           timestamps. Forensic chain from inbound -> canonical
           -> outbound emission stays queryable from audit_log;
           this endpoint is the per-row landing pad.
"""

from flask import jsonify, request, g

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp
from sqlalchemy import text


# Resource keys = inbound_<resource> table names match the v1.7
# decorator's per-resource dispatch. Hardcoded over reading the
# decorator's V170_INBOUND_RESOURCE_BY_ENDPOINT map at request time
# because the SELECT / table-name interpolation happens in raw SQL --
# anything dynamic here must be allowlisted.
_INBOUND_TABLES = {
    "sales_orders": "inbound_sales_orders",
    "items": "inbound_items",
    "customers": "inbound_customers",
    "vendors": "inbound_vendors",
    "purchase_orders": "inbound_purchase_orders",
}


_VALID_STATUSES = {"applied", "superseded"}


def _activity_union_sql(filter_resource: str | None) -> str:
    """Build the UNION ALL across staging tables. The `resource` column
    is materialised as a string literal per branch so the front-end
    knows which detail endpoint to call. Filter args come through
    bound parameters, never string-interpolation."""
    if filter_resource and filter_resource in _INBOUND_TABLES:
        table = _INBOUND_TABLES[filter_resource]
        return (
            f"SELECT '{filter_resource}' AS resource, inbound_id, source_system, "
            f"       external_id, external_version, canonical_id, "
            f"       received_at, status, superseded_at, "
            f"       ingested_via_token_id "
            f"  FROM {table} "
        )
    branches = []
    for resource, table in _INBOUND_TABLES.items():
        branches.append(
            f"SELECT '{resource}' AS resource, inbound_id, source_system, "
            f"       external_id, external_version, canonical_id, "
            f"       received_at, status, superseded_at, "
            f"       ingested_via_token_id "
            f"  FROM {table}"
        )
    return " UNION ALL ".join(branches)


@admin_bp.route("/inbound/activity", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def list_inbound_activity():
    """Recent inbound activity across all five resources.

    Query params (all optional):
      source_system: exact match
      resource: exact match against one of the five resource keys
      status: 'applied' | 'superseded'
      since / until: ISO-8601 timestamps; received_at >= / received_at <=
      limit: 1-500, default 100
    """
    source_system = request.args.get("source_system")
    resource = request.args.get("resource")
    status = request.args.get("status")
    since = request.args.get("since")
    until = request.args.get("until")
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(500, limit))

    if resource is not None and resource not in _INBOUND_TABLES:
        return (
            jsonify({
                "error": "unknown_resource",
                "valid": sorted(_INBOUND_TABLES.keys()),
            }),
            400,
        )
    if status is not None and status not in _VALID_STATUSES:
        return (
            jsonify({
                "error": "unknown_status",
                "valid": sorted(_VALID_STATUSES),
            }),
            400,
        )

    union_sql = _activity_union_sql(resource)
    where = []
    params = {"limit": limit}
    if source_system:
        where.append("source_system = :ss")
        params["ss"] = source_system
    if status:
        where.append("status = :st")
        params["st"] = status
    if since:
        where.append("received_at >= :since")
        params["since"] = since
    if until:
        where.append("received_at <= :until")
        params["until"] = until
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT resource, inbound_id, source_system, external_id, "
        f"       external_version, canonical_id, received_at, status, "
        f"       superseded_at, ingested_via_token_id "
        f"  FROM ({union_sql}) AS u {where_clause} "
        f" ORDER BY received_at DESC LIMIT :limit"
    )
    rows = g.db.execute(text(sql), params).fetchall()
    return jsonify({
        "rows": [
            {
                "resource": r.resource,
                "inbound_id": r.inbound_id,
                "source_system": r.source_system,
                "external_id": r.external_id,
                "external_version": r.external_version,
                "canonical_id": str(r.canonical_id) if r.canonical_id else None,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "status": r.status,
                "superseded_at": (
                    r.superseded_at.isoformat() if r.superseded_at else None
                ),
                "ingested_via_token_id": r.ingested_via_token_id,
            }
            for r in rows
        ],
        "limit": limit,
    })


@admin_bp.route(
    "/inbound/activity/<resource>/<int:inbound_id>", methods=["GET"]
)
@require_auth
@require_role("ADMIN")
@with_db
def get_inbound_row(resource: str, inbound_id: int):
    """Detail view: source_payload + canonical_payload + ingest metadata.

    Returns 404 when the row is not found. Resource must be one of the
    five known keys; an unknown value returns 400 unknown_resource so
    the caller's bug is visible (rather than a silent 404)."""
    if resource not in _INBOUND_TABLES:
        return (
            jsonify({
                "error": "unknown_resource",
                "valid": sorted(_INBOUND_TABLES.keys()),
            }),
            400,
        )
    table = _INBOUND_TABLES[resource]
    row = g.db.execute(
        text(
            f"SELECT inbound_id, source_system, external_id, external_version, "
            f"       canonical_id, source_payload, canonical_payload, "
            f"       received_at, status, superseded_at, ingested_via_token_id "
            f"  FROM {table} WHERE inbound_id = :iid"
        ),
        {"iid": inbound_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "resource": resource,
        "inbound_id": row.inbound_id,
        "source_system": row.source_system,
        "external_id": row.external_id,
        "external_version": row.external_version,
        "canonical_id": str(row.canonical_id) if row.canonical_id else None,
        "source_payload": row.source_payload,
        "canonical_payload": row.canonical_payload,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "status": row.status,
        "superseded_at": (
            row.superseded_at.isoformat() if row.superseded_at else None
        ),
        "ingested_via_token_id": row.ingested_via_token_id,
    })
