"""
Lookup endpoints: item/bin barcode lookups and text search.
"""

from flask import Blueprint, jsonify, request
from sqlalchemy import or_, text

from middleware.auth_middleware import require_auth
from models.database import get_db

lookup_bp = Blueprint("lookup", __name__)


@lookup_bp.route("/item/<barcode>")
@require_auth
def lookup_item(barcode):
    db = next(get_db())
    try:
        # Look up by UPC or barcode_aliases
        item_row = db.execute(
            text(
                """
                SELECT item_id, sku, item_name, upc, category, weight_lbs,
                       description, barcode_aliases
                FROM items
                WHERE upc = :barcode
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

        location_rows = db.execute(
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
    finally:
        db.close()


@lookup_bp.route("/bin/<barcode>")
@require_auth
def lookup_bin(barcode):
    db = next(get_db())
    try:
        bin_row = db.execute(
            text(
                """
                SELECT b.bin_id, b.bin_code, b.bin_barcode, b.bin_type,
                       b.aisle, b.row_num, b.level_num, z.zone_name
                FROM bins b
                JOIN zones z ON z.zone_id = b.zone_id
                WHERE b.bin_barcode = :barcode
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

        item_rows = db.execute(
            text(
                """
                SELECT it.item_id, it.sku, it.item_name, it.upc,
                       inv.quantity_on_hand, inv.quantity_allocated,
                       (inv.quantity_on_hand - inv.quantity_allocated) AS quantity_available
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
            }
            for r in item_rows
        ]

        return jsonify({"bin": bin_data, "items": items})
    finally:
        db.close()


@lookup_bp.route("/item/search")
@require_auth
def search_items():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    db = next(get_db())
    try:
        rows = db.execute(
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
    finally:
        db.close()


@lookup_bp.route("/bin/search")
@require_auth
def search_bins():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    db = next(get_db())
    try:
        rows = db.execute(
            text(
                """
                SELECT b.bin_id, b.bin_code, b.bin_barcode, b.bin_type,
                       b.aisle, b.row_num, b.level_num, z.zone_name
                FROM bins b
                JOIN zones z ON z.zone_id = b.zone_id
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
    finally:
        db.close()
