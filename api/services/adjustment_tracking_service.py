"""Exact count allocation validation; proof stays in the existing event outbox."""

from collections import defaultdict

from sqlalchemy import text

from services.inventory_service import add_inventory
from services.serial_history import latest_native_serial_position, lock_native_serials


def canonical_allocations(allocations):
    return sorted(({
        "bin_id": entry["bin_id"], "quantity": entry["quantity"],
        "lot_number": entry.get("lot_number") or None,
        "serial_number": entry.get("serial_number") or None,
    } for entry in allocations), key=lambda entry: (
        entry["bin_id"], entry["lot_number"] or "", entry["serial_number"] or "", entry["quantity"],
    ))


def prepare_adjustment_allocations(db, adjustment, supplied):
    item = db.execute(text("""
        SELECT is_lot_tracked, is_serial_tracked FROM items WHERE item_id = :iid FOR SHARE
    """), {"iid": adjustment.item_id}).fetchone()
    if not item or not adjustment.quantity_change:
        raise ValueError("The adjustment item or non-zero variance is unavailable")
    if supplied is None:
        if item.is_lot_tracked or item.is_serial_tracked:
            raise ValueError("Tracked variances require the actual lot and serial allocations")
        supplied = [{"bin_id": adjustment.bin_id, "quantity": abs(adjustment.quantity_change)}]
    allocations = canonical_allocations(supplied)
    if not allocations or len(allocations) > 500:
        raise ValueError("Choose a bounded set of actual count allocations")
    bin_row = db.execute(text("""
        SELECT bin_id FROM bins WHERE bin_id = :bid AND warehouse_id = :wid AND is_active FOR SHARE
    """), {"bid": adjustment.bin_id, "wid": adjustment.warehouse_id}).fetchone()
    if not bin_row or any(entry["bin_id"] != adjustment.bin_id for entry in allocations):
        raise ValueError("A variance allocation must use its original active count bin")
    serials = []
    for entry in allocations:
        quantity = entry["quantity"]
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Variance allocations use positive whole stock units")
        if bool(item.is_lot_tracked) != bool(entry["lot_number"]):
            raise ValueError("Provide exactly the required lot identity")
        if bool(item.is_serial_tracked) != bool(entry["serial_number"]) or (
            item.is_serial_tracked and quantity != 1
        ):
            raise ValueError("Provide one unique serial per stock unit")
        if entry["serial_number"]:
            serials.append(entry["serial_number"])
    if len(set(serials)) != len(serials):
        raise ValueError("A serial cannot appear twice in a variance")
    if sum(entry["quantity"] for entry in allocations) != abs(adjustment.quantity_change):
        raise ValueError("Allocations must equal the exact recorded variance")
    lock_native_serials(db, serials)
    for serial in sorted(serials):
        position = latest_native_serial_position(db, adjustment.item_id, serial)
        if position is not None and (
            (adjustment.quantity_change > 0 and position["warehouse_id"] is not None) or
            (adjustment.quantity_change < 0 and position["warehouse_id"] != adjustment.warehouse_id)
        ):
            raise ValueError("The serial's known native stock position conflicts with this variance")
    return allocations


def apply_adjustment_allocations(db, adjustment, allocations):
    # All affected rows are checked under locks before any stock update.
    db.execute(text("SELECT pg_advisory_xact_lock(:iid, :bid)"),
               {"iid": adjustment.item_id, "bid": adjustment.bin_id})
    totals = defaultdict(int)
    for entry in allocations:
        totals[entry["lot_number"]] += entry["quantity"]
    pending = []
    for lot in sorted(totals, key=lambda value: value or ""):
        rows = db.execute(text("""
            SELECT inventory_id, quantity_on_hand, quantity_allocated, warehouse_id
            FROM inventory WHERE item_id = :iid AND bin_id = :bid
              AND lot_number IS NOT DISTINCT FROM :lot
            ORDER BY inventory_id FOR UPDATE
        """), {"iid": adjustment.item_id, "bid": adjustment.bin_id, "lot": lot}).fetchall()
        if len(rows) > 1 or (rows and rows[0].warehouse_id != adjustment.warehouse_id):
            raise ValueError("The exact lot inventory row is ambiguous or outside this warehouse")
        existing = rows[0] if rows else None
        change = totals[lot] if adjustment.quantity_change > 0 else -totals[lot]
        if change < 0 and (not existing or existing.quantity_on_hand + change < max(0, existing.quantity_allocated)):
            raise ValueError("The variance exceeds the exact lot's unallocated stock")
        pending.append((lot, existing, change))
    for lot, existing, change in pending:
        if existing:
            db.execute(text("""
                UPDATE inventory SET quantity_on_hand = quantity_on_hand + :change, updated_at = NOW()
                WHERE inventory_id = :iid
            """), {"change": change, "iid": existing.inventory_id})
        else:
            add_inventory(db, adjustment.item_id, adjustment.bin_id, adjustment.warehouse_id, change, lot)


def adjustment_review_proof(db, adjustment):
    rows = db.execute(text("""
        SELECT payload, source_txn_id FROM integration_events
        WHERE aggregate_type = 'inventory_adjustment' AND aggregate_id = :aid
          AND aggregate_external_id = :external_id AND warehouse_id = :wid
          AND event_type = 'inventoryadjusted.completed' AND event_version = 1
        ORDER BY event_id LIMIT 2
    """), {"aid": adjustment.adjustment_id, "external_id": adjustment.external_id,
           "wid": adjustment.warehouse_id}).fetchall()
    if len(rows) != 1:
        raise ValueError("The approved variance has no unique immutable review proof")
    payload = rows[0].payload
    if payload.get("adjustment_external_id") != str(adjustment.external_id) or (
        payload.get("quantity_delta") != adjustment.quantity_change or not isinstance(payload.get("allocations"), list)
    ):
        raise ValueError("The approved variance allocation proof is unavailable or changed")
    return canonical_allocations(payload["allocations"]), str(rows[0].source_txn_id)
