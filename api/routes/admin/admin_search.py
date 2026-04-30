"""Global search endpoint backing the TopBar search dropdown (#163).

Single GET that fans out across items, bins, purchase_orders,
sales_orders and the denormalized customer columns on sales_orders.
Bins, POs and SOs are warehouse-scoped when warehouse_id is supplied;
items are global by design (no warehouse_id column). Customers come
from a DISTINCT projection over sales_orders.customer_name within
warehouse scope; selection routes to the SO list filtered by name
since there is no customers table.
"""

from flask import g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from routes.admin import admin_bp


_MIN_QUERY_LENGTH = 2
_PER_TYPE_LIMIT = 10
_TOTAL_LIMIT = 50


def _warehouse_id_or_none() -> int | None:
    raw = request.args.get("warehouse_id")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _search_items(pattern: str) -> list[dict]:
    rows = g.db.execute(
        text(
            """
            SELECT item_id, sku, item_name
              FROM items
             WHERE sku ILIKE :p OR item_name ILIKE :p OR upc ILIKE :p
             ORDER BY sku
             LIMIT :limit
            """
        ),
        {"p": pattern, "limit": _PER_TYPE_LIMIT},
    ).fetchall()
    return [
        {
            "type": "item",
            "id": int(r.item_id),
            "label": r.sku,
            "sublabel": r.item_name,
        }
        for r in rows
    ]


def _search_bins(pattern: str, warehouse_id: int | None) -> list[dict]:
    params: dict = {"p": pattern, "limit": _PER_TYPE_LIMIT}
    where = "(b.bin_code ILIKE :p OR b.bin_barcode ILIKE :p)"
    if warehouse_id is not None:
        where += " AND b.warehouse_id = :wid"
        params["wid"] = warehouse_id
    rows = g.db.execute(
        text(
            f"""
            SELECT b.bin_id, b.bin_code, b.bin_barcode,
                   w.warehouse_code
              FROM bins b
              JOIN warehouses w ON w.warehouse_id = b.warehouse_id
             WHERE {where}
             ORDER BY b.bin_code
             LIMIT :limit
            """
        ),
        params,
    ).fetchall()
    return [
        {
            "type": "bin",
            "id": int(r.bin_id),
            "label": r.bin_code,
            "sublabel": f"{r.warehouse_code} - {r.bin_barcode}",
        }
        for r in rows
    ]


def _search_purchase_orders(pattern: str, warehouse_id: int | None) -> list[dict]:
    params: dict = {"p": pattern, "limit": _PER_TYPE_LIMIT}
    where = "(po_number ILIKE :p OR vendor_name ILIKE :p)"
    if warehouse_id is not None:
        where += " AND warehouse_id = :wid"
        params["wid"] = warehouse_id
    rows = g.db.execute(
        text(
            f"""
            SELECT po_id, po_number, vendor_name, status
              FROM purchase_orders
             WHERE {where}
             ORDER BY created_at DESC
             LIMIT :limit
            """
        ),
        params,
    ).fetchall()
    return [
        {
            "type": "po",
            "id": int(r.po_id),
            "label": r.po_number,
            "sublabel": (
                f"{r.vendor_name} ({r.status})" if r.vendor_name else r.status
            ),
        }
        for r in rows
    ]


def _search_sales_orders(pattern: str, warehouse_id: int | None) -> list[dict]:
    params: dict = {"p": pattern, "limit": _PER_TYPE_LIMIT}
    where = "(so_number ILIKE :p OR customer_name ILIKE :p)"
    if warehouse_id is not None:
        where += " AND warehouse_id = :wid"
        params["wid"] = warehouse_id
    rows = g.db.execute(
        text(
            f"""
            SELECT so_id, so_number, customer_name, status
              FROM sales_orders
             WHERE {where}
             ORDER BY created_at DESC
             LIMIT :limit
            """
        ),
        params,
    ).fetchall()
    return [
        {
            "type": "so",
            "id": int(r.so_id),
            "label": r.so_number,
            "sublabel": (
                f"{r.customer_name} ({r.status})" if r.customer_name else r.status
            ),
        }
        for r in rows
    ]


def _search_customers(pattern: str, warehouse_id: int | None) -> list[dict]:
    """Customer is a denormalized field on sales_orders, not a first-class
    table. Project DISTINCT customer_name within the search and route
    the UI to the SO list filtered by that name."""
    params: dict = {"p": pattern, "limit": _PER_TYPE_LIMIT}
    where = "customer_name ILIKE :p AND customer_name IS NOT NULL"
    if warehouse_id is not None:
        where += " AND warehouse_id = :wid"
        params["wid"] = warehouse_id
    rows = g.db.execute(
        text(
            f"""
            SELECT customer_name, COUNT(*) AS so_count,
                   MAX(customer_id) AS sample_customer_id
              FROM sales_orders
             WHERE {where}
             GROUP BY customer_name
             ORDER BY customer_name
             LIMIT :limit
            """
        ),
        params,
    ).fetchall()
    return [
        {
            "type": "customer",
            "id": r.customer_name,
            "label": r.customer_name,
            "sublabel": (
                f"{int(r.so_count)} order{'s' if int(r.so_count) != 1 else ''}"
            ),
        }
        for r in rows
    ]


@admin_bp.route("/search", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def global_search():
    raw_q = (request.args.get("q") or "").strip()
    if len(raw_q) < _MIN_QUERY_LENGTH:
        return (
            jsonify(
                {
                    "error": "min_length",
                    "min_length": _MIN_QUERY_LENGTH,
                    "detail": (
                        f"q must be at least {_MIN_QUERY_LENGTH} characters."
                    ),
                }
            ),
            400,
        )

    warehouse_id = _warehouse_id_or_none()
    pattern = f"%{raw_q}%"

    results: list[dict] = []
    results.extend(_search_items(pattern))
    results.extend(_search_bins(pattern, warehouse_id))
    results.extend(_search_purchase_orders(pattern, warehouse_id))
    results.extend(_search_sales_orders(pattern, warehouse_id))
    results.extend(_search_customers(pattern, warehouse_id))

    return jsonify(
        {
            "query": raw_q,
            "warehouse_id": warehouse_id,
            "results": results[:_TOTAL_LIMIT],
        }
    )
