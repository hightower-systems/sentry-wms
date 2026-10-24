"""Sales order request schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


# v1.8.0 (#288): per-component address fields. Names match the
# canonical column names exactly so import / export / API round-trips
# are mechanical. max_length matches the canonical VARCHAR.
ADDRESS_FIELD_NAMES = (
    "billing_address_name", "billing_address_line1", "billing_address_line2",
    "billing_address_city", "billing_address_state",
    "billing_address_postal_code", "billing_address_country",
    "billing_address_phone",
    "shipping_address_name", "shipping_address_line1", "shipping_address_line2",
    "shipping_address_city", "shipping_address_state",
    "shipping_address_postal_code", "shipping_address_country",
    "shipping_address_phone",
)


class SOLineEntry(BaseModel):
    item_id: int = Field(..., gt=0)
    quantity_ordered: int = Field(..., gt=0, le=1000000)
    line_number: Optional[int] = Field(None, ge=1)


class AdminPickLineEntry(BaseModel):
    """One {line, bin, quantity} entry in an admin virtual-pick request.

    Multiple entries may share the same so_line_id (split-bin pick:
    line N drawn from bin A and bin B in one submit); the route layer
    sums them per line for the over-pick guard.
    """
    so_line_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=1000000)


class AdminPickRequest(BaseModel):
    """Batched admin virtual-pick payload. All-or-nothing: any failure
    inside record_admin_pick rolls back every line in the batch."""
    lines: List[AdminPickLineEntry] = Field(..., min_length=1)


class RmaLineEntry(BaseModel):
    """One return line in a Create-RMA request: which item, how many to expect
    back, and the original line it derives from."""
    item_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=1000000)
    original_so_line_id: Optional[int] = Field(None, gt=0)


class CreateRmaRequest(BaseModel):
    lines: List[RmaLineEntry] = Field(..., min_length=1)
    # Optional operator note on the RMA, stored in sales_orders.memo (mig
    # 054). Free-form CSR text; same cap as the SO memo PATCH.
    memo: Optional[str] = Field(None, max_length=4000)


class ReceiveRmaRequest(BaseModel):
    """Receive one item into one bin against a return SO (the <orig>-RMA)."""
    item_id: int = Field(..., gt=0)
    so_line_id: Optional[int] = Field(None, gt=0)
    expected_quantity_received: Optional[int] = Field(None, ge=0)
    quantity: int = Field(..., gt=0, le=1000000)
    warehouse_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    notes: Optional[str] = Field(None, max_length=500)
    lot_number: Optional[str] = Field(None, max_length=50)
    serial_number: Optional[str] = Field(None, max_length=100)
    # Idempotency anchor -- a stable UUID the handheld generates once per receive
    # action and re-sends on retry. Dedups on item_receipts.external_id (UNIQUE)
    # so a double-tap doesn't double-restock. Optional for back-compat; when
    # absent the server mints a fresh receipt external_id (non-idempotent).
    idempotency_key: Optional[str] = Field(None, max_length=64)


class CreateSalesOrderRequest(BaseModel):
    so_number: str = Field(..., min_length=1, max_length=128)
    warehouse_id: int = Field(..., gt=0)
    lines: List[SOLineEntry] = Field(..., min_length=1)
    so_barcode: Optional[str] = Field(None, max_length=128)
    customer_name: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_email: Optional[str] = Field(None, max_length=255)
    customer_address: Optional[str] = Field(None, max_length=512)
    ship_method: Optional[str] = Field(None, max_length=100)
    ship_address: Optional[str] = Field(None, max_length=512)
    ship_by_date: Optional[str] = Field(None, max_length=32)
    # v1.9.0: free-text operator-facing note. TEXT column has no
    # length cap; the schema-level 4 KB ceiling here keeps a single
    # bad PATCH from inflating an SO row indefinitely.
    memo: Optional[str] = Field(None, max_length=4096)
    # mig 063: free-text upstream-origin label. No allowlist; whatever
    # the connector / operator hands us lands as-is up to 64 chars.
    order_origin: Optional[str] = Field(None, max_length=64)


class UpdateSalesOrderRequest(BaseModel):
    so_number: Optional[str] = Field(None, min_length=1, max_length=128)
    so_barcode: Optional[str] = Field(None, max_length=128)
    customer_name: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_email: Optional[str] = Field(None, max_length=255)
    customer_address: Optional[str] = Field(None, max_length=512)
    ship_method: Optional[str] = Field(None, max_length=100)
    ship_address: Optional[str] = Field(None, max_length=512)
    ship_by_date: Optional[str] = Field(None, max_length=32)
    priority: Optional[int] = Field(None, ge=0, le=10)
    memo: Optional[str] = Field(None, max_length=4096)
    # Shipment-state fields editable by ADMIN. Used to backfill SOs
    # that shipped via a retiring external system so the warehouse
    # view reflects accurate status. Status is
    # one of the canonical SO statuses; carrier/tracking_number/shipped_at
    # are the same values the dockd ship endpoint writes through the
    # service layer. Free-text NotificationOrigin column unchanged.
    status: Optional[str] = Field(None, max_length=32)
    carrier: Optional[str] = Field(None, max_length=64)
    tracking_number: Optional[str] = Field(None, max_length=128)
    # shipped_at is a calendar date (YYYY-MM-DD): the operator-facing
    # Shipped Date. The route anchors it at noon in COMPANY_TIMEZONE
    # before storing. Empty string clears to NULL.
    shipped_at: Optional[str] = Field(None, max_length=64)
    # source_system reassignment (mig 062). ADMIN-only OR
    # so-full-edit override; the canonical allowlist FK enforces that
    # the value is a recognised tag. Empty string clears the column.
    source_system: Optional[str] = Field(None, max_length=64)
    # mig 063: free-text upstream-origin label (e.g. "amazon",
    # "phone-order"). No allowlist; same status gate as the other
    # header fields. Empty string clears the column.
    order_origin: Optional[str] = Field(None, max_length=64)


class AddSalesOrderLineRequest(BaseModel):
    """Add a new line to an existing SO.

    item_id must already exist; the handler rejects duplicates against
    (so_id, item_id) so allocation/pick paths stay deterministic when
    fetching lines by item_id.
    """

    item_id: int = Field(..., gt=0)
    quantity_ordered: int = Field(..., gt=0, le=1000000)


class UpdateSalesOrderLineRequest(BaseModel):
    """Edit an existing SO line's quantity_ordered.

    Reducing below quantity_picked / quantity_shipped is rejected (those
    units have already left their source bin or shipped). Reducing below
    quantity_allocated triggers an allocation release on the backend.
    """

    quantity_ordered: int = Field(..., gt=0, le=1000000)


class RevertSalesOrderStatusRequest(BaseModel):
    """so-refinement: admin demotes an SO from PICKED/PACKED/SHIPPED
    back to an earlier status. The body picks which already-PICKED
    pick_tasks should release their inventory back to the source bin.
    Tasks not in this list stay PICKED; if any picked qty remains and
    new_status is below PICKED, the backend rejects with 409 so an
    operator does not end up with a status / inventory mismatch.

    Empty list is valid: PACKED to PICKED needs no pick release (just
    zeros quantity_packed), and SHIPPED to PACKED needs no release
    either (just clears the shipment fields).
    """

    new_status: str = Field(..., min_length=1, max_length=32)
    release_pick_task_ids: List[int] = Field(default_factory=list)


class UpdateSalesOrderAddressRequest(BaseModel):
    """v1.8.0 (#288): dedicated PATCH body for editing the 16
    structured billing/shipping address fields on a sales_order.

    Every field is optional. Fields not present in the body are left
    unchanged. An empty string is treated as an explicit clear (NULL
    on the canonical column); use null in JSON to leave unchanged.
    """

    billing_address_name:        Optional[str] = Field(None, max_length=200)
    billing_address_line1:       Optional[str] = Field(None, max_length=200)
    billing_address_line2:       Optional[str] = Field(None, max_length=200)
    billing_address_city:        Optional[str] = Field(None, max_length=100)
    billing_address_state:       Optional[str] = Field(None, max_length=100)
    billing_address_postal_code: Optional[str] = Field(None, max_length=32)
    billing_address_country:     Optional[str] = Field(None, max_length=64)
    billing_address_phone:       Optional[str] = Field(None, max_length=64)
    shipping_address_name:        Optional[str] = Field(None, max_length=200)
    shipping_address_line1:       Optional[str] = Field(None, max_length=200)
    shipping_address_line2:       Optional[str] = Field(None, max_length=200)
    shipping_address_city:        Optional[str] = Field(None, max_length=100)
    shipping_address_state:       Optional[str] = Field(None, max_length=100)
    shipping_address_postal_code: Optional[str] = Field(None, max_length=32)
    shipping_address_country:     Optional[str] = Field(None, max_length=64)
    shipping_address_phone:       Optional[str] = Field(None, max_length=64)

    @model_validator(mode="after")
    def _at_least_one_field_present(self) -> "UpdateSalesOrderAddressRequest":
        if not self.model_fields_set:
            raise ValueError(
                "address PATCH body must set at least one field; "
                "send an empty string to clear a field."
            )
        return self


class UpdateLineAllocationRequest(BaseModel):
    """Inline stepper adjustment for a sales_order_line's standing inventory
    reservation. ``delta`` is the signed step (+ reserve more, - release); the
    handler clamps the result to [quantity_picked, quantity_ordered] and
    reconciles inventory.quantity_allocated by the same amount. A rarely-used
    manual knob -- the picking flow normalizes reservations on its own."""

    delta: int = Field(..., ge=-1000000, le=1000000)


class PartialFulfillLineEntry(BaseModel):
    """Partial-fulfill: per-line short input.

    short_qty is the number of units the operator is declaring as
    unshippable for this line; it shifts onto the new BO SO. The
    handler enforces ``short_qty <= quantity_ordered - quantity_picked``
    at request time (server-side; the UI's max= attribute is hinting,
    not authoritative)."""

    so_line_id: int = Field(..., gt=0)
    short_qty:  int = Field(..., gt=0, le=1000000)


class PartialFulfillRequest(BaseModel):
    """Partial-fulfill: POST body for
    /admin/sales-orders/<so_id>/partial-fulfill.

    lines carries one entry per shorted line; lines that are NOT
    shorted are simply omitted (no zero-qty entries needed). At
    least one entry is required.

    reason_detail is free-text operator-supplied context that lands
    in the audit row and in the inventory_adjustments.reason_detail
    column on any SHORT adjustments written. Optional; the handler
    falls back to a generated stub when omitted."""

    lines: List[PartialFulfillLineEntry] = Field(..., min_length=1, max_length=200)
    reason_detail: Optional[str] = Field(None, max_length=500)


class CancelBackorderRequest(BaseModel):
    """Partial-fulfill: POST body for
    /admin/sales-orders/<bo_so_id>/cancel-backorder.

    cancellation_reason is an app-enforced enum; valid values live
    in constants.CANCEL_REASONS. Defaults to 'other' if omitted."""

    cancellation_reason: Optional[str] = Field("other", max_length=50)


class MarkSalesOrdersPrintedRequest(BaseModel):
    """mig 064: POST body to stamp printed_at on a batch of SOs. Sent by
    the picking-ticket print page after the client-side render confirms
    the ticket reached the operator. The server is intentionally NOT the
    trigger: Print All fetches data for many SOs and any of them could
    fail to render client-side; marking them printed unconditionally
    would silently drop those tickets from the queue."""

    so_ids: List[int] = Field(..., min_length=1, max_length=200)


class UpdateSalesOrderMemoRequest(BaseModel):
    """PATCH body for the free-form CSR memo on a sales_order, shown on
    the Outbound > Fraud page. An empty string is treated as an explicit
    clear (NULL). The length cap matches the operational reality -- memos
    are CSR notes, not log dumps."""

    memo: Optional[str] = Field(..., max_length=4000)
