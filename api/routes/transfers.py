"""
Bin transfer endpoint: general-purpose bin-to-bin inventory moves.
"""

import uuid

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from constants import ACTION_TRANSFER
from middleware.auth_middleware import require_auth, check_warehouse_access
from middleware.db import with_db
from schemas.bin_transfer import MoveRequest
from services.audit_service import write_audit_log
from services.inventory_service import move_inventory
from utils.validation import validate_body

transfers_bp = Blueprint("transfers", __name__)


@transfers_bp.route("/move", methods=["POST"])
@require_auth
@validate_body(MoveRequest)
@with_db
def move(validated):
    item_id = validated.item_id
    from_bin_id = validated.from_bin_id
    to_bin_id = validated.to_bin_id
    quantity = validated.quantity
    reason = validated.reason
    lot_number = validated.lot_number

    # Validate item
    item = g.db.execute(
        text("SELECT item_id, sku, item_name FROM items WHERE item_id = :iid"),
        {"iid": item_id},
    ).fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404

    # Validate bins
    from_bin = g.db.execute(
        text("SELECT bin_id, bin_code, warehouse_id FROM bins WHERE bin_id = :bid"),
        {"bid": from_bin_id},
    ).fetchone()
    if not from_bin:
        return jsonify({"error": "Source bin not found"}), 404

    to_bin = g.db.execute(
        text("SELECT bin_id, bin_code, warehouse_id FROM bins WHERE bin_id = :bid"),
        {"bid": to_bin_id},
    ).fetchone()
    if not to_bin:
        return jsonify({"error": "Destination bin not found"}), 404

    if to_bin.warehouse_id != from_bin.warehouse_id:
        return jsonify({"error": "Cross-warehouse moves are not allowed here. Use the admin inter-warehouse transfer."}), 400

    username = g.current_user["username"]
    warehouse_id = from_bin.warehouse_id

    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied

    # 1 & 2. Move inventory (decrement source, upsert destination)
    try:
        new_source_qty, new_dest_qty = move_inventory(
            g.db, item_id, from_bin_id, to_bin_id, warehouse_id, quantity, lot_number
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 3. Create bin_transfers record
    result = g.db.execute(
        text(
            """
            INSERT INTO bin_transfers (item_id, from_bin_id, to_bin_id, warehouse_id, quantity,
                                       transfer_type, lot_number, reason, transferred_by, external_id)
            VALUES (:iid, :from_bid, :to_bid, :wh, :qty, 'MOVE', :lot, :reason, :user, :ext_id)
            RETURNING transfer_id
            """
        ),
        {
            "iid": item_id,
            "from_bid": from_bin_id,
            "to_bid": to_bin_id,
            "wh": warehouse_id,
            "qty": quantity,
            "lot": lot_number,
            "reason": reason,
            "user": username,
            "ext_id": str(uuid.uuid4()),
        },
    )
    transfer_id = result.fetchone()[0]

    # 4. Audit log
    write_audit_log(
        g.db,
        action_type=ACTION_TRANSFER,
        entity_type="ITEM",
        entity_id=item_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "from_bin_id": from_bin_id,
            "from_bin_code": from_bin.bin_code,
            "to_bin_id": to_bin_id,
            "to_bin_code": to_bin.bin_code,
            "quantity": quantity,
            "reason": reason,
            "transfer_id": transfer_id,
        },
    )

    # 5. Commit
    g.db.commit()

    return jsonify({
        "message": "Transfer completed",
        "transfer_id": transfer_id,
        "item": {
            "sku": item.sku,
            "item_name": item.item_name,
        },
        "from_bin": {
            "bin_code": from_bin.bin_code,
            "remaining_quantity": new_source_qty,
        },
        "to_bin": {
            "bin_code": to_bin.bin_code,
            "new_quantity": new_dest_qty,
        },
        "quantity_moved": quantity,
    })
