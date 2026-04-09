"""
Shared inventory manipulation functions used by receiving, putaway, transfers,
and other workflows that touch inventory quantities.
"""

from sqlalchemy import text


def add_inventory(db, item_id, bin_id, warehouse_id, quantity, lot_number=None):
    """Increment existing inventory or create a new record.

    Used by receiving to add stock into a bin.
    Returns the new quantity_on_hand at (item_id, bin_id, lot_number).
    """
    existing = db.execute(
        text(
            """
            SELECT inventory_id, quantity_on_hand
            FROM inventory
            WHERE item_id = :item_id AND bin_id = :bin_id
              AND lot_number IS NOT DISTINCT FROM :lot_number
            """
        ),
        {"item_id": item_id, "bin_id": bin_id, "lot_number": lot_number},
    ).fetchone()

    if existing:
        new_qty = existing.quantity_on_hand + quantity
        db.execute(
            text(
                """
                UPDATE inventory
                SET quantity_on_hand = quantity_on_hand + :qty, updated_at = NOW()
                WHERE inventory_id = :inv_id
                """
            ),
            {"qty": quantity, "inv_id": existing.inventory_id},
        )
        return new_qty
    else:
        db.execute(
            text(
                """
                INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, lot_number)
                VALUES (:item_id, :bin_id, :warehouse_id, :qty, :lot_number)
                """
            ),
            {
                "item_id": item_id,
                "bin_id": bin_id,
                "warehouse_id": warehouse_id,
                "qty": quantity,
                "lot_number": lot_number,
            },
        )
        return quantity


def move_inventory(db, item_id, from_bin_id, to_bin_id, warehouse_id, quantity, lot_number=None):
    """Atomic bin-to-bin inventory transfer.

    Decrements source (deletes row if quantity reaches zero), upserts destination.
    Returns (new_source_qty, new_dest_qty).
    Raises ValueError if insufficient inventory in source bin.
    """
    # Check source inventory
    source_inv = db.execute(
        text(
            """
            SELECT inventory_id, quantity_on_hand
            FROM inventory
            WHERE item_id = :item_id AND bin_id = :bin_id
              AND lot_number IS NOT DISTINCT FROM :lot_number
            """
        ),
        {"item_id": item_id, "bin_id": from_bin_id, "lot_number": lot_number},
    ).fetchone()

    if not source_inv or source_inv.quantity_on_hand < quantity:
        available = source_inv.quantity_on_hand if source_inv else 0
        raise ValueError(f"Insufficient inventory in source bin. Available: {available}")

    # Decrement source
    new_source_qty = source_inv.quantity_on_hand - quantity
    if new_source_qty == 0:
        db.execute(
            text("DELETE FROM inventory WHERE inventory_id = :inv_id"),
            {"inv_id": source_inv.inventory_id},
        )
    else:
        db.execute(
            text(
                "UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"
            ),
            {"qty": new_source_qty, "inv_id": source_inv.inventory_id},
        )

    # Upsert destination
    new_dest_qty = add_inventory(db, item_id, to_bin_id, warehouse_id, quantity, lot_number)

    return new_source_qty, new_dest_qty
