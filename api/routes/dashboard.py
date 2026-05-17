"""Productivity dashboard API (v1.8.0 #297).

Endpoints:

- GET  /api/v1/dashboard/productivity   per-user metrics for a date range
- GET  /api/v1/dashboard/preferences    user's chart_order + defaults
- PUT  /api/v1/dashboard/preferences    upsert preferences row
- GET  /api/v1/dashboard/received-today per-PO receipts for today (P11.3)
- GET  /api/v1/dashboard/shipping-health per-source-system SO health (P11.4)

Auth: cookie + ADMIN role for productivity (operators with admin
visibility); preferences are per-user with the user_id derived
from g.current_user (never from the request body).
"""

from datetime import date, timedelta
from typing import List, Optional

from flask import Blueprint, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from services.productivity_service import (
    DASHBOARD_EVENTS,
    get_productivity,
)


dashboard_bp = Blueprint("dashboard", __name__)


_VALID_CHART_ORDER_KEYS = {slug for (slug, _, _) in DASHBOARD_EVENTS}
_VALID_DEFAULT_RANGES = {
    "today", "yesterday", "last_7d", "last_30d", "custom",
}
_VALID_DEFAULT_VIEWS = {"charts", "table"}

_MAX_RANGE_DAYS = 90


# ============================================================
# Productivity
# ============================================================


class _ProductivityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    warehouse_id: int = Field(..., gt=0)

    @field_validator("end")
    @classmethod
    def _end_not_before_start(cls, v, info):
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError("end must be on or after start")
        return v


@dashboard_bp.route("/productivity", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def productivity():
    try:
        params = _ProductivityQuery.model_validate({
            "start": request.args.get("start"),
            "end": request.args.get("end"),
            "warehouse_id": request.args.get("warehouse_id", type=int),
        })
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    span_days = (params.end - params.start).days + 1
    if span_days > _MAX_RANGE_DAYS:
        return jsonify({
            "error": "range_too_large",
            "max_range_days": _MAX_RANGE_DAYS,
            "requested_days": span_days,
        }), 422

    # The endpoint accepts inclusive [start, end] dates; the SQL uses
    # half-open [start, end+1day) so the index range scan is clean.
    start_dt = params.start
    end_dt_exclusive = params.end + timedelta(days=1)
    payload = get_productivity(
        g.db, params.warehouse_id, start_dt, end_dt_exclusive,
    )
    # Fix up the response range to mirror what the operator asked for
    # (the cache key uses the SQL-level half-open form).
    payload["range"] = {
        "start": params.start.isoformat(),
        "end": params.end.isoformat(),
    }
    return jsonify(payload)


# ============================================================
# Preferences
# ============================================================


class _PreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_order: Optional[List[str]] = None
    default_range: Optional[str] = None
    default_view: Optional[str] = None

    @field_validator("chart_order")
    @classmethod
    def _check_chart_order(cls, v):
        if v is None:
            return v
        unknown = [k for k in v if k not in _VALID_CHART_ORDER_KEYS]
        if unknown:
            raise ValueError(
                f"chart_order has unknown keys: {unknown!r}. "
                f"Valid: {sorted(_VALID_CHART_ORDER_KEYS)}"
            )
        if len(set(v)) != len(v):
            raise ValueError("chart_order must not contain duplicates")
        return v

    @field_validator("default_range")
    @classmethod
    def _check_default_range(cls, v):
        if v is not None and v not in _VALID_DEFAULT_RANGES:
            raise ValueError(
                f"default_range must be one of {sorted(_VALID_DEFAULT_RANGES)}"
            )
        return v

    @field_validator("default_view")
    @classmethod
    def _check_default_view(cls, v):
        if v is not None and v not in _VALID_DEFAULT_VIEWS:
            raise ValueError(
                f"default_view must be one of {sorted(_VALID_DEFAULT_VIEWS)}"
            )
        return v


def _resolve_user_id():
    """user_id derived from g.current_user (CSRF / IDOR protection per
    plan section 5.2). Returns None when unauthenticated."""
    user = getattr(g, "current_user", None) or {}
    return user.get("user_id")


def _row_to_dict(row) -> dict:
    return {
        "chart_order": (
            list(row.chart_order) if row.chart_order is not None
            else [slug for (slug, _, _) in DASHBOARD_EVENTS]
        ),
        "default_range": row.default_range,
        "default_view": row.default_view,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@dashboard_bp.route("/preferences", methods=["GET"])
@require_auth
@with_db
def get_preferences():
    user_id = _resolve_user_id()
    if user_id is None:
        return jsonify({"error": "unauthenticated"}), 401
    row = g.db.execute(
        text(
            "SELECT chart_order, default_range, default_view, updated_at "
            "  FROM user_dashboard_preferences WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()
    if row is None:
        return jsonify({
            "chart_order": [slug for (slug, _, _) in DASHBOARD_EVENTS],
            "default_range": "today",
            "default_view": "charts",
            "updated_at": None,
        })
    return jsonify(_row_to_dict(row))


@dashboard_bp.route("/preferences", methods=["PUT"])
@require_auth
@with_db
def put_preferences():
    user_id = _resolve_user_id()
    if user_id is None:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        body = _PreferencesBody.model_validate(request.get_json() or {})
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    # Build the UPSERT dynamically so unset fields keep their existing
    # value (rather than reverting to defaults).
    fields = {}
    if body.chart_order is not None:
        fields["chart_order"] = body.chart_order
    if body.default_range is not None:
        fields["default_range"] = body.default_range
    if body.default_view is not None:
        fields["default_view"] = body.default_view
    if not fields:
        return jsonify({"error": "no_fields_to_update"}), 422

    # Build the INSERT column list (always includes user_id) + the
    # ON CONFLICT updates for the fields the body actually set. Empty
    # column case (only user_id) is handled by the no_fields_to_update
    # guard above.
    insert_cols = ["user_id"] + list(fields.keys())
    insert_placeholders = ["(:uid)"] + [f"(:{c})" for c in fields.keys()]

    # Use psycopg2.extras.Json wrapping for chart_order JSONB binding.
    from psycopg2.extras import Json
    params = {"uid": user_id}
    for col, val in fields.items():
        params[col] = Json(val) if col == "chart_order" else val

    # Construct the SQL: INSERT ... ON CONFLICT (user_id) DO UPDATE SET ...
    set_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in fields.keys()
    ) + ", updated_at = NOW()"
    cols_sql = ", ".join(insert_cols)
    vals_sql = ", ".join(f":{c}" if c != "user_id" else ":uid"
                         for c in insert_cols)
    g.db.execute(
        text(
            f"INSERT INTO user_dashboard_preferences ({cols_sql}) "
            f"VALUES ({vals_sql}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {set_clause}"
        ),
        params,
    )
    g.db.commit()

    row = g.db.execute(
        text(
            "SELECT chart_order, default_range, default_view, updated_at "
            "  FROM user_dashboard_preferences WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()
    return jsonify(_row_to_dict(row))


# ============================================================
# Received Today (P11.3)
# ============================================================


@dashboard_bp.route("/received", methods=["GET"])
@require_auth
@with_db
def received():
    """avid-overhaul-mk1 P11.3 / P11.6: per-PO receipts for a date range.

    Aggregates audit_log RECEIVE rows in [start, end] (inclusive
    dates, treated as half-open [start, end+1day) in SQL for clean
    index scans). Per PO the response carries the PO header, what
    was received in range (units + distinct line items + receivers),
    and when the most recent line landed.

    start / end default to CURRENT_DATE so callers without an
    explicit range still see "today" without special-casing on the
    client. warehouse_id is required.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422

    start_str = request.args.get("start") or date.today().isoformat()
    end_str = request.args.get("end") or date.today().isoformat()
    try:
        start_dt = date.fromisoformat(start_str)
        end_dt = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({"error": "start/end must be YYYY-MM-DD"}), 422
    if end_dt < start_dt:
        return jsonify({"error": "end must be on or after start"}), 422
    end_exclusive = end_dt + timedelta(days=1)

    rows = g.db.execute(
        text(
            """
            SELECT al.entity_id AS po_id,
                   po.po_number,
                   po.vendor_name,
                   po.status,
                   SUM(COALESCE((al.details->>'quantity')::int, 0)) AS units_received,
                   COUNT(DISTINCT (al.details->>'item_id')::int) AS lines_received,
                   COUNT(*) AS receive_events,
                   ARRAY_AGG(DISTINCT al.user_id) AS receivers,
                   MAX(al.created_at) AS last_received_at
              FROM audit_log al
              JOIN purchase_orders po ON po.po_id = al.entity_id
             WHERE al.action_type = 'RECEIVE'
               AND al.entity_type = 'PO'
               AND al.warehouse_id = :wid
               AND al.created_at >= :start
               AND al.created_at < :end
             GROUP BY al.entity_id, po.po_number, po.vendor_name, po.status
             ORDER BY MAX(al.created_at) DESC
            """
        ),
        {"wid": warehouse_id, "start": start_dt, "end": end_exclusive},
    ).fetchall()

    return jsonify({
        "warehouse_id": warehouse_id,
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "pos": [
            {
                "po_id": r.po_id,
                "po_number": r.po_number,
                "vendor_name": r.vendor_name,
                "status": r.status,
                "units_received": int(r.units_received or 0),
                "lines_received": int(r.lines_received or 0),
                "receive_events": int(r.receive_events or 0),
                "receivers": list(r.receivers or []),
                "last_received_at": (
                    r.last_received_at.isoformat()
                    if r.last_received_at else None
                ),
            }
            for r in rows
        ],
    })


# ============================================================
# Shipping Health (P11.4)
# ============================================================


# Watchlist threshold: any unshipped SO older than this is "stuck"
# unless it has its own ship_by_date (which takes precedence). Eight
# days is one full work week plus a buffer; tighter than that produces
# too much noise for a default dashboard view.
_STUCK_AGE_DAYS = 8


@dashboard_bp.route("/shipping-health", methods=["GET"])
@require_auth
@with_db
def shipping_health():
    """avid-overhaul-mk1 P11.4 / P11.6: per-source_system outbound health.

    Returns two slices:

      * by_source: one row per source_system carrying the count of
        orders received (by created_at) and shipped (by shipped_at)
        in the requested [start, end] range, plus the global "need
        to ship today" count (ship_by_date <= today and not yet
        shipped). The current-state slice (unshipped + oldest age)
        also stays so the UI can use it for header summaries.
      * stuck_orders: SOs past their ship_by_date (or older than 8
        days when ship_by_date is NULL) that have not shipped or
        been cancelled. Action-oriented watchlist for the desk.

    start / end default to CURRENT_DATE; warehouse_id is required.
    Range parameters apply only to orders_received / orders_shipped;
    need_to_ship_today and unshipped_count are always current-state.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422

    start_str = request.args.get("start") or date.today().isoformat()
    end_str = request.args.get("end") or date.today().isoformat()
    try:
        start_dt = date.fromisoformat(start_str)
        end_dt = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({"error": "start/end must be YYYY-MM-DD"}), 422
    if end_dt < start_dt:
        return jsonify({"error": "end must be on or after start"}), 422
    end_exclusive = end_dt + timedelta(days=1)

    # Per-source aggregate. COALESCE the source_system to a sentinel
    # so admin-created and POS-created SOs (NULL tag) still surface
    # as a "(no tag)" row instead of disappearing. orders_received
    # uses created_at because order_date is operator-supplied and
    # may be NULL; created_at is the ingestion-time source of truth.
    by_source_rows = g.db.execute(
        text(
            """
            SELECT COALESCE(so.source_system, '(no tag)') AS source_system,
                   COUNT(*) FILTER (
                       WHERE so.created_at >= :start
                         AND so.created_at < :end
                   ) AS orders_received,
                   COUNT(*) FILTER (
                       WHERE so.status = 'SHIPPED'
                         AND so.shipped_at >= :start
                         AND so.shipped_at < :end
                   ) AS orders_shipped,
                   COUNT(*) FILTER (
                       WHERE so.ship_by_date IS NOT NULL
                         AND so.ship_by_date <= CURRENT_DATE
                         AND so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
                   ) AS need_to_ship_today,
                   COUNT(*) FILTER (
                       WHERE so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
                   ) AS unshipped_count,
                   MIN(so.created_at) FILTER (
                       WHERE so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
                   ) AS oldest_unshipped_at
              FROM sales_orders so
             WHERE so.warehouse_id = :wid
             GROUP BY COALESCE(so.source_system, '(no tag)')
             ORDER BY COALESCE(so.source_system, '(no tag)')
            """
        ),
        {"wid": warehouse_id, "start": start_dt, "end": end_exclusive},
    ).fetchall()

    # avid-overhaul-mk1 P11.7 / P11.8: marketplace classification by
    # so_number pattern. Production SO numbers identify their
    # marketplace by shape, not by a source_system tag:
    #   * Amazon: three digits then a hyphen (123-1234567)
    #   * Ebay:   two digits then a hyphen   (12-3456789)
    #   * BigCommerce: no hyphen at all      (BC123456)
    # Anything that does not match all three falls into "Other" so
    # the dashboard exposes the unclassified backlog rather than
    # hiding it. Each bucket carries the SO list so clicking a
    # bubble can drop straight into "what specifically needs to
    # ship". 200-row cap per bucket keeps the payload bounded; a
    # bigger backlog should land in the Sales Orders search anyway.
    pattern_rows = g.db.execute(
        text(
            r"""
            SELECT
                CASE
                    WHEN so.so_number ~ '^[0-9]{3}-'       THEN 'Amazon'
                    WHEN so.so_number ~ '^[0-9]{2}-'       THEN 'Ebay'
                    WHEN POSITION('-' IN so.so_number) = 0 THEN 'BigCommerce'
                    ELSE 'Other'
                END AS marketplace,
                so.so_id, so.so_number, so.customer_name,
                so.status, so.ship_by_date
              FROM sales_orders so
             WHERE so.warehouse_id = :wid
               AND so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
               AND so.ship_by_date IS NOT NULL
               AND so.ship_by_date <= CURRENT_DATE
             ORDER BY so.ship_by_date ASC, so.so_id ASC
            """
        ),
        {"wid": warehouse_id},
    ).fetchall()
    pattern_buckets: dict[str, list] = {
        "Amazon": [], "Ebay": [], "BigCommerce": [], "Other": [],
    }
    for r in pattern_rows:
        bucket = pattern_buckets.get(r.marketplace)
        if bucket is None:
            continue
        if len(bucket) >= 200:
            continue
        bucket.append({
            "so_id": r.so_id,
            "so_number": r.so_number,
            "customer_name": r.customer_name,
            "status": r.status,
            "ship_by_date": r.ship_by_date.isoformat() if r.ship_by_date else None,
        })
    by_marketplace_pattern = [
        {
            "marketplace": name,
            "count": len(pattern_buckets[name]),
            "orders": pattern_buckets[name],
        }
        for name in ("Amazon", "Ebay", "BigCommerce")
    ]
    # Surface unclassified rows only if any exist so the bubble row
    # stays clean for warehouses whose so_numbers all match.
    if pattern_buckets["Other"]:
        by_marketplace_pattern.append({
            "marketplace": "Other",
            "count": len(pattern_buckets["Other"]),
            "orders": pattern_buckets["Other"],
        })

    # Stuck watchlist. "Stuck" = past ship_by_date OR older than the
    # threshold when no ship_by_date is set. Capped at 100 so a
    # backlog explosion does not blow up the payload; the UI links
    # out to Sales Orders with a filter for the full list.
    stuck_rows = g.db.execute(
        text(
            """
            SELECT so.so_id, so.so_number, so.customer_name,
                   so.status, so.created_at, so.ship_by_date,
                   COALESCE(so.source_system, '(no tag)') AS source_system,
                   EXTRACT(EPOCH FROM (NOW() - so.created_at)) / 86400 AS age_days
              FROM sales_orders so
             WHERE so.warehouse_id = :wid
               AND so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
               AND (
                   (so.ship_by_date IS NOT NULL AND so.ship_by_date < CURRENT_DATE)
                OR (so.ship_by_date IS NULL AND so.created_at < NOW() - make_interval(days => :threshold))
               )
             ORDER BY so.created_at ASC
             LIMIT 100
            """
        ),
        {"wid": warehouse_id, "threshold": _STUCK_AGE_DAYS},
    ).fetchall()

    return jsonify({
        "warehouse_id": warehouse_id,
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "stuck_threshold_days": _STUCK_AGE_DAYS,
        "by_marketplace_pattern": by_marketplace_pattern,
        "by_source": [
            {
                "source_system": r.source_system,
                "orders_received": int(r.orders_received or 0),
                "orders_shipped": int(r.orders_shipped or 0),
                "need_to_ship_today": int(r.need_to_ship_today or 0),
                "unshipped_count": int(r.unshipped_count or 0),
                "oldest_unshipped_at": (
                    r.oldest_unshipped_at.isoformat()
                    if r.oldest_unshipped_at else None
                ),
            }
            for r in by_source_rows
        ],
        "stuck_orders": [
            {
                "so_id": r.so_id,
                "so_number": r.so_number,
                "customer_name": r.customer_name,
                "status": r.status,
                "source_system": r.source_system,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "ship_by_date": (
                    r.ship_by_date.isoformat() if r.ship_by_date else None
                ),
                "age_days": round(float(r.age_days or 0), 1),
            }
            for r in stuck_rows
        ],
    })
