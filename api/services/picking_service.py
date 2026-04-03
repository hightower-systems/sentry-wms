"""
Core picking business logic - batch creation, pick confirmation, short picks, batch completion.
"""

from datetime import datetime, timezone

from sqlalchemy import text

from services.audit_service import write_audit_log


def create_pick_batch(db, so_identifiers, warehouse_id, username):
    # 1. Resolve SOs
    sales_orders = []
    for ident in so_identifiers:
        so = db.execute(
            text(
                """
                SELECT so_id, so_number, so_barcode, status, warehouse_id
                FROM sales_orders
                WHERE (so_number = :ident OR so_barcode = :ident)
                  AND warehouse_id = :wh
                LIMIT 1
                """
            ),
            {"ident": ident, "wh": warehouse_id},
        ).fetchone()

        if not so:
            raise ValueError(f"Sales order '{ident}' not found")
        if so.status != "OPEN":
            raise ValueError(f"Sales order '{ident}' status is {so.status}, must be OPEN")
        sales_orders.append(so)

    # 2. Generate batch number
    now = datetime.now(timezone.utc)
    batch_number = f"BATCH-{now.strftime('%Y%m%d-%H%M%S')}"

    # 3. Create pick_batches record
    result = db.execute(
        text(
            """
            INSERT INTO pick_batches (batch_number, warehouse_id, assigned_to, status)
            VALUES (:batch_number, :warehouse_id, :assigned_to, 'OPEN')
            RETURNING batch_id
            """
        ),
        {"batch_number": batch_number, "warehouse_id": warehouse_id, "assigned_to": username},
    )
    batch_id = result.fetchone()[0]

    # 4. Create pick_batch_orders and assign totes
    orders_info = []
    for idx, so in enumerate(sales_orders, 1):
        tote_number = f"TOTE-{idx}"
        db.execute(
            text(
                """
                INSERT INTO pick_batch_orders (batch_id, so_id, tote_number)
                VALUES (:batch_id, :so_id, :tote_number)
                """
            ),
            {"batch_id": batch_id, "so_id": so.so_id, "tote_number": tote_number},
        )
        orders_info.append({"so_id": so.so_id, "so_number": so.so_number, "tote_number": tote_number})

    # 5. For each SO, for each line, allocate inventory and create pick tasks
    total_items = 0
    for order in orders_info:
        so_id = order["so_id"]
        tote_number = order["tote_number"]

        lines = db.execute(
            text(
                """
                SELECT so_line_id, item_id, quantity_ordered, quantity_allocated
                FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_ordered > quantity_allocated
                """
            ),
            {"so_id": so_id},
        ).fetchall()

        for line in lines:
            needed = line.quantity_ordered - line.quantity_allocated
            if needed <= 0:
                continue

            # Find available inventory sorted by bin type preference, then FIFO
            inv_rows = db.execute(
                text(
                    """
                    SELECT inv.inventory_id, inv.bin_id, inv.quantity_on_hand, inv.quantity_allocated,
                           (inv.quantity_on_hand - inv.quantity_allocated) AS available,
                           b.pick_sequence, b.bin_type, inv.lot_number
                    FROM inventory inv
                    JOIN bins b ON b.bin_id = inv.bin_id
                    WHERE inv.item_id = :item_id
                      AND inv.warehouse_id = :wh
                      AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                      AND b.bin_type NOT IN ('INBOUND_STAGING', 'OUTBOUND_STAGING')
                    ORDER BY
                      CASE b.bin_type
                        WHEN 'PICKING' THEN 1
                        WHEN 'STANDARD' THEN 2
                        ELSE 3
                      END,
                      inv.updated_at ASC
                    """
                ),
                {"item_id": line.item_id, "wh": warehouse_id},
            ).fetchall()

            remaining = needed
            for inv in inv_rows:
                if remaining <= 0:
                    break

                take = min(remaining, inv.available)

                # Increment inventory.quantity_allocated
                db.execute(
                    text(
                        "UPDATE inventory SET quantity_allocated = quantity_allocated + :qty WHERE inventory_id = :inv_id"
                    ),
                    {"qty": take, "inv_id": inv.inventory_id},
                )

                # Increment sales_order_lines.quantity_allocated
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_allocated = quantity_allocated + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": take, "sol_id": line.so_line_id},
                )

                # Create pick_tasks record
                db.execute(
                    text(
                        """
                        INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, bin_id,
                                                quantity_to_pick, pick_sequence, tote_number, status)
                        VALUES (:batch_id, :so_id, :so_line_id, :item_id, :bin_id,
                                :qty, :pick_seq, :tote, 'PENDING')
                        """
                    ),
                    {
                        "batch_id": batch_id,
                        "so_id": so_id,
                        "so_line_id": line.so_line_id,
                        "item_id": line.item_id,
                        "bin_id": inv.bin_id,
                        "qty": take,
                        "pick_seq": inv.pick_sequence,
                        "tote": tote_number,
                    },
                )

                total_items += take
                remaining -= take

    # 6. Update each SO status to ALLOCATED
    for order in orders_info:
        db.execute(
            text("UPDATE sales_orders SET status = 'ALLOCATED' WHERE so_id = :so_id"),
            {"so_id": order["so_id"]},
        )

    # 7. Update batch totals
    db.execute(
        text(
            "UPDATE pick_batches SET total_orders = :orders, total_items = :items WHERE batch_id = :bid"
        ),
        {"orders": len(sales_orders), "items": total_items, "bid": batch_id},
    )

    # 8. Get the full task list
    tasks = _get_tasks_for_batch(db, batch_id)

    db.commit()

    return {
        "batch_id": batch_id,
        "batch_number": batch_number,
        "status": "OPEN",
        "total_orders": len(sales_orders),
        "total_items": total_items,
        "orders": [{"so_number": o["so_number"], "tote_number": o["tote_number"]} for o in orders_info],
        "tasks": tasks,
    }


def get_batch_tasks(db, batch_id):
    batch = db.execute(
        text(
            """
            SELECT batch_id, batch_number, status, assigned_to, total_orders, total_items,
                   created_at, started_at, completed_at, warehouse_id
            FROM pick_batches WHERE batch_id = :bid
            """
        ),
        {"bid": batch_id},
    ).fetchone()

    if not batch:
        return None

    orders = db.execute(
        text(
            """
            SELECT so.so_number, pbo.tote_number
            FROM pick_batch_orders pbo
            JOIN sales_orders so ON so.so_id = pbo.so_id
            WHERE pbo.batch_id = :bid
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    tasks = _get_tasks_for_batch(db, batch_id)

    return {
        "batch_id": batch.batch_id,
        "batch_number": batch.batch_number,
        "status": batch.status,
        "total_orders": batch.total_orders,
        "total_items": batch.total_items,
        "orders": [{"so_number": o.so_number, "tote_number": o.tote_number} for o in orders],
        "tasks": tasks,
    }


def get_next_task(db, batch_id):
    row = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.pick_sequence, pt.quantity_to_pick, pt.quantity_picked,
                   pt.tote_number, pt.status,
                   b.bin_code, b.bin_barcode, b.aisle, b.row_num, b.level_num,
                   i.sku, i.item_name, i.upc,
                   so.so_number
            FROM pick_tasks pt
            JOIN bins b ON b.bin_id = pt.bin_id
            JOIN items i ON i.item_id = pt.item_id
            JOIN sales_orders so ON so.so_id = pt.so_id
            WHERE pt.batch_id = :bid AND pt.status = 'PENDING'
            ORDER BY pt.pick_sequence ASC
            LIMIT 1
            """
        ),
        {"bid": batch_id},
    ).fetchone()

    if not row:
        return None

    return _task_row_to_dict(row)


def confirm_pick(db, pick_task_id, scanned_barcode, quantity_picked, username):
    # 1. Load pick task
    task = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.batch_id, pt.so_id, pt.so_line_id, pt.item_id,
                   pt.bin_id, pt.quantity_to_pick, pt.status, pt.tote_number
            FROM pick_tasks pt
            WHERE pt.pick_task_id = :tid
            """
        ),
        {"tid": pick_task_id},
    ).fetchone()

    if not task:
        raise ValueError("Pick task not found")
    if task.status != "PENDING":
        raise ValueError(f"Pick task is already {task.status}")

    # 2. Validate barcode
    item = db.execute(
        text("SELECT item_id, sku, upc, barcode_aliases FROM items WHERE item_id = :iid"),
        {"iid": task.item_id},
    ).fetchone()

    if not _barcode_matches(scanned_barcode, item.upc, item.barcode_aliases):
        raise BarcodeError(f"Wrong item scanned. Expected SKU: {item.sku}")

    # 3. Update pick task
    db.execute(
        text(
            """
            UPDATE pick_tasks
            SET status = 'PICKED', quantity_picked = :qty, picked_by = :user,
                picked_at = NOW(), scan_confirmed = TRUE
            WHERE pick_task_id = :tid
            """
        ),
        {"qty": quantity_picked, "user": username, "tid": pick_task_id},
    )

    # 4. Update sales_order_lines.quantity_picked
    db.execute(
        text(
            "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
        ),
        {"qty": quantity_picked, "sol_id": task.so_line_id},
    )

    # 5. Update inventory
    db.execute(
        text(
            """
            UPDATE inventory
            SET quantity_on_hand = quantity_on_hand - :picked,
                quantity_allocated = quantity_allocated - :allocated,
                updated_at = NOW()
            WHERE item_id = :iid AND bin_id = :bid
            """
        ),
        {"picked": quantity_picked, "allocated": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
    )

    # 6. Get remaining count
    remaining = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid AND status = 'PENDING'"),
        {"bid": task.batch_id},
    ).scalar()

    # 7. Audit log
    batch = db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": task.batch_id},
    ).fetchone()

    write_audit_log(
        db,
        action_type="PICK",
        entity_type="SO",
        entity_id=task.so_id,
        user_id=username,
        warehouse_id=batch.warehouse_id,
        details={
            "pick_task_id": pick_task_id,
            "item_id": task.item_id,
            "sku": item.sku,
            "quantity_picked": quantity_picked,
            "bin_id": task.bin_id,
            "batch_id": task.batch_id,
        },
    )

    db.commit()

    # 8. Get bin info for response
    bin_row = db.execute(
        text("SELECT bin_code FROM bins WHERE bin_id = :bid"),
        {"bid": task.bin_id},
    ).fetchone()

    return {
        "task": {
            "pick_task_id": pick_task_id,
            "status": "PICKED",
            "sku": item.sku,
            "quantity_picked": quantity_picked,
            "bin_code": bin_row.bin_code,
            "tote_number": task.tote_number,
        },
        "remaining_tasks": remaining,
    }


def short_pick(db, pick_task_id, quantity_available, username):
    # 1. Load pick task
    task = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.batch_id, pt.so_id, pt.so_line_id, pt.item_id,
                   pt.bin_id, pt.quantity_to_pick, pt.status, pt.tote_number
            FROM pick_tasks pt
            WHERE pt.pick_task_id = :tid
            """
        ),
        {"tid": pick_task_id},
    ).fetchone()

    if not task:
        raise ValueError("Pick task not found")
    if task.status != "PENDING":
        raise ValueError(f"Pick task is already {task.status}")

    shortage = task.quantity_to_pick - quantity_available

    # 2. Update pick task
    db.execute(
        text(
            """
            UPDATE pick_tasks
            SET status = 'SHORT', quantity_picked = :qty, picked_by = :user, picked_at = NOW()
            WHERE pick_task_id = :tid
            """
        ),
        {"qty": quantity_available, "user": username, "tid": pick_task_id},
    )

    # 3. Update inventory
    db.execute(
        text(
            """
            UPDATE inventory
            SET quantity_on_hand = quantity_on_hand - :picked,
                quantity_allocated = quantity_allocated - :allocated,
                updated_at = NOW()
            WHERE item_id = :iid AND bin_id = :bid
            """
        ),
        {"picked": quantity_available, "allocated": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
    )

    # 4. Update sales_order_lines.quantity_picked
    db.execute(
        text(
            "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
        ),
        {"qty": quantity_available, "sol_id": task.so_line_id},
    )

    # 5. Audit log
    item = db.execute(
        text("SELECT sku FROM items WHERE item_id = :iid"),
        {"iid": task.item_id},
    ).fetchone()

    batch = db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": task.batch_id},
    ).fetchone()

    write_audit_log(
        db,
        action_type="PICK",
        entity_type="SO",
        entity_id=task.so_id,
        user_id=username,
        warehouse_id=batch.warehouse_id,
        details={
            "pick_task_id": pick_task_id,
            "item_id": task.item_id,
            "sku": item.sku,
            "quantity_to_pick": task.quantity_to_pick,
            "quantity_picked": quantity_available,
            "shortage": shortage,
            "bin_id": task.bin_id,
            "batch_id": task.batch_id,
            "type": "SHORT_PICK",
        },
    )

    db.commit()

    bin_row = db.execute(
        text("SELECT bin_code FROM bins WHERE bin_id = :bid"),
        {"bid": task.bin_id},
    ).fetchone()

    return {
        "task": {
            "pick_task_id": pick_task_id,
            "status": "SHORT",
            "sku": item.sku,
            "quantity_to_pick": task.quantity_to_pick,
            "quantity_picked": quantity_available,
            "shortage": shortage,
            "bin_code": bin_row.bin_code,
        },
    }


def complete_batch(db, batch_id, username):
    # 1. Load batch
    batch = db.execute(
        text("SELECT batch_id, batch_number, status, warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).fetchone()

    if not batch:
        raise ValueError("Batch not found")

    # Check all tasks are in terminal state
    pending_count = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid AND status = 'PENDING'"),
        {"bid": batch_id},
    ).scalar()

    if pending_count > 0:
        raise ValueError(f"Cannot complete batch - {pending_count} tasks still pending")

    # 2. Update batch
    db.execute(
        text(
            "UPDATE pick_batches SET status = 'COMPLETED', completed_at = NOW() WHERE batch_id = :bid"
        ),
        {"bid": batch_id},
    )

    # 3. Update each SO to PICKING
    so_rows = db.execute(
        text(
            """
            SELECT pbo.so_id, so.so_number
            FROM pick_batch_orders pbo
            JOIN sales_orders so ON so.so_id = pbo.so_id
            WHERE pbo.batch_id = :bid
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    for so in so_rows:
        db.execute(
            text("UPDATE sales_orders SET status = 'PICKING', picked_at = NOW() WHERE so_id = :so_id"),
            {"so_id": so.so_id},
        )

    # 4. Audit log
    write_audit_log(
        db,
        action_type="PICK",
        entity_type="BATCH",
        entity_id=batch_id,
        user_id=username,
        warehouse_id=batch.warehouse_id,
        details={"batch_number": batch.batch_number, "so_count": len(so_rows)},
    )

    # 5. Summary
    task_stats = db.execute(
        text(
            """
            SELECT COALESCE(SUM(quantity_picked), 0) AS total_picked,
                   COUNT(*) FILTER (WHERE status = 'SHORT') AS total_shorts
            FROM pick_tasks WHERE batch_id = :bid
            """
        ),
        {"bid": batch_id},
    ).fetchone()

    db.commit()

    return {
        "batch_id": batch_id,
        "batch_number": batch.batch_number,
        "summary": {
            "total_orders": len(so_rows),
            "total_items_picked": task_stats.total_picked,
            "total_shorts": task_stats.total_shorts,
            "orders": [{"so_number": so.so_number, "status": "PICKING"} for so in so_rows],
        },
    }


# --- Helpers ---

def _get_tasks_for_batch(db, batch_id):
    rows = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.pick_sequence, pt.quantity_to_pick, pt.quantity_picked,
                   pt.tote_number, pt.status,
                   b.bin_code, b.bin_barcode, b.aisle, b.row_num, b.level_num,
                   i.sku, i.item_name, i.upc,
                   so.so_number
            FROM pick_tasks pt
            JOIN bins b ON b.bin_id = pt.bin_id
            JOIN items i ON i.item_id = pt.item_id
            JOIN sales_orders so ON so.so_id = pt.so_id
            WHERE pt.batch_id = :bid
            ORDER BY pt.pick_sequence ASC, b.bin_code ASC
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    return [_task_row_to_dict(r) for r in rows]


def _task_row_to_dict(row):
    return {
        "pick_task_id": row.pick_task_id,
        "pick_sequence": row.pick_sequence,
        "bin_code": row.bin_code,
        "bin_barcode": row.bin_barcode,
        "aisle": row.aisle,
        "row_num": row.row_num,
        "level_num": row.level_num,
        "sku": row.sku,
        "item_name": row.item_name,
        "upc": row.upc,
        "quantity_to_pick": row.quantity_to_pick,
        "tote_number": row.tote_number,
        "so_number": row.so_number,
        "status": row.status,
    }


def _barcode_matches(scanned, upc, barcode_aliases):
    if scanned == upc:
        return True
    if barcode_aliases and isinstance(barcode_aliases, list):
        return scanned in barcode_aliases
    return False


class BarcodeError(Exception):
    """Raised when a scanned barcode doesn't match the expected item."""
    pass
