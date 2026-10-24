"""Transfer allocation proof and exact lot-preserving physical movement.

Uses existing picks, inventory, approval JSON and immutable stock events. There
is no additional inventory ledger or process-local idempotency state.
"""

from collections import defaultdict
from sqlalchemy import text
from services.inventory_service import add_inventory
from services.serial_history import lock_native_serials, latest_native_serial_position


def transfer_pick_sources(db, to_id, source_warehouse_id):
    rows = db.execute(text("""
        SELECT task.to_line_id, task.item_id, task.bin_id AS source_bin_id,
               bin.bin_code, SUM(task.quantity_picked) AS quantity
        FROM pick_tasks task JOIN pick_batches batch ON batch.batch_id = task.batch_id
        JOIN bins bin ON bin.bin_id = task.bin_id
        JOIN transfer_order_lines line ON line.to_line_id = task.to_line_id
          AND line.to_id = task.to_id AND line.item_id = task.item_id
        WHERE task.to_id = :to_id AND task.status = 'PICKED'
          AND batch.status = 'COMPLETED' AND batch.warehouse_id = :warehouse_id
          AND bin.warehouse_id = :warehouse_id
        GROUP BY task.to_line_id, task.item_id, task.bin_id, bin.bin_code
        ORDER BY task.to_line_id, task.bin_id
    """), {"to_id": to_id, "warehouse_id": source_warehouse_id}).fetchall()
    used = db.execute(text("""
        SELECT entry.value FROM transfer_order_approvals approval
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(approval.lines_snapshot->'allocations', '[]'::jsonb)) entry(value)
        WHERE approval.to_id = :to_id AND approval.status = 'APPROVED'
    """), {"to_id": to_id}).fetchall()
    consumed = defaultdict(int)
    for row in used:
        allocation = row.value
        consumed[(allocation["to_line_id"], allocation["item_id"], allocation["source_bin_id"])] += allocation["quantity"]
    return [{"to_line_id": row.to_line_id, "item_id": row.item_id,
             "source_bin_id": row.source_bin_id, "bin_code": row.bin_code,
             "quantity": int(row.quantity) - consumed[(row.to_line_id, row.item_id, row.source_bin_id)]}
            for row in rows if int(row.quantity) > consumed[(row.to_line_id, row.item_id, row.source_bin_id)]]


def prepare_transfer_allocations(db, header, snapshot_lines, supplied):
    parents = db.execute(text("""
        SELECT line.to_line_id, line.item_id, line.picked_qty, line.approved_qty,
               item.is_lot_tracked, item.is_serial_tracked
        FROM transfer_order_lines line JOIN items item ON item.item_id = line.item_id
        WHERE line.to_id = :to_id ORDER BY line.to_line_id FOR UPDATE OF line
    """), {"to_id": header.to_id}).fetchall()
    by_id = {row.to_line_id: row for row in parents}
    expected = {}
    for line in snapshot_lines:
        parent = by_id.get(line["to_line_id"])
        qty = line["picked_in_snapshot"]
        if (parent is None or parent.item_id != line["item_id"] or type(qty) is not int
                or qty <= 0 or qty > parent.picked_qty - parent.approved_qty or parent.to_line_id in expected):
            raise ValueError("The approval snapshot differs from its exact remaining transfer lines")
        expected[parent.to_line_id] = qty
    sources = transfer_pick_sources(db, header.to_id, header.source_warehouse_id)
    capacity = {(row["to_line_id"], row["item_id"], row["source_bin_id"]): row["quantity"] for row in sources}
    if supplied is None:
        if any(by_id[line_id].is_lot_tracked or by_id[line_id].is_serial_tracked for line_id in expected):
            raise ValueError("Provide the actual source, destination, lot and serial allocations")
        destination = db.execute(text("SELECT bin_id FROM bins WHERE warehouse_id = :warehouse_id AND is_active AND bin_type = 'Staging' ORDER BY bin_id LIMIT 1"), {"warehouse_id": header.destination_warehouse_id}).fetchone()
        if destination is None:
            raise ValueError("Choose an active destination bin")
        supplied = []
        for line_id, qty in expected.items():
            remaining = qty
            for source in sources:
                if source["to_line_id"] != line_id or remaining <= 0:
                    continue
                take = min(source["quantity"], remaining)
                supplied.append({"to_line_id": line_id, "source_bin_id": source["source_bin_id"], "destination_bin_id": destination.bin_id, "quantity": take})
                remaining -= take
    allocations = []
    totals, bins_total, stock_total = defaultdict(int), defaultdict(int), defaultdict(int)
    serials = []
    for raw in supplied:
        entry = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
        parent = by_id.get(entry["to_line_id"])
        if parent is None or parent.to_line_id not in expected:
            raise ValueError("The allocation belongs to a different transfer line")
        qty, lot, serial = entry["quantity"], (entry.get("lot_number") or "").strip() or None, (entry.get("serial_number") or "").strip() or None
        if (type(qty) is not int or qty <= 0 or bool(parent.is_lot_tracked) != bool(lot)
                or bool(parent.is_serial_tracked) != bool(serial) or (serial and (qty != 1 or len(serial) > 100 or any(char == "," or ord(char) < 32 for char in serial)))):
            raise ValueError("Each allocation needs its exact tracking identities and positive stock units; one unit per serial")
        entry.update(item_id=parent.item_id, lot_number=lot, serial_number=serial)
        totals[parent.to_line_id] += qty
        bins_total[(parent.to_line_id, parent.item_id, entry["source_bin_id"])] += qty
        stock_total[(parent.item_id, entry["source_bin_id"], lot)] += qty
        if serial:
            serials.append(serial)
        allocations.append(entry)
    if not allocations or len(allocations) > 500 or dict(totals) != expected or len(serials) != len(set(serials)):
        raise ValueError("Allocations must match the exact snapshot quantities and contain unique serials")
    if any(qty > capacity.get(key, 0) for key, qty in bins_total.items()):
        raise ValueError("The source allocations exceed actual unapproved picked-bin quantities")
    all_bins = sorted({entry[key] for entry in allocations for key in ("source_bin_id", "destination_bin_id")})
    bins = db.execute(text("SELECT bin_id, warehouse_id FROM bins WHERE bin_id = ANY(:ids) AND is_active ORDER BY bin_id FOR SHARE"), {"ids": all_bins}).fetchall()
    bin_warehouse = {row.bin_id: row.warehouse_id for row in bins}
    for entry in allocations:
        if (entry["source_bin_id"] == entry["destination_bin_id"] or bin_warehouse.get(entry["source_bin_id"]) != header.source_warehouse_id
                or bin_warehouse.get(entry["destination_bin_id"]) != header.destination_warehouse_id):
            raise ValueError("A source or destination bin is inactive or belongs to a different warehouse")
    lock_native_serials(db, serials)
    for entry in allocations:
        if entry["serial_number"]:
            position = latest_native_serial_position(db, entry["item_id"], entry["serial_number"])
            if position is not None and position["warehouse_id"] != header.source_warehouse_id:
                raise ValueError("The latest native serial fact does not place it in the source warehouse")
    for item_id, bin_id in sorted({(entry["item_id"], entry[key]) for entry in allocations for key in ("source_bin_id", "destination_bin_id")}):
        db.execute(text("SELECT pg_advisory_xact_lock(:item_id, :bin_id)"), {"item_id": item_id, "bin_id": bin_id})
    stock_ids = {}
    for (item_id, bin_id, lot), qty in sorted(stock_total.items(), key=lambda pair: (pair[0][0], pair[0][1], pair[0][2] or "")):
        rows = db.execute(text("SELECT inventory_id, quantity_on_hand, quantity_allocated FROM inventory WHERE item_id = :item_id AND bin_id = :bin_id AND warehouse_id = :warehouse_id AND lot_number IS NOT DISTINCT FROM :lot ORDER BY inventory_id FOR UPDATE"), {"item_id": item_id, "bin_id": bin_id, "warehouse_id": header.source_warehouse_id, "lot": lot}).fetchall()
        if len(rows) != 1 or rows[0].quantity_on_hand < qty or rows[0].quantity_allocated < qty:
            raise ValueError("The exact source lot lacks sufficient reserved physical stock")
        stock_ids[(item_id, bin_id, lot)] = rows[0].inventory_id
    return allocations, stock_total, stock_ids


def apply_transfer_allocations(db, header, allocations, stock_total, stock_ids):
    for key, qty in sorted(stock_total.items(), key=lambda pair: (pair[0][0], pair[0][1], pair[0][2] or "")):
        db.execute(text("UPDATE inventory SET quantity_on_hand = quantity_on_hand - :qty, quantity_allocated = quantity_allocated - :qty, updated_at = NOW() WHERE inventory_id = :inventory_id"), {"qty": qty, "inventory_id": stock_ids[key]})
    for entry in sorted(allocations, key=lambda row: (row["item_id"], row["destination_bin_id"], row["lot_number"] or "", row["serial_number"] or "")):
        add_inventory(db, entry["item_id"], entry["destination_bin_id"], header.destination_warehouse_id, entry["quantity"], entry["lot_number"])
