"""Read serial movement facts from existing physical operation records."""

from sqlalchemy import text


def lock_native_serials(db, serials):
    for serial in sorted(set(serials)):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:serial, 0))"),
            {"serial": serial},
        )


def latest_native_serial_position(db, item_id, serial):
    """Return the last known warehouse, or NULL for a physical stock exit.

    A missing record is unknown history. ERP tracking validation remains
    authoritative; this helper neither fabricates nor backfills serial stock.
    The caller must hold the serial transaction lock before using this result
    to authorize a physical mutation.
    """
    return db.execute(text("""
        SELECT occurred_at, warehouse_id FROM (
            SELECT COALESCE(CASE WHEN receipt_event.event_id IS NOT NULL THEN
                       COALESCE(receipt_event.visible_at, 'infinity'::timestamptz) END,
                       receipt.received_at) AS occurred_at,
                   receipt.warehouse_id, 1 AS priority, receipt.receipt_id AS sequence_id
            FROM item_receipts receipt
            LEFT JOIN LATERAL (
                SELECT event_id, visible_at FROM integration_events
                WHERE aggregate_type = 'item_receipt' AND aggregate_external_id = receipt.external_id
                  AND event_type IN ('receipt.completed', 'return.received')
                ORDER BY event_id DESC LIMIT 1
            ) receipt_event ON TRUE
            WHERE receipt.item_id = :item_id
              AND :serial = ANY(regexp_split_to_array(COALESCE(receipt.serial_number, ''), E'[,\\r\\n]+'))
            UNION ALL
            SELECT COALESCE(CASE WHEN shipment_event.event_id IS NOT NULL THEN
                       COALESCE(shipment_event.visible_at, 'infinity'::timestamptz) END,
                       shipment.shipped_at), NULL::integer, 2, shipment.fulfillment_id
            FROM item_fulfillment_lines line
            JOIN item_fulfillments shipment ON shipment.fulfillment_id = line.fulfillment_id
            LEFT JOIN LATERAL (
                SELECT event_id, visible_at FROM integration_events
                WHERE aggregate_type = 'sales_order' AND event_type = 'ship.confirmed'
                  AND payload->>'fulfillment_external_id' = shipment.external_id::text
                ORDER BY event_id DESC LIMIT 1
            ) shipment_event ON TRUE
            WHERE line.item_id = :item_id AND shipment.status = 'SHIPPED'
              AND shipment.voided_at IS NULL
              AND :serial = ANY(regexp_split_to_array(COALESCE(line.serial_number, ''), E'[,\\r\\n]+'))
            UNION ALL
            SELECT COALESCE(event.visible_at, 'infinity'::timestamptz),
                   CASE WHEN adjustment.quantity_change > 0 THEN adjustment.warehouse_id ELSE NULL END, 3, event.event_id
            FROM inventory_adjustments adjustment
            JOIN integration_events event ON event.aggregate_id = adjustment.adjustment_id
              AND event.aggregate_external_id = adjustment.external_id
              AND event.aggregate_type = 'inventory_adjustment'
              AND event.event_type = 'inventoryadjusted.completed'
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(event.payload->'allocations', '[]'::jsonb)) allocation
            WHERE adjustment.item_id = :item_id AND adjustment.status = 'APPROVED'
              AND :serial = ANY(regexp_split_to_array(COALESCE(allocation->>'serial_number', ''), E'[,\\r\\n]+'))
            UNION ALL
            SELECT COALESCE(CASE WHEN transfer_event.event_id IS NOT NULL THEN
                       COALESCE(transfer_event.visible_at, 'infinity'::timestamptz) END,
                       approval.approved_at), transfer.destination_warehouse_id, 4, approval.to_approval_id
            FROM transfer_order_approvals approval
            JOIN transfer_orders transfer ON transfer.to_id = approval.to_id
            LEFT JOIN LATERAL (
                SELECT event_id, visible_at FROM integration_events
                WHERE aggregate_type = 'inventory_transfer' AND event_type = 'inventorytransfer.completed'
                  AND aggregate_external_id = approval.external_id
                ORDER BY event_id DESC LIMIT 1
            ) transfer_event ON TRUE
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(approval.lines_snapshot->'allocations', '[]'::jsonb)) allocation
            WHERE approval.status = 'APPROVED'
              AND (allocation->>'item_id')::bigint = :item_id
              AND :serial = ANY(regexp_split_to_array(COALESCE(allocation->>'serial_number', ''), E'[,\\r\\n]+'))
        ) movements
        ORDER BY occurred_at DESC, priority DESC, sequence_id DESC
        LIMIT 1
    """), {"item_id": item_id, "serial": serial}).mappings().first()
