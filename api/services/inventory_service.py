"""
Shared inventory manipulation functions used by receiving, putaway, transfers,
and other workflows that touch inventory quantities.

V-030: all inventory mutations below are race-safe. ``add_inventory`` uses
``INSERT ... ON CONFLICT DO UPDATE`` so concurrent callers cannot create
duplicate rows or overwrite each other. ``move_inventory`` locks the source
row with ``SELECT ... FOR UPDATE`` before checking available quantity, so
two concurrent moves from the same bin cannot both pass the
sufficient-stock check.
"""

from sqlalchemy import text


def add_inventory(db, item_id, bin_id, warehouse_id, quantity, lot_number=None):
    """Increment existing inventory or create a new record.

    V-030 race safety: Postgres's default UNIQUE(item_id, bin_id, lot_number)
    treats NULL lot_numbers as distinct, so ON CONFLICT cannot serialize
    concurrent inserts when lot is NULL (the common case). We use a
    transaction-scoped advisory lock keyed on (item_id, bin_id) to
    serialize callers, then the existing SELECT-then-INSERT-or-UPDATE
    flow runs safely. The lock releases automatically at COMMIT or
    ROLLBACK.

    Returns the new quantity_on_hand at (item_id, bin_id, lot_number).
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(:iid, :bid)"),
        {"iid": item_id, "bid": bin_id},
    )

    existing = db.execute(
        text(
            """
            SELECT inventory_id, quantity_on_hand
            FROM inventory
            WHERE item_id = :item_id AND bin_id = :bin_id
              AND lot_number IS NOT DISTINCT FROM :lot_number
            FOR UPDATE
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

    Locks the source row with SELECT ... FOR UPDATE so a concurrent
    move from the same bin cannot also pass the sufficient-stock check.
    Decrements source (deletes row if quantity reaches zero), upserts
    destination via add_inventory's ON CONFLICT path.

    Returns (new_source_qty, new_dest_qty).
    Raises ValueError if insufficient inventory in source bin.
    """
    # V-030: lock the source row until commit so concurrent callers
    # serialize through this critical section.
    source_inv = db.execute(
        text(
            """
            SELECT inventory_id, quantity_on_hand
            FROM inventory
            WHERE item_id = :item_id AND bin_id = :bin_id
              AND lot_number IS NOT DISTINCT FROM :lot_number
            FOR UPDATE
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

    # Upsert destination (atomic via ON CONFLICT).
    new_dest_qty = add_inventory(db, item_id, to_bin_id, warehouse_id, quantity, lot_number)

    return new_source_qty, new_dest_qty
