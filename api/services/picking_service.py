"""
Core picking business logic - batch creation, wave picking, pick confirmation,
short picks, batch completion.
"""

from datetime import datetime, timezone

from sqlalchemy import text

from services.audit_service import write_audit_log
from services.connector_stub import enrich_order

from constants import (
    BATCH_OPEN, BATCH_IN_PROGRESS, BATCH_COMPLETED,
    SO_OPEN, SO_PICKING, SO_PICKED,
    TASK_PENDING, TASK_PICKED, TASK_SHORT, TASK_SKIPPED,
    ACTION_PICK,
    BIN_PICKABLE, BIN_PICKABLE_STAGING,
)


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
        if so.status != SO_OPEN:
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
            VALUES (:batch_number, :warehouse_id, :assigned_to, :status)
            RETURNING batch_id
            """
        ),
        {"batch_number": batch_number, "warehouse_id": warehouse_id, "assigned_to": username, "status": BATCH_OPEN},
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
            # V-030: lock every candidate inventory row before reading
            # quantity_allocated so two concurrent batch-creates cannot
            # both allocate the same stock. FOR UPDATE OF inv restricts
            # the lock to the inventory table (bins is read-only here).
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
                      AND b.bin_type IN (:bin_pickable, :bin_pickable_staging)
                    ORDER BY
                      b.pick_sequence ASC,
                      inv.updated_at ASC
                    FOR UPDATE OF inv
                    """
                ),
                {"item_id": line.item_id, "wh": warehouse_id, "bin_pickable": BIN_PICKABLE, "bin_pickable_staging": BIN_PICKABLE_STAGING},
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
                                :qty, :pick_seq, :tote, :task_status)
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
                        "task_status": TASK_PENDING,
                    },
                )

                total_items += take
                remaining -= take

    # 6. Update each SO status to PICKING
    for order in orders_info:
        db.execute(
            text("UPDATE sales_orders SET status = :status WHERE so_id = :so_id"),
            {"so_id": order["so_id"], "status": SO_PICKING},
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
        "status": BATCH_OPEN,
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
                   so.so_number,
                   z.zone_name
            FROM pick_tasks pt
            JOIN bins b ON b.bin_id = pt.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            JOIN items i ON i.item_id = pt.item_id
            JOIN sales_orders so ON so.so_id = pt.so_id
            WHERE pt.batch_id = :bid AND pt.status = :task_pending
            ORDER BY pt.pick_sequence ASC
            LIMIT 1
            """
        ),
        {"bid": batch_id, "task_pending": TASK_PENDING},
    ).fetchone()

    if not row:
        return None

    result = _task_row_to_dict(row)

    # Add contributing_orders for wave picks
    result["contributing_orders"] = _get_contributing_orders(db, row.pick_task_id)

    # Add pick_number / total_picks
    pick_number = db.execute(
        text(
            """
            SELECT COUNT(*) FROM pick_tasks
            WHERE batch_id = :bid AND status != :task_pending
            """
        ),
        {"bid": batch_id, "task_pending": TASK_PENDING},
    ).scalar()
    total_picks = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).scalar()
    result["pick_number"] = pick_number + 1
    result["total_picks"] = total_picks

    return result


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
    if task.status != TASK_PENDING:
        raise ValueError(f"Pick task is already {task.status}")

    # Cap quantity to task requirement to prevent over-pick
    if quantity_picked > task.quantity_to_pick:
        raise ValueError(
            f"Cannot pick {quantity_picked} - task only requires {task.quantity_to_pick}"
        )

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
            SET status = :task_status, quantity_picked = :qty, picked_by = :user,
                picked_at = NOW(), scan_confirmed = TRUE
            WHERE pick_task_id = :tid
            """
        ),
        {"qty": quantity_picked, "user": username, "tid": pick_task_id, "task_status": TASK_PICKED},
    )

    # 4. Update sales_order_lines.quantity_picked (wave or standard)
    breakdown = db.execute(
        text("SELECT id, so_id, so_line_id, quantity FROM wave_pick_breakdown WHERE pick_task_id = :tid ORDER BY so_id"),
        {"tid": pick_task_id},
    ).fetchall()

    if breakdown:
        # Wave pick - update each contributing SO line
        for bd in breakdown:
            db.execute(
                text(
                    "UPDATE wave_pick_breakdown SET quantity_picked = quantity WHERE id = :bid"
                ),
                {"bid": bd.id},
            )
            db.execute(
                text(
                    "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
                ),
                {"qty": bd.quantity, "sol_id": bd.so_line_id},
            )
    else:
        # Standard pick - single SO line
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
            ),
            {"qty": quantity_picked, "sol_id": task.so_line_id},
        )

    # 5. Update inventory (floor at zero for safety)
    db.execute(
        text(
            """
            UPDATE inventory
            SET quantity_on_hand = GREATEST(0, quantity_on_hand - :picked),
                quantity_allocated = GREATEST(0, quantity_allocated - :allocated),
                updated_at = NOW()
            WHERE item_id = :iid AND bin_id = :bid
            """
        ),
        {"picked": quantity_picked, "allocated": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
    )

    # 6. Get remaining count
    remaining = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid AND status = :task_status"),
        {"bid": task.batch_id, "task_status": TASK_PENDING},
    ).scalar()

    # 7. Audit log
    batch = db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": task.batch_id},
    ).fetchone()

    write_audit_log(
        db,
        action_type=ACTION_PICK,
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
            "status": TASK_PICKED,
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
    if task.status != TASK_PENDING:
        raise ValueError(f"Pick task is already {task.status}")

    if quantity_available > task.quantity_to_pick:
        raise ValueError(
            f"Available quantity {quantity_available} exceeds task requirement "
            f"{task.quantity_to_pick} - use confirm instead"
        )

    shortage = task.quantity_to_pick - quantity_available

    # 2. Update pick task
    db.execute(
        text(
            """
            UPDATE pick_tasks
            SET status = :task_status, quantity_picked = :qty, picked_by = :user, picked_at = NOW()
            WHERE pick_task_id = :tid
            """
        ),
        {"qty": quantity_available, "user": username, "tid": pick_task_id, "task_status": TASK_SHORT},
    )

    # 3. Update inventory (floor at zero for safety)
    db.execute(
        text(
            """
            UPDATE inventory
            SET quantity_on_hand = GREATEST(0, quantity_on_hand - :picked),
                quantity_allocated = GREATEST(0, quantity_allocated - :allocated),
                updated_at = NOW()
            WHERE item_id = :iid AND bin_id = :bid
            """
        ),
        {"picked": quantity_available, "allocated": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
    )

    # 4. Update sales_order_lines.quantity_picked (wave or standard)
    breakdown = db.execute(
        text("SELECT id, so_id, so_line_id, quantity FROM wave_pick_breakdown WHERE pick_task_id = :tid ORDER BY so_id"),
        {"tid": pick_task_id},
    ).fetchall()

    if breakdown:
        # Wave pick - distribute available quantity FIFO by SO ID
        remaining_to_give = quantity_available
        for bd in breakdown:
            give = min(remaining_to_give, bd.quantity)
            short_qty = bd.quantity - give
            db.execute(
                text(
                    "UPDATE wave_pick_breakdown SET quantity_picked = :picked, short_quantity = :short WHERE id = :bid"
                ),
                {"picked": give, "short": short_qty, "bid": bd.id},
            )
            if give > 0:
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": give, "sol_id": bd.so_line_id},
                )
            remaining_to_give -= give
    else:
        # Standard pick - single SO line
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
        action_type=ACTION_PICK,
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
            "status": TASK_SHORT,
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
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid AND status = :task_status"),
        {"bid": batch_id, "task_status": TASK_PENDING},
    ).scalar()

    if pending_count > 0:
        raise ValueError(f"Cannot complete batch - {pending_count} tasks still pending")

    # 2. Update batch
    db.execute(
        text(
            "UPDATE pick_batches SET status = :batch_status, completed_at = NOW() WHERE batch_id = :bid"
        ),
        {"bid": batch_id, "batch_status": BATCH_COMPLETED},
    )

    # 3. Update each SO to PICKING
    # v1.5.0 #119: FOR UPDATE OF so locks each sales_orders row for the
    # rest of this transaction so two concurrent complete_batch calls
    # that share an SO serialise on the SO aggregate. The lock scope
    # is sales_orders only; pick_batch_orders rows are not locked.
    so_rows = db.execute(
        text(
            """
            SELECT pbo.so_id, so.so_number
            FROM pick_batch_orders pbo
            JOIN sales_orders so ON so.so_id = pbo.so_id
            WHERE pbo.batch_id = :bid
            FOR UPDATE OF so
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    for so in so_rows:
        db.execute(
            text("UPDATE sales_orders SET status = :status, picked_at = NOW() WHERE so_id = :so_id"),
            {"so_id": so.so_id, "status": SO_PICKED},
        )

    # 4. Audit log
    write_audit_log(
        db,
        action_type=ACTION_PICK,
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
                   COUNT(*) FILTER (WHERE status = :task_short) AS total_shorts
            FROM pick_tasks WHERE batch_id = :bid
            """
        ),
        {"bid": batch_id, "task_short": TASK_SHORT},
    ).fetchone()

    db.commit()

    return {
        "batch_id": batch_id,
        "batch_number": batch.batch_number,
        "summary": {
            "total_orders": len(so_rows),
            "total_items_picked": task_stats.total_picked,
            "total_shorts": task_stats.total_shorts,
            "orders": [{"so_number": so.so_number, "status": SO_PICKED} for so in so_rows],
        },
    }


# --- Wave Picking ---


def wave_validate(db, so_barcode, warehouse_id):
    """Validate an SO barcode for wave picking. Lightweight check, no allocation."""
    so = db.execute(
        text(
            """
            SELECT so_id, so_number, status, warehouse_id
            FROM sales_orders
            WHERE (so_number = :barcode OR so_barcode = :barcode)
              AND warehouse_id = :wh
            LIMIT 1
            """
        ),
        {"barcode": so_barcode, "wh": warehouse_id},
    ).fetchone()

    if not so:
        # --- FUTURE: ERP Connector Hook ---
        # If a connector is configured, attempt to pull the SO:
        #   result = enrich_order(so_barcode, warehouse_id)
        #   if result: re-query DB and continue
        # For now, return "Order not found" immediately.
        # --- END FUTURE HOOK ---
        return {"valid": False, "error": "Order not found"}

    if so.status not in (SO_OPEN,):
        # Check if already in an active pick batch
        active_batch = db.execute(
            text(
                """
                SELECT pb.batch_id
                FROM pick_batch_orders pbo
                JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
                WHERE pbo.so_id = :so_id AND pb.status IN (:batch_open, :batch_in_progress)
                LIMIT 1
                """
            ),
            {"so_id": so.so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
        ).fetchone()

        if active_batch:
            return {"valid": False, "error": "Order already in active pick batch", "batch_id": active_batch.batch_id}

        return {"valid": False, "error": f"Order status is {so.status}, must be OPEN"}

    # Check if already in an active batch even if OPEN (edge case)
    active_batch = db.execute(
        text(
            """
            SELECT pb.batch_id
            FROM pick_batch_orders pbo
            JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
            WHERE pbo.so_id = :so_id AND pb.status IN (:batch_open, :batch_in_progress)
            LIMIT 1
            """
        ),
        {"so_id": so.so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
    ).fetchone()

    if active_batch:
        return {"valid": False, "error": "Order already in active pick batch", "batch_id": active_batch.batch_id}

    # Get line count and total units
    line_stats = db.execute(
        text(
            """
            SELECT COUNT(*) AS line_count, COALESCE(SUM(quantity_ordered), 0) AS total_units
            FROM sales_order_lines WHERE so_id = :so_id
            """
        ),
        {"so_id": so.so_id},
    ).fetchone()

    if line_stats.line_count == 0:
        return {"valid": False, "error": "Order has no items"}

    return {
        "valid": True,
        "so_id": so.so_id,
        "so_number": so.so_number,
        "line_count": line_stats.line_count,
        "total_units": line_stats.total_units,
    }


def wave_create(db, so_ids, warehouse_id, username):
    """Create a wave pick batch from multiple SOs with combined item picks."""
    if len(so_ids) != len(set(so_ids)):
        raise ValueError("Duplicate SO IDs in request")

    # 1. Validate all SOs
    sales_orders = []
    for so_id in so_ids:
        so = db.execute(
            text(
                "SELECT so_id, so_number, status, warehouse_id FROM sales_orders WHERE so_id = :so_id"
            ),
            {"so_id": so_id},
        ).fetchone()

        if not so:
            raise ValueError(f"SO not found: {so_id}")
        if so.warehouse_id != warehouse_id:
            raise ValueError(f"SO {so.so_number} is in a different warehouse")
        if so.status != SO_OPEN:
            raise ValueError(f"SO {so.so_number} status is {so.status}, must be OPEN")

        # Check not already in active batch
        active = db.execute(
            text(
                """
                SELECT pb.batch_id FROM pick_batch_orders pbo
                JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
                WHERE pbo.so_id = :so_id AND pb.status IN (:batch_open, :batch_in_progress)
                LIMIT 1
                """
            ),
            {"so_id": so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
        ).fetchone()
        if active:
            raise AlreadyInBatchError(so.so_number, active.batch_id)

        # Check has lines
        line_count = db.execute(
            text("SELECT COUNT(*) FROM sales_order_lines WHERE so_id = :so_id"),
            {"so_id": so_id},
        ).scalar()
        if line_count == 0:
            raise ValueError(f"SO {so.so_number} has no items")

        sales_orders.append(so)

    # 2. Generate batch
    now = datetime.now(timezone.utc)
    batch_number = f"WAVE-{now.strftime('%Y%m%d-%H%M%S')}"

    result = db.execute(
        text(
            """
            INSERT INTO pick_batches (batch_number, warehouse_id, assigned_to, status)
            VALUES (:bn, :wh, :user, :status)
            RETURNING batch_id
            """
        ),
        {"bn": batch_number, "wh": warehouse_id, "user": username, "status": BATCH_OPEN},
    )
    batch_id = result.fetchone()[0]

    # 3. Create wave_pick_orders and pick_batch_orders
    for idx, so in enumerate(sales_orders, 1):
        tote = f"TOTE-{idx}"
        db.execute(
            text("INSERT INTO pick_batch_orders (batch_id, so_id, tote_number) VALUES (:bid, :sid, :tote)"),
            {"bid": batch_id, "sid": so.so_id, "tote": tote},
        )
        db.execute(
            text("INSERT INTO wave_pick_orders (batch_id, so_id) VALUES (:bid, :sid)"),
            {"bid": batch_id, "sid": so.so_id},
        )

    # 4. Gather all lines across all SOs, group by item_id
    # line_map: item_id -> [ {so_id, so_line_id, needed} ]
    line_map = {}
    for so in sales_orders:
        lines = db.execute(
            text(
                """
                SELECT so_line_id, item_id, quantity_ordered, quantity_allocated
                FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_ordered > quantity_allocated
                """
            ),
            {"so_id": so.so_id},
        ).fetchall()
        for line in lines:
            needed = line.quantity_ordered - line.quantity_allocated
            if needed <= 0:
                continue
            if line.item_id not in line_map:
                line_map[line.item_id] = []
            line_map[line.item_id].append({
                "so_id": so.so_id,
                "so_line_id": line.so_line_id,
                "needed": needed,
            })

    # 5. For each item, allocate inventory and create combined pick tasks
    total_units = 0
    warnings = []

    for item_id, contributions in line_map.items():
        combined_needed = sum(c["needed"] for c in contributions)

        # V-030: lock every candidate inventory row before reading
        # quantity_allocated so two concurrent wave-creates cannot
        # both allocate the same stock.
        inv_rows = db.execute(
            text(
                """
                SELECT inv.inventory_id, inv.bin_id, inv.quantity_on_hand, inv.quantity_allocated,
                       (inv.quantity_on_hand - inv.quantity_allocated) AS available,
                       b.pick_sequence, b.bin_type
                FROM inventory inv
                JOIN bins b ON b.bin_id = inv.bin_id
                WHERE inv.item_id = :item_id
                  AND inv.warehouse_id = :wh
                  AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                  AND b.bin_type IN (:bin_pickable, :bin_pickable_staging)
                ORDER BY
                  b.pick_sequence ASC,
                  inv.updated_at ASC
                FOR UPDATE OF inv
                """
            ),
            {"item_id": item_id, "wh": warehouse_id, "bin_pickable": BIN_PICKABLE, "bin_pickable_staging": BIN_PICKABLE_STAGING},
        ).fetchall()

        total_available = sum(r.available for r in inv_rows)
        if total_available < combined_needed:
            item_info = db.execute(
                text("SELECT sku FROM items WHERE item_id = :iid"),
                {"iid": item_id},
            ).fetchone()
            warnings.append({
                "sku": item_info.sku,
                "needed": combined_needed,
                "available": total_available,
            })

        # Allocate from bins in order, creating one pick task per bin
        remaining = combined_needed
        for inv in inv_rows:
            if remaining <= 0:
                break

            take = min(remaining, inv.available)

            # Allocate inventory
            db.execute(
                text("UPDATE inventory SET quantity_allocated = quantity_allocated + :qty WHERE inventory_id = :inv_id"),
                {"qty": take, "inv_id": inv.inventory_id},
            )

            # Create combined pick task - use first contributing SO as reference
            first_contrib = contributions[0]
            task_result = db.execute(
                text(
                    """
                    INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, bin_id,
                                            quantity_to_pick, pick_sequence, tote_number, status)
                    VALUES (:bid, :so_id, :so_line_id, :item_id, :bin_id,
                            :qty, :pick_seq, 'WAVE', :task_status)
                    RETURNING pick_task_id
                    """
                ),
                {
                    "bid": batch_id,
                    "so_id": first_contrib["so_id"],
                    "so_line_id": first_contrib["so_line_id"],
                    "item_id": item_id,
                    "bin_id": inv.bin_id,
                    "qty": take,
                    "pick_seq": inv.pick_sequence,
                    "task_status": TASK_PENDING,
                },
            )
            pick_task_id = task_result.fetchone()[0]

            # Create wave_pick_breakdown records - distribute this pick across SOs
            pick_remaining = take
            for contrib in contributions:
                if pick_remaining <= 0:
                    break
                alloc = min(pick_remaining, contrib["needed"])
                if alloc <= 0:
                    continue

                db.execute(
                    text(
                        """
                        INSERT INTO wave_pick_breakdown (pick_task_id, so_id, so_line_id, quantity)
                        VALUES (:tid, :so_id, :sol_id, :qty)
                        """
                    ),
                    {"tid": pick_task_id, "so_id": contrib["so_id"], "sol_id": contrib["so_line_id"], "qty": alloc},
                )

                # Update SO line allocation
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_allocated = quantity_allocated + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": alloc, "sol_id": contrib["so_line_id"]},
                )

                contrib["needed"] -= alloc
                pick_remaining -= alloc

            total_units += take
            remaining -= take

    # 6. Update SO statuses to PICKING
    for so in sales_orders:
        db.execute(
            text("UPDATE sales_orders SET status = :status WHERE so_id = :so_id"),
            {"so_id": so.so_id, "status": SO_PICKING},
        )

    # 7. Update batch totals
    total_picks = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).scalar()

    db.execute(
        text("UPDATE pick_batches SET total_orders = :orders, total_items = :items WHERE batch_id = :bid"),
        {"orders": len(sales_orders), "items": total_units, "bid": batch_id},
    )

    # 8. Audit log
    write_audit_log(
        db,
        action_type=ACTION_PICK,
        entity_type="BATCH",
        entity_id=batch_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "batch_number": batch_number,
            "type": "WAVE_CREATE",
            "so_count": len(sales_orders),
            "total_picks": total_picks,
            "total_units": total_units,
        },
    )

    # 9. Get first pick for response
    first_pick = get_next_task(db, batch_id)

    db.commit()

    response = {
        "batch_id": batch_id,
        "batch_number": batch_number,
        "total_orders": len(sales_orders),
        "total_picks": total_picks,
        "total_units": total_units,
        "orders": [
            {"so_id": so.so_id, "so_number": so.so_number, "line_count": sum(1 for c in line_map.values() for item in c if item["so_id"] == so.so_id)}
            for so in sales_orders
        ],
    }

    if first_pick:
        response["first_pick"] = first_pick

    if warnings:
        response["warnings"] = warnings

    return response


class AlreadyInBatchError(Exception):
    """Raised when an SO is already in an active pick batch."""
    def __init__(self, so_number, batch_id):
        self.so_number = so_number
        self.batch_id = batch_id
        super().__init__(f"SO {so_number} already in active pick batch {batch_id}")


# --- Helpers ---

def _get_contributing_orders(db, pick_task_id):
    """Get contributing orders for a wave pick task."""
    rows = db.execute(
        text(
            """
            SELECT wpb.so_id, so.so_number, wpb.quantity
            FROM wave_pick_breakdown wpb
            JOIN sales_orders so ON so.so_id = wpb.so_id
            WHERE wpb.pick_task_id = :tid
            ORDER BY wpb.so_id
            """
        ),
        {"tid": pick_task_id},
    ).fetchall()
    return [{"so_number": r.so_number, "quantity": r.quantity} for r in rows]


def _get_tasks_for_batch(db, batch_id):
    rows = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.pick_sequence, pt.quantity_to_pick, pt.quantity_picked,
                   pt.tote_number, pt.status,
                   b.bin_code, b.bin_barcode, b.aisle, b.row_num, b.level_num,
                   i.sku, i.item_name, i.upc,
                   so.so_number,
                   z.zone_name
            FROM pick_tasks pt
            JOIN bins b ON b.bin_id = pt.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
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
        "zone": row.zone_name or None,
        "aisle": row.aisle or None,
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
