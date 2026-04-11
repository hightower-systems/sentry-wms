"""
Lookup endpoints: item/bin barcode lookups and text search.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db

lookup_bp = Blueprint("lookup", __name__)


@lookup_bp.route("/item/<barcode>")
@require_auth
@with_db
def lookup_item(barcode):
    barcode = barcode.strip()
    print(f"[LOOKUP] Item lookup received: '{barcode}' (len={len(barcode)})")

    # Look up by UPC, SKU, or barcode_aliases
    item_row = g.db.execute(
        text(
            """
            SELECT item_id, sku, item_name, upc, category, weight_lbs,
                   description, barcode_aliases
            FROM items
            WHERE upc = :barcode
               OR sku = :barcode
               OR barcode_aliases @> CAST(:barcode_json AS jsonb)
            LIMIT 1
            """
        ),
        {"barcode": barcode, "barcode_json": f'["{barcode}"]'},
    ).fetchone()

    if not item_row:
        return jsonify({"error": "Item not found"}), 404

    item = {
        "item_id": item_row.item_id,
        "sku": item_row.sku,
        "item_name": item_row.item_name,
        "upc": item_row.upc,
        "category": item_row.category,
        "weight_lbs": float(item_row.weight_lbs) if item_row.weight_lbs else None,
    }

    location_rows = g.db.execute(
        text(
            """
            SELECT i.bin_id, b.bin_code, b.bin_type, z.zone_name,
                   i.quantity_on_hand, i.quantity_allocated,
                   (i.quantity_on_hand - i.quantity_allocated) AS quantity_available,
                   i.lot_number
            FROM inventory i
            JOIN bins b ON b.bin_id = i.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            WHERE i.item_id = :item_id
            """
        ),
        {"item_id": item_row.item_id},
    ).fetchall()

    locations = [
        {
            "bin_id": r.bin_id,
            "bin_code": r.bin_code,
            "bin_type": r.bin_type,
            "zone_name": r.zone_name,
            "quantity_on_hand": r.quantity_on_hand,
            "quantity_allocated": r.quantity_allocated,
            "quantity_available": r.quantity_available,
            "lot_number": r.lot_number,
        }
        for r in location_rows
    ]

    return jsonify({"item": item, "locations": locations})


@lookup_bp.route("/bin/<barcode>")
@require_auth
@with_db
def lookup_bin(barcode):
    barcode = barcode.strip()
    print(f"[LOOKUP] Bin lookup received: '{barcode}' (len={len(barcode)})")

    bin_row = g.db.execute(
        text(
            """
            SELECT b.bin_id, b.bin_code, b.bin_barcode, b.bin_type,
                   b.aisle, b.row_num, b.level_num, z.zone_name
            FROM bins b
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            WHERE b.bin_barcode = :barcode OR b.bin_code = :barcode
            LIMIT 1
            """
        ),
        {"barcode": barcode},
    ).fetchone()

    if not bin_row:
        return jsonify({"error": "Bin not found"}), 404

    bin_data = {
        "bin_id": bin_row.bin_id,
        "bin_code": bin_row.bin_code,
        "bin_barcode": bin_row.bin_barcode,
        "bin_type": bin_row.bin_type,
        "zone_name": bin_row.zone_name,
        "aisle": bin_row.aisle,
        "row_num": bin_row.row_num,
        "level_num": bin_row.level_num,
    }

    item_rows = g.db.execute(
        text(
            """
            SELECT it.item_id, it.sku, it.item_name, it.upc,
                   inv.quantity_on_hand, inv.quantity_allocated,
                   (inv.quantity_on_hand - inv.quantity_allocated) AS quantity_available,
                   inv.lot_number
            FROM inventory inv
            JOIN items it ON it.item_id = inv.item_id
            WHERE inv.bin_id = :bin_id
            """
        ),
        {"bin_id": bin_row.bin_id},
    ).fetchall()

    items = [
        {
            "item_id": r.item_id,
            "sku": r.sku,
            "item_name": r.item_name,
            "upc": r.upc,
            "quantity_on_hand": r.quantity_on_hand,
            "quantity_allocated": r.quantity_allocated,
            "quantity_available": r.quantity_available,
            "lot_number": r.lot_number,
        }
        for r in item_rows
    ]

    return jsonify({"bin": bin_data, "items": items})


@lookup_bp.route("/so/<barcode>")
@require_auth
@with_db
def lookup_so(barcode):
    """Generic SO lookup  -  returns SO data regardless of status."""
    barcode = barcode.strip()
    print(f"[LOOKUP] SO lookup received: '{barcode}' (len={len(barcode)})")

    so_row = g.db.execute(
        text(
            """
            SELECT so_id, so_number, so_barcode, customer_name, status,
                   warehouse_id, customer_phone, customer_address, ship_address
            FROM sales_orders
            WHERE so_barcode = :barcode OR so_number = :barcode
            LIMIT 1
            """
        ),
        {"barcode": barcode},
    ).fetchone()

    if not so_row:
        return jsonify({"error": "Sales order not found"}), 404

    # Fetch SO lines for detail display
    lines = g.db.execute(
        text("""
            SELECT sol.quantity_ordered, sol.quantity_picked, sol.quantity_packed,
                   sol.quantity_shipped, sol.status AS line_status,
                   i.sku, i.item_name
            FROM sales_order_lines sol
            JOIN items i ON i.item_id = sol.item_id
            WHERE sol.so_id = :sid
            ORDER BY sol.line_number
        """),
        {"sid": so_row.so_id},
    ).fetchall()

    return jsonify({
        "sales_order": {
            "so_id": so_row.so_id,
            "so_number": so_row.so_number,
            "so_barcode": so_row.so_barcode,
            "customer_name": so_row.customer_name,
            "customer_phone": so_row.customer_phone,
            "customer_address": so_row.customer_address,
            "ship_address": so_row.ship_address,
            "status": so_row.status,
            "warehouse_id": so_row.warehouse_id,
            "lines": [
                {
                    "sku": l.sku,
                    "item_name": l.item_name,
                    "quantity_ordered": l.quantity_ordered,
                    "quantity_picked": l.quantity_picked,
                    "quantity_packed": l.quantity_packed,
                    "quantity_shipped": l.quantity_shipped,
                }
                for l in lines
            ],
        }
    })


@lookup_bp.route("/item/search")
@require_auth
@with_db
def search_items():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    rows = g.db.execute(
        text(
            """
            SELECT item_id, sku, item_name, upc, category, weight_lbs
            FROM items
            WHERE sku ILIKE :q OR item_name ILIKE :q OR upc ILIKE :q
            LIMIT 50
            """
        ),
        {"q": f"%{q}%"},
    ).fetchall()

    results = [
        {
            "item_id": r.item_id,
            "sku": r.sku,
            "item_name": r.item_name,
            "upc": r.upc,
            "category": r.category,
            "weight_lbs": float(r.weight_lbs) if r.weight_lbs else None,
        }
        for r in rows
    ]

    return jsonify(results)


@lookup_bp.route("/bin/search")
@require_auth
@with_db
def search_bins():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    rows = g.db.execute(
        text(
            """
            SELECT b.bin_id, b.bin_code, b.bin_barcode, b.bin_type,
                   b.aisle, b.row_num, b.level_num, z.zone_name
            FROM bins b
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            WHERE b.bin_code ILIKE :q OR b.bin_barcode ILIKE :q
            LIMIT 50
            """
        ),
        {"q": f"%{q}%"},
    ).fetchall()

    results = [
        {
            "bin_id": r.bin_id,
            "bin_code": r.bin_code,
            "bin_barcode": r.bin_barcode,
            "bin_type": r.bin_type,
            "zone_name": r.zone_name,
            "aisle": r.aisle,
            "row_num": r.row_num,
            "level_num": r.level_num,
        }
        for r in rows
    ]

    return jsonify(results)
