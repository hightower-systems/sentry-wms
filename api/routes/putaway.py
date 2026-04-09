"""
Put-away endpoints: pending items, preferred bin suggestion, confirm transfer,
and preferred bin management.
"""

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db
from services.audit_service import write_audit_log
from services.inventory_service import move_inventory

putaway_bp = Blueprint("putaway", __name__)


@putaway_bp.route("/pending/<int:warehouse_id>")
@require_auth
@with_db
def pending_putaway(warehouse_id):
    rows = g.db.execute(
        text(
            """
            SELECT inv.inventory_id, inv.item_id, i.sku, i.item_name, i.upc,
                   inv.quantity_on_hand AS quantity, inv.bin_id, b.bin_code,
                   inv.lot_number
            FROM inventory inv
            JOIN items i ON i.item_id = inv.item_id
            JOIN bins b ON b.bin_id = inv.bin_id
            WHERE b.bin_type = 'Staging'
              AND inv.quantity_on_hand > 0
              AND inv.warehouse_id = :warehouse_id
            """
        ),
        {"warehouse_id": warehouse_id},
    ).fetchall()

    return jsonify({
        "pending_items": [
            {
                "inventory_id": r.inventory_id,
                "item_id": r.item_id,
                "sku": r.sku,
                "item_name": r.item_name,
                "upc": r.upc,
                "quantity": r.quantity,
                "bin_id": r.bin_id,
                "bin_code": r.bin_code,
                "lot_number": r.lot_number,
            }
            for r in rows
        ]
    })


@putaway_bp.route("/suggest/<int:item_id>")
@require_auth
@with_db
def suggest_bin(item_id):
    item = g.db.execute(
        text("SELECT item_id, sku, item_name, default_bin_id FROM items WHERE item_id = :item_id"),
        {"item_id": item_id},
    ).fetchone()

    if not item:
        return jsonify({"error": "Item not found"}), 404

    # Query preferred_bins table for priority 1
    preferred = g.db.execute(
        text(
            """
            SELECT pb.preferred_bin_id, pb.bin_id, pb.priority, pb.notes,
                   b.bin_code, b.bin_barcode, z.zone_name
            FROM preferred_bins pb
            JOIN bins b ON b.bin_id = pb.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            WHERE pb.item_id = :item_id
            ORDER BY pb.priority ASC
            LIMIT 1
            """
        ),
        {"item_id": item_id},
    ).fetchone()

    preferred_bin = None
    if preferred:
        preferred_bin = {
            "bin_id": preferred.bin_id,
            "bin_code": preferred.bin_code,
            "bin_barcode": preferred.bin_barcode,
            "zone_name": preferred.zone_name,
            "priority": preferred.priority,
        }

    # Fallback: if no preferred bin, check default_bin_id on items table
    if not preferred_bin and item.default_bin_id:
        default = g.db.execute(
            text(
                """
                SELECT b.bin_id, b.bin_code, b.bin_barcode, z.zone_name
                FROM bins b
                LEFT JOIN zones z ON z.zone_id = b.zone_id
                WHERE b.bin_id = :bin_id
                """
            ),
            {"bin_id": item.default_bin_id},
        ).fetchone()
        if default:
            preferred_bin = {
                "bin_id": default.bin_id,
                "bin_code": default.bin_code,
                "bin_barcode": default.bin_barcode,
                "zone_name": default.zone_name,
                "priority": 1,
            }

    return jsonify({
        "item_id": item.item_id,
        "sku": item.sku,
        "item_name": item.item_name,
        "preferred_bin": preferred_bin,
        # Keep backward-compat key
        "suggested_bin": preferred_bin,
    })


@putaway_bp.route("/confirm", methods=["POST"])
@require_auth
@with_db
def confirm_putaway():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    item_id = data.get("item_id")
    from_bin_id = data.get("from_bin_id")
    to_bin_id = data.get("to_bin_id")
    quantity = data.get("quantity", 0)
    lot_number = data.get("lot_number")

    if not item_id or not from_bin_id or not to_bin_id:
        return jsonify({"error": "item_id, from_bin_id, and to_bin_id are required"}), 400

    if quantity <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400

    if from_bin_id == to_bin_id:
        return jsonify({"error": "from_bin_id and to_bin_id must be different"}), 400

    item = g.db.execute(
        text("SELECT item_id, sku FROM items WHERE item_id = :item_id"),
        {"item_id": item_id},
    ).fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404

    from_bin = g.db.execute(
        text("SELECT bin_id, bin_code, warehouse_id FROM bins WHERE bin_id = :bin_id"),
        {"bin_id": from_bin_id},
    ).fetchone()
    if not from_bin:
        return jsonify({"error": "Source bin not found"}), 404

    to_bin = g.db.execute(
        text("SELECT bin_id, bin_code FROM bins WHERE bin_id = :bin_id"),
        {"bin_id": to_bin_id},
    ).fetchone()
    if not to_bin:
        return jsonify({"error": "Destination bin not found"}), 404

    username = g.current_user["username"]
    warehouse_id = from_bin.warehouse_id

    # 1 & 2. Move inventory (decrement source, upsert destination)
    try:
        move_inventory(g.db, item_id, from_bin_id, to_bin_id, warehouse_id, quantity, lot_number)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 3. Transfer record
    result = g.db.execute(
        text(
            """
            INSERT INTO bin_transfers (item_id, from_bin_id, to_bin_id, warehouse_id, quantity,
                                       transfer_type, lot_number, transferred_by)
            VALUES (:item_id, :from_bin_id, :to_bin_id, :warehouse_id, :quantity,
                    'PUTAWAY', :lot_number, :transferred_by)
            RETURNING transfer_id
            """
        ),
        {
            "item_id": item_id,
            "from_bin_id": from_bin_id,
            "to_bin_id": to_bin_id,
            "warehouse_id": warehouse_id,
            "quantity": quantity,
            "lot_number": lot_number,
            "transferred_by": username,
        },
    )
    transfer_id = result.fetchone()[0]

    # 4. Audit
    write_audit_log(
        g.db,
        action_type="PUTAWAY",
        entity_type="ITEM",
        entity_id=item_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "from_bin_id": from_bin_id,
            "to_bin_id": to_bin_id,
            "quantity": quantity,
            "transfer_id": transfer_id,
        },
    )

    g.db.commit()

    return jsonify({
        "message": "Put-away confirmed",
        "transfer_id": transfer_id,
        "item": item.sku,
        "from_bin": from_bin.bin_code,
        "to_bin": to_bin.bin_code,
        "quantity": quantity,
    })


@putaway_bp.route("/update-preferred", methods=["POST"])
@require_auth
@with_db
def update_preferred():
    """Create or update a preferred bin for an item."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    item_id = data.get("item_id")
    bin_id = data.get("bin_id")
    set_as_primary = data.get("set_as_primary", True)

    if not item_id or not bin_id:
        return jsonify({"error": "item_id and bin_id are required"}), 400

    item = g.db.execute(
        text("SELECT item_id, sku FROM items WHERE item_id = :item_id"),
        {"item_id": item_id},
    ).fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404

    bin_row = g.db.execute(
        text("SELECT bin_id, bin_code FROM bins WHERE bin_id = :bin_id"),
        {"bin_id": bin_id},
    ).fetchone()
    if not bin_row:
        return jsonify({"error": "Bin not found"}), 404

    username = g.current_user["username"]

    # Get current priority-1 bin for audit log
    old_preferred = g.db.execute(
        text(
            """
            SELECT pb.bin_id, b.bin_code
            FROM preferred_bins pb
            JOIN bins b ON b.bin_id = pb.bin_id
            WHERE pb.item_id = :item_id AND pb.priority = 1
            """
        ),
        {"item_id": item_id},
    ).fetchone()

    old_bin_code = old_preferred.bin_code if old_preferred else None

    if set_as_primary:
        # Bump all existing priorities down by 1
        g.db.execute(
            text("UPDATE preferred_bins SET priority = priority + 1, updated_at = NOW() WHERE item_id = :item_id"),
            {"item_id": item_id},
        )

        # Upsert the new bin as priority 1
        existing = g.db.execute(
            text("SELECT preferred_bin_id FROM preferred_bins WHERE item_id = :item_id AND bin_id = :bin_id"),
            {"item_id": item_id, "bin_id": bin_id},
        ).fetchone()

        if existing:
            g.db.execute(
                text("UPDATE preferred_bins SET priority = 1, updated_at = NOW() WHERE preferred_bin_id = :pbid"),
                {"pbid": existing.preferred_bin_id},
            )
        else:
            g.db.execute(
                text(
                    """
                    INSERT INTO preferred_bins (item_id, bin_id, priority, notes)
                    VALUES (:item_id, :bin_id, 1, 'Set via put-away')
                    """
                ),
                {"item_id": item_id, "bin_id": bin_id},
            )

        # Update items.default_bin_id for backward compat
        g.db.execute(
            text("UPDATE items SET default_bin_id = :bin_id, updated_at = NOW() WHERE item_id = :item_id"),
            {"bin_id": bin_id, "item_id": item_id},
        )

    # Audit log
    warehouse_id = g.db.execute(
        text("SELECT warehouse_id FROM bins WHERE bin_id = :bin_id"),
        {"bin_id": bin_id},
    ).scalar()

    write_audit_log(
        g.db,
        action_type="PREFERRED_BIN_UPDATE",
        entity_type="ITEM",
        entity_id=item_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "sku": item.sku,
            "old_bin": old_bin_code,
            "new_bin": bin_row.bin_code,
            "set_as_primary": set_as_primary,
        },
    )

    g.db.commit()

    return jsonify({
        "message": f"Preferred bin for {item.sku} {'set to' if not old_bin_code else 'changed to'} {bin_row.bin_code}",
        "item_id": item_id,
        "bin_id": bin_id,
        "bin_code": bin_row.bin_code,
    })
