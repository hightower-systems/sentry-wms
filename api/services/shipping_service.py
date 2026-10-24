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
from services.shipping_allocations import prepare_shipping_allocations

from constants import (
    SO_PICKED,
    SO_PACKED,
    SO_SHIPPED,
    ACTION_SHIP,
    ACTION_SHIP_VOID,
    TASK_PICKED,
    TASK_SHORT,
    order_type_allows_fulfillment_ops,
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


# Canonical carrier vocabulary, shared with the mobile ship screens' carrier
# picker (mobile/src/screens/ShipScreen.js CARRIERS). Each entry maps a
# lower-cased token to its canonical label; "usps" is listed before "ups" as
# defensive ordering even though the two share no substring.
_CARRIER_TOKENS = (
    ("usps", "USPS"),
    ("ups", "UPS"),
    ("fedex", "FedEx"),
    ("fed ex", "FedEx"),
    ("dhl", "DHL"),
    ("amazon", "Amazon"),
)


def carrier_from_ship_method(ship_method):
    """Best-effort carrier label from a free-text ship method.

    The admin SO-edit manual-ship surface has no carrier field -- Ship Method
    already conveys the operator's shipping choice -- yet ship.confirmed/1
    requires a non-null carrier string. Resolve one from the ship method using
    the same carrier vocabulary the mobile ship screens offer, defaulting to
    "Other" when the method names no known carrier or is blank. Marketplace
    ship methods are messy free text: "UPS (UPS Ground)", "USPSParcel",
    "FedEx (2nd Day)" all resolve; "Standard", "FreeEconomy", "Expedited",
    "SecondDay" fall through to "Other".
    """
    haystack = (ship_method or "").lower()
    for token, label in _CARRIER_TOKENS:
        if token in haystack:
            return label
    return "Other"


def _detect_silent_shortfall(db, so_id):
    """Return the SO's silently-under-picked lines: quantity_picked <
    quantity_ordered with NO explicit shortfall marker (a pick_tasks row with
    status SHORT, or a wave_pick_breakdown.short_quantity > 0). Empty list when
    the SO is clean. Shared by record_ship (via _assert_no_silent_shortfall) and
    record_admin_ship (which turns it into a bypassable admin acknowledgement)."""
    return db.execute(
        text(
            """
            SELECT sol.so_line_id, i.sku, sol.quantity_ordered, sol.quantity_picked
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :so_id
               AND sol.quantity_picked < sol.quantity_ordered
               AND NOT EXISTS (
                   SELECT 1 FROM pick_tasks pt
                    WHERE pt.so_line_id = sol.so_line_id
                      AND pt.status = :short
               )
               AND NOT EXISTS (
                   SELECT 1 FROM wave_pick_breakdown wb
                    WHERE wb.so_line_id = sol.so_line_id
                      AND wb.short_quantity > 0
               )
             ORDER BY sol.so_line_id
            """
        ),
        {"so_id": so_id, "short": TASK_SHORT},
    ).fetchall()


def _shortfall_message(silently_short):
    """One-line summary of a silent-shortfall detection, naming the first SKU."""
    first = silently_short[0]
    more = "" if len(silently_short) == 1 else f" (+{len(silently_short) - 1} more)"
    return (
        "Cannot ship - line under-picked with no short-close marker: "
        f"sku={first.sku} ordered={first.quantity_ordered} picked={first.quantity_picked}{more}"
    )


def _assert_no_silent_shortfall(db, so_id):
    """Raise ValueError if any SO line is silently under-picked. Runs before any
    fulfillment write, so a refused ship leaves no fulfillment row. Used by
    record_ship (the warehouse ship path, where a shortfall is always an error)."""
    silently_short = _detect_silent_shortfall(db, so_id)
    if silently_short:
        raise ValueError(_shortfall_message(silently_short))


def _emit_ship_confirmed(
    db,
    *,
    so_id,
    so_external_id,
    warehouse_id,
    tracking_number,
    carrier,
    ship_method,
    source_txn_id,
    username,
    shipped_at,
    fulfillment_id,
):
    """Emit the single ship.confirmed/1 event for a fulfilled SO onto the
    integration_events outbox. Shared by record_ship and record_admin_ship so
    both emit byte-identical payloads.

    tracking_numbers[] is array-shaped (Sentry creates one fulfillment per SO,
    so exactly one entry, or [] for local pickup). Sentry's internal column is
    ship_method; the wire contract renames it to service_level. packages[]
    mirrors the single synthesised package from pack.confirmed.

    The payload carries source-system external_ids (the identifiers the
    upstream connector originally pushed to Pipe B), NOT Sentry's internal
    canonical UUIDs, so a receiver can correlate ship.confirmed back to its own
    marketplace record without re-querying cross_system_mappings. Falls back to
    the canonical UUID when no mapping row exists."""
    pack_lines = db.execute(
        text(
            """
            SELECT
                COALESCE(csm.source_id, CAST(i.external_id AS TEXT)) AS item_external_id,
                fulfillment.quantity_shipped AS quantity_packed,
                sol.so_line_id, fulfillment.bin_id,
                fulfillment.lot_number, fulfillment.serial_number
              FROM item_fulfillment_lines fulfillment
              JOIN sales_order_lines sol ON sol.so_line_id = fulfillment.so_line_id
              JOIN items i ON i.item_id = sol.item_id
              LEFT JOIN LATERAL (
                  SELECT source_id FROM cross_system_mappings
                  WHERE canonical_id = i.external_id AND source_type = 'item'
                  ORDER BY last_updated_at DESC LIMIT 1
              ) csm ON TRUE
             WHERE sol.so_id = :sid AND fulfillment.fulfillment_id = :fulfillment_id
             ORDER BY sol.line_number, fulfillment.fulfillment_line_id
            """
        ),
        {"sid": so_id, "fulfillment_id": fulfillment_id},
    ).fetchall()
    stats = db.execute(
        text(
            """
            SELECT COALESCE(SUM(i.weight_lbs * sol.quantity_picked), 0) AS total_weight,
                   (SELECT external_id FROM item_fulfillments WHERE fulfillment_id = :fulfillment_id) AS fulfillment_external_id
              FROM sales_order_lines sol
              JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_id = :sid
            """
        ),
        {"sid": so_id, "fulfillment_id": fulfillment_id},
    ).fetchone()
    # Resolve the SO's source-system identifier for the outbound payload.
    # Falls back to the canonical UUID when no cross_system_mappings row exists
    # (SO created directly in Sentry without a Pipe-B push). package_external_id
    # stays Sentry-internal (one fulfillment per SO, synthesised, no upstream
    # identity to map to); the canonical UUID is the stable stem for it.
    so_external_id_str = str(so_external_id)
    so_source_external_id = (
        resolve_source_external_id(db, "sales_order", so_external_id)
        or so_external_id_str
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
            "fulfillment_external_id": str(stats.fulfillment_external_id),
            # Local-pickup orders ship without a carrier label, so
            # tracking_number may be None. Emit [] instead of [None] so the
            # payload is valid string-array shaped.
            "tracking_numbers": (
                [tracking_number] if (tracking_number and tracking_number.strip()) else []
            ),
            "carrier": carrier,
            "service_level": ship_method,
            "packages": [
                {
                    "package_external_id": f"{so_external_id_str}-pkg-1",
                    "weight_lb": float(stats.total_weight) if stats.total_weight is not None else None,
                    "dimensions_in": None,
                    "lines": [
                        {
                            "item_external_id": str(line.item_external_id),
                            "quantity_packed": line.quantity_packed,
                            "so_line_id": line.so_line_id,
                            "bin_id": line.bin_id,
                            "lot_number": line.lot_number,
                            "serial_number": line.serial_number,
                        }
                        for line in pack_lines
                    ],
                },
            ],
            "completed_by_user_external_id": get_user_external_id(db, username),
            "completed_at": shipped_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )


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
    shipped_at_override=None,
    audit_details_extra=None,
    allocations=None,
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
      - shipped_at_override: explicit ship instant (tz-aware datetime) used
        for both item_fulfillments.shipped_at and the SO header instead of
        NOW(). The admin SO-edit ship path passes the operator-entered
        Shipped Date (anchored at noon company-local). None keeps NOW(),
        matching the cookie-auth and dockd surfaces.
      - audit_details_extra: dict merged into the audit_log.details body so
        dockd-specific attribution (station_label, manual_link, weight,
        dims, idempotency_key, operator_username) lands in the chained log.

    Returns dict with fulfillment_id, shipped_at, lines_shipped,
    total_quantity, audit_log_id.

    Raises ValueError when any SO line is silently under-picked - that is,
    quantity_picked < quantity_ordered AND no explicit shortfall marker
    (pick_tasks.status = SHORT or wave_pick_breakdown.short_quantity > 0)
    exists for the line. Historically this function silently omitted lines
    where quantity_picked = 0, letting the SO ship without the customer's
    full order; the guard makes that impossible.
    """
    # Layer 2C: line-level fulfillment guard. Mirrors complete_batch
    # (Layer 2A) and complete_packing (Layer 2B). Runs before the
    # fulfillment INSERT so a refused ship leaves no fulfillment row.
    _assert_no_silent_shortfall(db, so_id)

    # 1. Create item_fulfillments record
    result = db.execute(
        text(
            """
            INSERT INTO item_fulfillments (so_id, warehouse_id, tracking_number, carrier, ship_method, shipped_by, status, external_id, pre_ship_status, shipping_cost, shipped_at)
            VALUES (:so_id, :wh, :tracking, :carrier, :ship_method, :shipped_by, :shipped_status, :ext_id, :pre_status, :ship_cost, COALESCE(CAST(:shipped_at_override AS timestamptz), NOW()))
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
            "shipped_at_override": shipped_at_override,
        },
    )
    fulfillment_row = result.fetchone()
    fulfillment_id = fulfillment_row.fulfillment_id
    shipped_at = fulfillment_row.shipped_at

    # 2. Create fulfillment lines for each SO line with quantity_picked > 0
    so_lines = db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.item_id, sol.quantity_picked,
                   item.is_lot_tracked, item.is_serial_tracked
            FROM sales_order_lines sol
            JOIN items item ON item.item_id = sol.item_id
            WHERE sol.so_id = :so_id AND sol.quantity_picked > 0
            """
        ),
        {"so_id": so_id},
    ).fetchall()

    resolved_allocations = prepare_shipping_allocations(db, so_id, warehouse_id, so_lines, allocations)
    lines_shipped = len(so_lines)
    total_quantity = sum(line.quantity_picked for line in so_lines)
    for allocation in resolved_allocations:
        db.execute(
            text(
                """
                INSERT INTO item_fulfillment_lines (fulfillment_id, so_line_id, item_id, quantity_shipped, bin_id, lot_number, serial_number)
                VALUES (:fid, :so_line_id, :item_id, :quantity, :bin_id, :lot_number, :serial_number)
                """
            ),
            {
                "fid": fulfillment_id,
                **allocation,
            },
        )

    for line in so_lines:
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_shipped = quantity_picked, status = :status WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id, "status": SO_SHIPPED},
        )

    # 4. Update SO status with carrier and tracking
    db.execute(
        text(
            """
            UPDATE sales_orders
            SET status = :shipped_status, shipped_at = :so_shipped_at, carrier = :carrier, tracking_number = :tracking,
                ship_method = COALESCE(:ship_method, ship_method)
            WHERE so_id = :so_id
            """
        ),
        {
            "so_id": so_id,
            "carrier": carrier,
            "tracking": tracking_number,
            # The method Dockd sends reflects the carrier actually used, which
            # may differ from the customer-selected method captured at ingest.
            # COALESCE preserves the existing value when no method is supplied
            # (e.g. local-pickup orders ship without a carrier method).
            "ship_method": ship_method,
            "shipped_status": SO_SHIPPED,
            "so_shipped_at": shipped_at,
        },
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

    # 6. v1.5.0 #118: emit ship.confirmed on the integration_events outbox
    # (shared with record_admin_ship so the payloads cannot drift).
    _emit_ship_confirmed(
        db,
        so_id=so_id,
        so_external_id=so_external_id,
        warehouse_id=warehouse_id,
        tracking_number=tracking_number,
        carrier=carrier,
        ship_method=ship_method,
        source_txn_id=source_txn_id,
        username=username,
        shipped_at=shipped_at,
        fulfillment_id=fulfillment_id,
    )

    return {
        "fulfillment_id": fulfillment_id,
        "shipped_at": shipped_at,
        "lines_shipped": lines_shipped,
        "total_quantity": total_quantity,
        "audit_log_id": audit_log_id,
    }


class AdminShipError(Exception):
    """Operator submitted an admin-ship request that cannot be applied. kind
    maps to an HTTP status at the route layer:

      * 'not_found'         404 -- SO does not exist
      * 'not_eligible'      422 -- return/refund SO, not fulfillable
      * 'wrong_status'      422 -- SO not in PICKED / PACKED / SHIPPED
      * 'already_fulfilled' 409 -- SO already has a non-voided fulfillment
      * 'nothing_to_ship'   422 -- no line is picked-but-unshipped
      * 'silent_shortfall'  422 -- a line is under-picked with no short marker;
                                   bypassable with acknowledge_shortfall. Carries
                                   `lines` so the UI can name the blocking SKUs.
      * 'no_bin'            422 -- the SO's warehouse has no active bin to record
                                   the corrective fulfillment line against
    """

    def __init__(self, message, kind, **context):
        super().__init__(message)
        self.kind = kind
        self.context = context


_ADMIN_SHIPPABLE_STATUSES = (SO_PICKED, SO_PACKED, SO_SHIPPED)


def record_admin_ship(db, *, so_id, username, source_txn_id, acknowledge_shortfall=False):
    """Admin corrective ship: stamp shipped on a picked-but-unshipped SO
    (including the stranded header-SHIPPED / line-shipped=0 case) with the full
    fulfillment + audit bookkeeping, so the Create RMA button renders.

    Event emission splits on pre-ship status:
      * A stranded-SHIPPED SO already had its revenue/COGS booked in the GL at
        bulk-import time. Re-emitting ship.confirmed would double-count it
        downstream, so this path emits NOTHING -- the fulfillment + audit row
        are the whole correction.
      * A genuinely-unshipped PICKED / PACKED SO never reached the GL, so it
        DOES emit one ship.confirmed/1. Its carrier is derived from ship_method
        (an unshipped order has no carrier value yet).
    The already-fulfilled guard (409) still means at most one ship.confirmed is
    ever emitted per SO.

    A silent shortfall (a line under-picked with no SHORT / wave marker) is
    normally refused, but legacy imported orders never carry those markers -- so
    `acknowledge_shortfall=True` lets an admin ship the picked floor of a real
    pre-cutover partial. Ships ALL shippable lines (quantity_shipped =
    quantity_picked). Does NOT move inventory -- on-hand left the bin at pick
    time; stamping shipped is pure bookkeeping. Caller owns the transaction.
    """
    so = db.execute(
        text(
            "SELECT so_id, so_number, external_id, warehouse_id, status, "
            "       order_type, shipped_at, carrier, tracking_number, ship_method "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise AdminShipError(f"sales order {so_id} not found", kind="not_found")
    if not order_type_allows_fulfillment_ops(so.order_type):
        raise AdminShipError(
            "cannot admin-ship a return SO",
            kind="not_eligible",
            order_type=so.order_type,
        )
    if so.status not in _ADMIN_SHIPPABLE_STATUSES:
        raise AdminShipError(
            f"SO must be PICKED, PACKED or SHIPPED to admin-ship (current: {so.status})",
            kind="wrong_status",
            current_status=so.status,
        )

    # Safe-by-construction guard: never emit a second ship.confirmed for an SO
    # that already carries a live fulfillment.
    existing = db.execute(
        text(
            "SELECT 1 FROM item_fulfillments "
            "WHERE so_id = :sid AND status != 'VOIDED' LIMIT 1"
        ),
        {"sid": so_id},
    ).fetchone()
    if existing:
        raise AdminShipError(
            "SO already has a fulfillment; nothing to correct",
            kind="already_fulfilled",
        )

    # Under-pick guard -- bypassable by an explicit admin acknowledgement.
    # Legacy imported orders never carry SHORT / wave_pick_breakdown markers, so
    # a real pre-cutover partial would otherwise be unshippable with no operator
    # path. The UI surfaces the blocking SKUs and re-POSTs with the flag set.
    if not acknowledge_shortfall:
        shortfall = _detect_silent_shortfall(db, so_id)
        if shortfall:
            raise AdminShipError(
                _shortfall_message(shortfall),
                kind="silent_shortfall",
                lines=[
                    {
                        "sku": r.sku,
                        "ordered": r.quantity_ordered,
                        "picked": r.quantity_picked,
                    }
                    for r in shortfall
                ],
            )

    # Every line with unshipped picked stock. A POS sale (shipped already = its
    # picked/ordered, no fulfillment) yields nothing here -> nothing_to_ship.
    shippable = db.execute(
        text(
            """
            SELECT so_line_id, item_id, quantity_picked, quantity_shipped
              FROM sales_order_lines
             WHERE so_id = :sid AND quantity_picked > quantity_shipped
             ORDER BY so_line_id
            """
        ),
        {"sid": so_id},
    ).fetchall()
    if not shippable:
        raise AdminShipError(
            "no shippable lines (nothing picked-but-unshipped)",
            kind="nothing_to_ship",
        )

    # 1. One fulfillment header. Preserve the SO's existing carrier / tracking /
    # ship_method (a stranded-SHIPPED SO may already carry them; a PICKED order
    # has none) and its shipped_at, else NOW().
    fulfillment = db.execute(
        text(
            """
            INSERT INTO item_fulfillments
                (so_id, warehouse_id, tracking_number, carrier, ship_method,
                 shipped_by, status, external_id, pre_ship_status, shipped_at)
            VALUES
                (:sid, :wh, :tracking, :carrier, :ship_method,
                 :user, :shipped_status, :ext, :pre_status,
                 COALESCE(CAST(:existing_shipped_at AS timestamptz), NOW()))
            RETURNING fulfillment_id, shipped_at
            """
        ),
        {
            "sid": so_id,
            "wh": so.warehouse_id,
            "tracking": so.tracking_number,
            "carrier": so.carrier,
            "ship_method": so.ship_method,
            "user": username,
            "shipped_status": SO_SHIPPED,
            "ext": str(uuid.uuid4()),
            "pre_status": so.status,
            "existing_shipped_at": so.shipped_at,
        },
    ).fetchone()
    fulfillment_id = fulfillment.fulfillment_id
    shipped_at = fulfillment.shipped_at

    # A corrective fulfillment on a legacy stranded-SHIPPED order has no
    # pick_tasks, so resolve a real bin in the SO's warehouse rather than a
    # hardcoded id that could belong to another warehouse or not exist (FK 500).
    # item_fulfillment_lines.bin_id is write-only in this codebase -- nothing
    # reads it back for restock -- so any valid active bin in the SO's warehouse
    # satisfies the FK and keeps the record in the right warehouse.
    fallback_bin = db.execute(
        text(
            "SELECT bin_id FROM bins "
            " WHERE warehouse_id = :wh AND is_active "
            " ORDER BY bin_id ASC LIMIT 1"
        ),
        {"wh": so.warehouse_id},
    ).fetchone()

    # 2. Fulfillment lines + stamp quantity_shipped = quantity_picked.
    lines_shipped = 0
    total_quantity = 0
    for line in shippable:
        pick_task = db.execute(
            text(
                """
                SELECT bin_id FROM pick_tasks
                 WHERE so_id = :sid AND item_id = :iid AND status IN (:picked, :short)
                 ORDER BY pick_task_id ASC LIMIT 1
                """
            ),
            {"sid": so_id, "iid": line.item_id, "picked": TASK_PICKED, "short": TASK_SHORT},
        ).fetchone()
        if pick_task:
            bin_id = pick_task.bin_id
        elif fallback_bin is not None:
            bin_id = fallback_bin.bin_id
        else:
            raise AdminShipError(
                f"warehouse {so.warehouse_id} has no active bin to record the fulfillment",
                kind="no_bin",
            )
        newly_shipped = line.quantity_picked - line.quantity_shipped
        db.execute(
            text(
                """
                INSERT INTO item_fulfillment_lines
                    (fulfillment_id, so_line_id, item_id, quantity_shipped, bin_id)
                VALUES (:fid, :sol, :iid, :qty, :bin)
                """
            ),
            {"fid": fulfillment_id, "sol": line.so_line_id, "iid": line.item_id,
             "qty": newly_shipped, "bin": bin_id},
        )
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_shipped = quantity_picked, "
                "status = :status WHERE so_line_id = :sol"
            ),
            {"status": SO_SHIPPED, "sol": line.so_line_id},
        )
        lines_shipped += 1
        total_quantity += newly_shipped

    # 3. Header -> SHIPPED, keeping any existing shipped_at; carrier/tracking
    # are left as-is (this action does not introduce or overwrite them).
    db.execute(
        text(
            "UPDATE sales_orders SET status = :status, "
            "shipped_at = COALESCE(shipped_at, :shipped_at) WHERE so_id = :sid"
        ),
        {"status": SO_SHIPPED, "shipped_at": shipped_at, "sid": so_id},
    )

    # 4. Audit (ACTION_SHIP, like record_ship; source flags the admin origin).
    audit_log_id = write_audit_log(
        db,
        action_type=ACTION_SHIP,
        entity_type="SO",
        entity_id=so_id,
        user_id=username,
        warehouse_id=so.warehouse_id,
        details={
            "so_number": so.so_number,
            "fulfillment_id": fulfillment_id,
            "source": "admin_ship",
            "lines_shipped": lines_shipped,
            "total_quantity": total_quantity,
        },
    )

    # 5. ship.confirmed -- ONLY for a genuinely-unshipped order. `so.status` is
    # the pre-admin-ship status (captured before step 3's UPDATE). A stranded
    # SHIPPED SO already had its revenue/COGS booked in the GL at bulk-import
    # time; re-emitting would double-count downstream (a second Cash Sale JE,
    # negative inventory movements backdated into a closed period, a
    # customer-facing marketplace re-confirmation). For that repair path the
    # fulfillment + audit row are the whole correction -- emit nothing.
    if so.status != SO_SHIPPED:
        # An unshipped order has no carrier value (a real ship sets it), but
        # ship.confirmed/1 requires a non-null carrier. Derive one from the
        # ship method, exactly like the admin SO-edit manual-ship path.
        _emit_ship_confirmed(
            db,
            so_id=so_id,
            so_external_id=so.external_id,
            warehouse_id=so.warehouse_id,
            tracking_number=so.tracking_number,
            carrier=carrier_from_ship_method(so.ship_method),
            ship_method=so.ship_method,
            source_txn_id=source_txn_id,
            username=username,
            shipped_at=shipped_at,
            fulfillment_id=fulfillment_id,
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
    # 1. Revert the SO to its pre-ship status. Normally we also clear the
    # per-ship fields record_ship populated (tracking / carrier / shipped_at).
    # But a corrective admin-ship on a stranded-SHIPPED order (pre_ship_status
    # == SHIPPED) did not create those fields -- it only copied the SO's
    # pre-existing imported values into the fulfillment. Nulling them here would
    # permanently erase the order's original tracking and ship date, so for that
    # case revert the status only and leave the ship identity intact.
    if pre_ship_status == SO_SHIPPED:
        db.execute(
            text("UPDATE sales_orders SET status = :pre WHERE so_id = :so_id"),
            {"pre": pre_ship_status, "so_id": so_id},
        )
    else:
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
            "sales_order_external_id": str(so_external_id),
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
