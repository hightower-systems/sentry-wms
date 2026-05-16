"""POS admin endpoints — feeds the new admin/src/pages/POSActivity.jsx
dashboard with sales-order activity + daily KPI counters.

Data model note: Sentry intentionally does NOT store per-line prices or
operator metadata in `sales_orders` columns. Per v1.10.0 design (POS
endpoint surface release), pricing + cashier_id + terminal_id ride into
the audit_log.details JSONB at POS_CHECKOUT time. So this module's
queries LEFT JOIN audit_log to surface those fields back to the
dashboard.

Endpoints:
    GET /api/admin/pos/sales-orders   — paginated POS SO list w/ filters
    GET /api/admin/pos/summary        — today's KPI counters (sales /
                                        revenue / refunds / terminals)

Both gated by ADMIN role (same as the existing /admin/sales-orders).
"""

import math
from datetime import date, datetime, timedelta
from flask import g, jsonify, request
from sqlalchemy import text

from constants import ACTION_POS_CHECKOUT, ACTION_POS_REFUND
from middleware.auth_middleware import require_admin_or_page_permission, require_auth
from middleware.db import with_db
from routes.admin import admin_bp


# ---------------------------------------------------------------------------
# GET /api/admin/pos/sales-orders
# ---------------------------------------------------------------------------


@admin_bp.route("/pos/sales-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("pos-activity")
@with_db
def list_pos_sales_orders():
    """Paginated list of POS sales orders with the cashier/terminal/total
    detail joined from audit_log.

    Query params:
        page         — 1-based page number, default 1
        per_page     — page size, default 50, max 200
        since        — YYYY-MM-DD (orders with order_date >= since at UTC midnight)
        until        — YYYY-MM-DD (orders with order_date <  until at UTC midnight)
        terminal_id  — filter on the audit_log JSONB 'terminal_id' field
        cashier_id   — filter on the audit_log JSONB 'cashier_id' field (= sales_orders.created_by)
        status       — sales_orders.status filter (OPEN/ALLOCATED/PICKED/PACKED/SHIPPED/CANCELLED)
        order_type   — 'sale' (default) | 'refund' | 'all'
    """
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    since = request.args.get("since")
    until = request.args.get("until")
    terminal_id = request.args.get("terminal_id")
    cashier_id = request.args.get("cashier_id")
    so_status = request.args.get("status")
    order_type = request.args.get("order_type", "sale").lower()

    where_clauses = ["so.order_source = :pos_source"]
    params = {"pos_source": "pos", "checkout_action": ACTION_POS_CHECKOUT}

    if order_type == "sale":
        where_clauses.append("so.order_type = :order_type")
        params["order_type"] = "sale"
    elif order_type == "refund":
        where_clauses.append("so.order_type = :order_type")
        params["order_type"] = "refund"
    elif order_type != "all":
        # unknown values are treated as 'sale' to fail safe
        where_clauses.append("so.order_type = :order_type")
        params["order_type"] = "sale"

    if since:
        where_clauses.append("so.order_date >= CAST(:since AS DATE)")
        params["since"] = since
    if until:
        where_clauses.append("so.order_date < CAST(:until AS DATE)")
        params["until"] = until
    if so_status:
        where_clauses.append("so.status = :so_status")
        params["so_status"] = so_status
    if terminal_id:
        where_clauses.append("al.details->>'terminal_id' = :terminal_id")
        params["terminal_id"] = terminal_id
    if cashier_id:
        where_clauses.append("al.user_id = :cashier_id")
        params["cashier_id"] = cashier_id

    where_sql = "WHERE " + " AND ".join(where_clauses)

    # Count (de-duped via DISTINCT so multi-audit-row SOs don't inflate)
    total = g.db.execute(
        text(
            f"""
            SELECT COUNT(DISTINCT so.so_id)
              FROM sales_orders so
              LEFT JOIN audit_log al
                ON al.entity_type = 'SO'
               AND al.entity_id   = so.so_id
               AND al.action_type = :checkout_action
              {where_sql}
            """
        ),
        params,
    ).scalar()
    pages = max(1, math.ceil((total or 0) / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page

    rows = g.db.execute(
        text(
            f"""
            SELECT DISTINCT ON (so.so_id)
                   so.so_id,
                   so.so_number,
                   so.status,
                   so.order_type,
                   so.order_date,
                   so.customer_name,
                   so.customer_phone,
                   so.external_txn_ref,
                   so.warehouse_id,
                   al.user_id AS cashier_id,
                   al.details->>'terminal_id' AS terminal_id,
                   COALESCE((al.details->>'total_cents')::int, 0) AS total_cents,
                   al.details->>'payment_method' AS payment_method,
                   al.created_at AS checkout_at
              FROM sales_orders so
              LEFT JOIN audit_log al
                ON al.entity_type = 'SO'
               AND al.entity_id   = so.so_id
               AND al.action_type = :checkout_action
              {where_sql}
             ORDER BY so.so_id DESC, al.created_at DESC
             LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).fetchall()

    return jsonify({
        "sales_orders": [
            {
                "so_id": r.so_id,
                "so_number": r.so_number,
                "status": r.status,
                "order_type": r.order_type,
                "order_date": r.order_date.isoformat() if r.order_date else None,
                "customer_name": r.customer_name,
                "customer_phone": r.customer_phone,
                "external_txn_ref": r.external_txn_ref,
                "warehouse_id": r.warehouse_id,
                "cashier_id": r.cashier_id,
                "terminal_id": r.terminal_id,
                "total_cents": r.total_cents,
                "payment_method": r.payment_method,
                "checkout_at": r.checkout_at.isoformat() if r.checkout_at else None,
            }
            for r in rows
        ],
        "total": total or 0,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })


# ---------------------------------------------------------------------------
# GET /api/admin/pos/summary
# ---------------------------------------------------------------------------


@admin_bp.route("/pos/summary", methods=["GET"])
@require_auth
@require_admin_or_page_permission("pos-activity")
@with_db
def pos_summary():
    """Today's KPI counters for the dashboard top bar.

    Computed as of `now` in the server's timezone (UTC for ACA deploys —
    document the offset if cashier-day boundaries become important).

    Returns:
        today: {sales_count, sales_total_cents, refund_count, refund_total_cents}
        active_terminals: list of distinct terminal_id seen in the last 24h
    """
    # "Today" boundary: midnight UTC. For SLC retail store, that's ~6pm
    # local the prior day -- if the user wants a Mountain-Time boundary,
    # we'd switch this to AT TIME ZONE 'America/Denver'. Keeping UTC for
    # now to match the rest of the admin surface.
    today_start_utc = "DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')"

    row = g.db.execute(
        text(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE so.order_type = 'sale')
                AS sales_count,
              COUNT(*) FILTER (WHERE so.order_type = 'refund')
                AS refund_count,
              COALESCE(SUM(
                CASE WHEN so.order_type = 'sale'
                  THEN COALESCE((al.details->>'total_cents')::int, 0)
                  ELSE 0
                END
              ), 0) AS sales_total_cents,
              COALESCE(SUM(
                CASE WHEN so.order_type = 'refund'
                  THEN COALESCE((al.details->>'total_cents')::int, 0)
                  ELSE 0
                END
              ), 0) AS refund_total_cents
            FROM sales_orders so
            LEFT JOIN audit_log al
              ON al.entity_type = 'SO'
             AND al.entity_id   = so.so_id
             AND al.action_type = :checkout_action
            WHERE so.order_source = 'pos'
              AND so.order_date >= {today_start_utc}
            """
        ),
        {"checkout_action": ACTION_POS_CHECKOUT},
    ).fetchone()

    # Active terminals in the last 24h (a "registered today" pulse).
    twenty_four_hours_ago = "(NOW() - INTERVAL '24 hours')"
    terminals = g.db.execute(
        text(
            f"""
            SELECT DISTINCT al.details->>'terminal_id' AS terminal_id
              FROM audit_log al
             WHERE al.action_type = :checkout_action
               AND al.created_at >= {twenty_four_hours_ago}
               AND al.details->>'terminal_id' IS NOT NULL
             ORDER BY terminal_id
            """
        ),
        {"checkout_action": ACTION_POS_CHECKOUT},
    ).fetchall()

    return jsonify({
        "today": {
            "sales_count": row.sales_count or 0,
            "sales_total_cents": int(row.sales_total_cents or 0),
            "refund_count": row.refund_count or 0,
            "refund_total_cents": int(row.refund_total_cents or 0),
        },
        "active_terminals": [t.terminal_id for t in terminals if t.terminal_id],
    })
