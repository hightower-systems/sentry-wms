"""
Shipping service: records a ship event against an already-locked sales order.

Extracted from api/routes/shipping.py so the cookie-auth /api/shipping/fulfill
route and the bearer-token /api/v1/dockd/* surface share one transaction body
(fulfillment insert + line writes + SO update + audit + outbox emit).
"""

import uuid
from datetime import timezone

from sqlalchemy import text

from services.audit_service import write_audit_log
from services.events_service import emit_event, get_user_external_id, resolve_source_external_id

from constants import (
    SO_SHIPPED,
    ACTION_SHIP,
    ACTION_SHIP_VOID,
    TASK_PICKED,
    TASK_SHORT,
)


def require_packing_before_shipping(db) -> bool:
    """True when app_settings.require_packing_before_shipping is set to
    something OTHER than the literal string 'false'. The setting defaults
    on (returns True when the row is absent) so a fresh install gates
    shipping behind packing rather than letting a misconfigured deploy
    skip the verify step. Both the cookie-auth /api/shipping/fulfill
    and the dockd /api/v1/dockd/orders/.../ship surfaces consult this
    helper, so the gate is consistent across surfaces."""
    row = db.execute(
        text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
    ).fetchone()
    return not row or row.value != "false"


def record_ship(
    db,
    *,
    so_id,
    so_number,
    so_external_id,
    warehouse_id,
    tracking_number,
    carrier,
    ship_method,
    username,
    source_txn_id,
    pre_ship_status=None,
    shipping_cost=None,
    audit_details_extra=None,
):
    """Record a ship event on an already-locked sales order.

    Caller MUST have:
      - SELECTed the sales_orders row FOR UPDATE
      - Validated warehouse scope
      - Validated status is shippable (PICKED or PACKED, depending on the
        require_packing_before_shipping app setting)

    Caller is responsible for the transaction commit. This function does not
    commit; it does emit one ship.confirmed/1 event onto the outbox.

    v1.9.0 dockd kwargs (all optional, default behaviour matches the
    cookie-auth /api/shipping/fulfill route):
      - pre_ship_status: status the order was in before this ship; stored
        on item_fulfillments.pre_ship_status so a void can revert cleanly.
      - shipping_cost: ShipRush-returned cost, persisted on
        item_fulfillments.shipping_cost AND mirrored into
        audit_log.details.shipping_cost.
      - audit_details_extra: dict merged into the audit_log.details body so
        dockd-specific attribution (station_label, manual_link, weight,
        dims, idempotency_key, operator_username) lands in the chained log.

    Returns dict with fulfillment_id, shipped_at, lines_shipped,
    total_quantity, audit_log_id.
    """
    # 1. Create item_fulfillments record
    result = db.execute(
        text(
            """
            INSERT INTO item_fulfillments (so_id, warehouse_id, tracking_number, carrier, ship_method, shipped_by, status, external_id, pre_ship_status, shipping_cost)
            VALUES (:so_id, :wh, :tracking, :carrier, :ship_method, :shipped_by, :shipped_status, :ext_id, :pre_status, :ship_cost)
            RETURNING fulfillment_id, shipped_at
            """
        ),
        {
            "so_id": so_id,
            "wh": warehouse_id,
            "tracking": tracking_number,
            "carrier": carrier,
            "ship_method": ship_method,
            "shipped_by": username,
            "shipped_status": SO_SHIPPED,
            "ext_id": str(uuid.uuid4()),
            "pre_status": pre_ship_status,
            "ship_cost": shipping_cost,
        },
    )
    fulfillment_row = result.fetchone()
    fulfillment_id = fulfillment_row.fulfillment_id
    shipped_at = fulfillment_row.shipped_at

    # 2. Create fulfillment lines for each SO line with quantity_picked > 0
    so_lines = db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.item_id, sol.quantity_picked
            FROM sales_order_lines sol
            WHERE sol.so_id = :so_id AND sol.quantity_picked > 0
            """
        ),
        {"so_id": so_id},
    ).fetchall()

    lines_shipped = 0
    total_quantity = 0

    for line in so_lines:
        # Find bin_id from pick_tasks
        pick_task = db.execute(
            text(
                """
                SELECT bin_id FROM pick_tasks
                WHERE so_id = :so_id AND item_id = :item_id AND status IN (:task_picked, :task_short)
                ORDER BY pick_task_id ASC
                LIMIT 1
                """
            ),
            {"so_id": so_id, "item_id": line.item_id, "task_picked": TASK_PICKED, "task_short": TASK_SHORT},
        ).fetchone()

        bin_id = pick_task.bin_id if pick_task else 1  # fallback shouldn't happen

        db.execute(
            text(
                """
                INSERT INTO item_fulfillment_lines (fulfillment_id, so_line_id, item_id, quantity_shipped, bin_id)
                VALUES (:fid, :sol_id, :item_id, :qty, :bin_id)
                """
            ),
            {
                "fid": fulfillment_id,
                "sol_id": line.so_line_id,
                "item_id": line.item_id,
                "qty": line.quantity_picked,
                "bin_id": bin_id,
            },
        )

        # 3. Update SO line
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_shipped = quantity_picked, status = :status WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id, "status": SO_SHIPPED},
        )

        lines_shipped += 1
        total_quantity += line.quantity_picked

    # 4. Update SO status with carrier and tracking
    db.execute(
        text(
            """
            UPDATE sales_orders
            SET status = :shipped_status, shipped_at = NOW(), carrier = :carrier, tracking_number = :tracking
            WHERE so_id = :so_id
            """
        ),
        {"so_id": so_id, "carrier": carrier, "tracking": tracking_number, "shipped_status": SO_SHIPPED},
    )

    # 5. Audit log
    audit_details = {
        "so_number": so_number,
        "tracking_number": tracking_number,
        "carrier": carrier,
        "fulfillment_id": fulfillment_id,
    }
    if shipping_cost is not None:
        audit_details["shipping_cost"] = float(shipping_cost)
    if audit_details_extra:
        audit_details.update(audit_details_extra)
    audit_log_id = write_audit_log(
        db,
        action_type=ACTION_SHIP,
        entity_type="SO",
        entity_id=so_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details=audit_details,
    )

    # 6. v1.5.0 #118: emit ship.confirmed on the integration_events
    # outbox. tracking_numbers[] is array-shaped (Sentry creates one
    # fulfillment per SO today, so exactly one entry). Sentry's internal
    # column is ship_method; the wire contract renames it to
    # service_level per plan 1.7.1. packages[] mirrors the single
    # synthesised package from pack.confirmed.
    # AvidMax customization (preserves 780c7c0 across the v1.9 record_ship
    # refactor): outbound payload carries SOURCE-SYSTEM external_ids (the
    # identifiers the upstream connector originally pushed to Pipe B) so
    # Fabric receivers correlate ship.confirmed back to BC/Amazon/eBay
    # marketplaces without re-querying cross_system_mappings. Falls back to
    # canonical UUID when no mapping row exists (e.g., item created directly
    # via the admin UI with no Pipe-B push).
    pack_lines = db.execute(
        text(
            """
            SELECT
                COALESCE(csm.source_id, CAST(i.external_id AS TEXT)) AS item_external_id,
                sol.quantity_packed
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
              LEFT JOIN cross_system_mappings csm
                ON csm.canonical_id = i.external_id
               AND csm.source_type = 'item'
             WHERE sol.so_id = :sid
             ORDER BY sol.line_number
            """
        ),
        {"sid": so_id},
    ).fetchall()
    stats = db.execute(
        text(
            """
            SELECT COALESCE(SUM(i.weight_lbs * sol.quantity_picked), 0) AS total_weight
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :sid
            """
        ),
        {"sid": so_id},
    ).fetchone()
    # Resolve the SO's source-system identifier for the outbound payload.
    # Falls back to the canonical UUID when no cross_system_mappings row
    # exists (SO created directly in Sentry without a Pipe-B push).
    # package_external_id stays Sentry-canonical — packages are
    # Sentry-synthesized (one fulfillment per SO), no upstream identity to
    # map. The canonical UUID is the stable stem for package addressing.
    so_canonical_str = str(so_external_id)
    so_source_external_id = (
        resolve_source_external_id(db, "sales_order", so_external_id)
        or so_canonical_str
    )
    emit_event(
        db,
        event_type="ship.confirmed",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=so_id,
        aggregate_external_id=so_external_id,
        warehouse_id=warehouse_id,
        source_txn_id=source_txn_id,
        payload={
            "sales_order_external_id": so_source_external_id,
            "tracking_numbers": [tracking_number],
            "carrier": carrier,
            "service_level": ship_method,
            "packages": [
                {
                    "package_external_id": f"{so_canonical_str}-pkg-1",
                    "weight_lb": float(stats.total_weight) if stats.total_weight is not None else None,
                    "dimensions_in": None,
                    "lines": [
                        {
                            "item_external_id": str(line.item_external_id),
                            "quantity_packed": line.quantity_packed,
                        }
                        for line in pack_lines
                    ],
                },
            ],
            "completed_by_user_external_id": get_user_external_id(db, username),
            "completed_at": shipped_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    return {
        "fulfillment_id": fulfillment_id,
        "shipped_at": shipped_at,
        "lines_shipped": lines_shipped,
        "total_quantity": total_quantity,
        "audit_log_id": audit_log_id,
    }


def record_void_ship(
    db,
    *,
    so_id,
    so_number,
    so_external_id,
    warehouse_id,
    fulfillment_id,
    pre_ship_status,
    operator_username,
    operator_external_id,
    reason,
    source_txn_id,
    audit_details_extra=None,
):
    """Reverse a previously-successful ship on an already-locked sales order.

    Caller MUST have:
      - SELECTed the sales_orders row FOR UPDATE
      - Validated SO.status == SHIPPED
      - Resolved operator_external_id (the ship.voided/1 schema requires
        a UUID); 422 unknown_operator is the caller's job.
      - Picked the SHIPPED item_fulfillments row whose pre_ship_status
        is the revert target.

    Caller is responsible for the transaction commit. This function does
    not commit; it does emit one ship.voided/1 event onto the outbox.

    Returns dict with voided_at, audit_log_id, reverted_to_status.
    """
    # 1. Revert the SO to its pre-ship status; clear the per-ship fields
    # the cookie-auth or dockd record_ship populated.
    db.execute(
        text(
            """
            UPDATE sales_orders
               SET status          = :pre,
                   tracking_number = NULL,
                   carrier         = NULL,
                   shipped_at      = NULL
             WHERE so_id = :so_id
            """
        ),
        {"pre": pre_ship_status, "so_id": so_id},
    )

    # 2. Mark the fulfillment VOIDED with operator + reason + timestamp.
    voided_row = db.execute(
        text(
            """
            UPDATE item_fulfillments
               SET status      = 'VOIDED',
                   voided_at   = NOW(),
                   voided_by   = :user,
                   void_reason = :reason
             WHERE fulfillment_id = :fid
             RETURNING voided_at
            """
        ),
        {"user": operator_username, "reason": reason, "fid": fulfillment_id},
    ).fetchone()
    voided_at = voided_row.voided_at

    # 3. Roll back per-line state so sales_order_lines stays consistent
    # with sales_orders.status. record_ship sets quantity_shipped =
    # quantity_picked and status = 'SHIPPED' on every picked line; void
    # reverses that so a re-ship through record_ship is idempotent.
    db.execute(
        text(
            """
            UPDATE sales_order_lines
               SET quantity_shipped = 0,
                   status           = :pre
             WHERE so_id = :so_id
            """
        ),
        {"pre": pre_ship_status, "so_id": so_id},
    )

    # 4. Audit log. Captures the original tracking/carrier indirectly via
    # the fulfillment_id pointer; the operator-supplied reason and the
    # revert target make the audit row self-describing.
    audit_details = {
        "so_number": so_number,
        "fulfillment_id": fulfillment_id,
        "reason": reason,
        "reverted_to_status": pre_ship_status,
    }
    if audit_details_extra:
        audit_details.update(audit_details_extra)
    audit_log_id = write_audit_log(
        db,
        action_type=ACTION_SHIP_VOID,
        entity_type="SO",
        entity_id=so_id,
        user_id=operator_username,
        warehouse_id=warehouse_id,
        details=audit_details,
    )

    # 5. Emit ship.voided/1. source_txn_id = idempotency_key ties outbox-
    # level dedup (mig 020 UNIQUE on aggregate_type, aggregate_id,
    # event_type, source_txn_id) to HTTP-level dedup so a successful
    # retry cannot double-emit. The schema requires
    # voided_by_user_external_id as UUID4 -- the caller has already
    # resolved it (route returns 422 unknown_operator otherwise).
    # AvidMax customization: ship.voided/1 also carries source-system SO id
    # for Fabric correlation. Same fallback to canonical UUID as ship.confirmed.
    so_source_external_id = (
        resolve_source_external_id(db, "sales_order", so_external_id)
        or str(so_external_id)
    )
    emit_event(
        db,
        event_type="ship.voided",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=so_id,
        aggregate_external_id=so_external_id,
        warehouse_id=warehouse_id,
        source_txn_id=source_txn_id,
        payload={
            "sales_order_external_id": so_source_external_id,
            "voided_at": voided_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "voided_by_user_external_id": str(operator_external_id),
            "reason": reason,
            "reverted_to_status": pre_ship_status,
        },
    )

    return {
        "voided_at": voided_at,
        "audit_log_id": audit_log_id,
        "reverted_to_status": pre_ship_status,
    }
