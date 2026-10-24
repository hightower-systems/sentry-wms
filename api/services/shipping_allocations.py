"""Validate shipment identities against native pick and receipt facts."""

import re
from collections import defaultdict

from sqlalchemy import text
from services.serial_history import latest_native_serial_position, lock_native_serials


def normalise_serials(value):
    serials = [part.strip() for part in re.split(r"[,\r\n]+", value or "") if part.strip()]
    if len(serials) != len(set(serials)):
        raise ValueError("Serial numbers must be unique")
    return serials


def _validate_serial_history(db, item_id, serials, warehouse_id):
    lock_native_serials(db, serials)
    for serial in sorted(serials):
        position = latest_native_serial_position(db, item_id, serial)
        if position is not None and position["warehouse_id"] != warehouse_id:
            raise ValueError("A selected serial number has exited stock or belongs to another warehouse")


def prepare_shipping_allocations(db, so_id, warehouse_id, lines, supplied):
    picks = db.execute(text("""
        SELECT so_line_id, bin_id, SUM(quantity) AS quantity FROM (
            SELECT task.so_line_id, task.bin_id, task.quantity_picked AS quantity
            FROM pick_tasks task
            WHERE task.so_id = :so_id AND task.so_line_id IS NOT NULL
              AND task.status IN ('PICKED', 'SHORT')
              AND NOT EXISTS (SELECT 1 FROM wave_pick_breakdown wave WHERE wave.pick_task_id = task.pick_task_id)
            UNION ALL
            SELECT wave.so_line_id, task.bin_id, wave.quantity_picked AS quantity
            FROM wave_pick_breakdown wave
            JOIN pick_tasks task ON task.pick_task_id = wave.pick_task_id
            WHERE wave.so_id = :so_id AND wave.so_line_id IS NOT NULL
              AND task.status IN ('PICKED', 'SHORT')
        ) picked GROUP BY so_line_id, bin_id
    """), {"so_id": so_id}).fetchall()
    picked = {(row.so_line_id, row.bin_id): int(row.quantity) for row in picks if row.quantity > 0}
    by_id = {line.so_line_id: line for line in lines}
    if supplied is None:
        if any(line.is_lot_tracked or line.is_serial_tracked for line in lines):
            raise ValueError("Provide the actual lot and serial allocations before shipping tracked items")
        allocations = [{"so_line_id": line_id, "bin_id": bin_id, "quantity": quantity,
                        "lot_number": None, "serial_number": None}
                       for (line_id, bin_id), quantity in picked.items()]
    else:
        allocations = [entry.model_dump() if hasattr(entry, "model_dump") else dict(entry) for entry in supplied]
    if not allocations or len(allocations) > 500:
        raise ValueError("Choose a bounded set of actual picked shipment allocations")
    bins = db.execute(text("SELECT bin_id FROM bins WHERE warehouse_id = :warehouse_id AND is_active AND bin_id = ANY(:bin_ids)"),
                      {"warehouse_id": warehouse_id, "bin_ids": list({entry["bin_id"] for entry in allocations})}).fetchall()
    allowed_bins = {row.bin_id for row in bins}
    totals, bin_totals, serials_by_item = defaultdict(int), defaultdict(int), defaultdict(list)
    for allocation in allocations:
        line = by_id.get(allocation["so_line_id"])
        if line is None or allocation["bin_id"] not in allowed_bins:
            raise ValueError("The shipment allocation is outside this order or warehouse")
        quantity = allocation["quantity"]
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Shipment quantities must be positive stock units")
        lot = (allocation.get("lot_number") or "").strip() or None
        serials = normalise_serials(allocation.get("serial_number"))
        if bool(line.is_lot_tracked) != bool(lot):
            raise ValueError("Provide exactly the required batch identity for this item")
        if (line.is_serial_tracked and len(serials) != quantity) or (not line.is_serial_tracked and serials):
            raise ValueError("Provide one actual serial number per serialized stock unit")
        allocation.update(item_id=line.item_id, lot_number=lot, serial_number="\n".join(serials) or None)
        totals[line.so_line_id] += quantity
        bin_totals[(line.so_line_id, allocation["bin_id"])] += quantity
        serials_by_item[line.item_id].extend(serials)
    if any(totals[line.so_line_id] != line.quantity_picked for line in lines) or bin_totals != picked:
        raise ValueError("Shipment allocations must match every actual picked line and source bin")
    for item_id, serials in sorted(serials_by_item.items()):
        if len(serials) != len(set(serials)):
            raise ValueError("A serial number cannot appear twice in this shipment")
        _validate_serial_history(db, item_id, serials, warehouse_id)
    return allocations


def validate_return_tracking(db, parent_id, original_line_id, item_id, quantity, lot, serials):
    if not parent_id or not original_line_id:
        raise ValueError("A tracked return needs its exact original shipment line")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
               {"identity": f"return:{parent_id}:{original_line_id}:{lot or ''}"})
    shipments = db.execute(text("""
        SELECT line.quantity_shipped, line.lot_number, line.serial_number, shipment.shipped_at
        FROM item_fulfillment_lines line
        JOIN item_fulfillments shipment ON shipment.fulfillment_id = line.fulfillment_id
        WHERE shipment.so_id = :parent_id AND line.so_line_id = :line_id
          AND line.item_id = :item_id AND shipment.status = 'SHIPPED' AND shipment.voided_at IS NULL
          AND line.lot_number IS NOT DISTINCT FROM :lot
        ORDER BY shipment.shipped_at DESC
    """), {"parent_id": parent_id, "line_id": original_line_id, "item_id": item_id, "lot": lot}).fetchall()
    received = db.execute(text("""
        SELECT COALESCE(SUM(receipt.quantity_received), 0)
        FROM item_receipts receipt
        JOIN sales_orders return_order ON return_order.so_id = receipt.so_id
        JOIN sales_order_lines return_line ON return_line.so_line_id = receipt.so_line_id
        WHERE return_order.parent_so_id = :parent_id
          AND return_line.original_so_line_id = :line_id
          AND receipt.item_id = :item_id
          AND receipt.lot_number IS NOT DISTINCT FROM :lot
    """), {"parent_id": parent_id, "line_id": original_line_id, "item_id": item_id, "lot": lot}).scalar()
    if not shipments or sum(row.quantity_shipped for row in shipments) < quantity + int(received):
        raise ValueError("The batch return exceeds its verified original shipment")
    shipped_serials = {serial for row in shipments for serial in normalise_serials(row.serial_number)}
    if not set(serials).issubset(shipped_serials):
        raise ValueError("A selected serial number was not shipped on the original order line")
    lock_native_serials(db, serials)
    for serial in sorted(serials):
        shipped_at = max(row.shipped_at for row in shipments if serial in normalise_serials(row.serial_number))
        position = latest_native_serial_position(db, item_id, serial)
        if position is not None and position["warehouse_id"] is not None:
            raise ValueError("A selected return serial is already present in stock")
        returned = db.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM item_receipts receipt
                JOIN sales_orders return_order ON return_order.so_id = receipt.so_id
                WHERE return_order.parent_so_id = :parent_id AND receipt.item_id = :item_id
                  AND receipt.received_at >= :shipped_at
                  AND :serial = ANY(regexp_split_to_array(COALESCE(receipt.serial_number, ''), E'[,\\r\\n]+'))
            )
        """), {"parent_id": parent_id, "item_id": item_id, "serial": serial, "shipped_at": shipped_at}).scalar()
        if returned:
            raise ValueError("A selected serial number was already received against this shipment")
