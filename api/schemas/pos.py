"""v1.10.0 POS request body schemas.

Strict-typed Pydantic models with extra='forbid'. The POS surface
contract is DRAFT-v1; additive changes only without a header bump.

Field widths match the corresponding DB column widths so a Pydantic-
validated body cannot fail at INSERT on column length:

  sku           VARCHAR(50)   matching items.sku
  warehouse_id  VARCHAR(20)   matching warehouses.warehouse_code
  bin_id        VARCHAR(50)   matching bins.bin_code

The wire-level warehouse_id and bin_id are warehouse_code and bin_code
respectively (string), not the integer surrogate keys; the conversion
to integer IDs happens inside the route's bulk classification query.
"""

from datetime import datetime
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, UUID4
from typing_extensions import Annotated


# Column-width caps. Matching the DB columns means a Pydantic-validated
# body cannot fail at the SQL parameter binding on length.
_SKU_MAX = 50
_WAREHOUSE_CODE_MAX = 20
_BIN_CODE_MAX = 50

# Per-line quantity bound. ge=1 because a zero-qty line carries no
# meaning at this surface; le=10000 catches a runaway client integer
# overflow attempt without constraining legitimate single-cart
# bulk orders (a 200-line cart at 10000 qty/line is 2 million units,
# well above any realistic counter sale).
_QTY_MIN = 1
_QTY_MAX = 10000

# Per-cart line bound. min=1 because a zero-line cart is not a cart;
# max=200 is the same ceiling the v1.7.0 inbound batch surface uses,
# so a POS Service that can build inbound batches can also build
# validate-cart bodies with the same memory ceiling.
_LINES_MIN = 1
_LINES_MAX = 200


class ValidateCartLine(BaseModel):
    """One line in the POS cart pre-flight check.

    sku, warehouse_id, and bin_id are wire-level identifiers (strings).
    The route resolves them to integer surrogate keys via the bulk
    classification query.
    """

    model_config = ConfigDict(extra="forbid")

    sku:          str = Field(..., min_length=1, max_length=_SKU_MAX)
    warehouse_id: str = Field(..., min_length=1, max_length=_WAREHOUSE_CODE_MAX)
    bin_id:       str = Field(..., min_length=1, max_length=_BIN_CODE_MAX)
    quantity:     int = Field(..., ge=_QTY_MIN, le=_QTY_MAX)


class ValidateCartBody(BaseModel):
    """POST /api/v1/pos/validate-cart body."""

    model_config = ConfigDict(extra="forbid")

    lines: List[ValidateCartLine] = Field(
        ...,
        min_length=_LINES_MIN,
        max_length=_LINES_MAX,
    )


# ----------------------------------------------------------------------
# Checkout body
# ----------------------------------------------------------------------

# Width caps for the checkout-specific fields. Each matches the DB
# column or the upstream wire shape it is captured against.
_EXTERNAL_TXN_REF_MAX = 128       # matches sales_orders.external_txn_ref VARCHAR(128)
_CASHIER_ID_MAX       = 100       # matches audit_log.user_id VARCHAR(100)
_TERMINAL_ID_MAX      = 100
_FULFILLMENT_NOTE_MAX = 500       # operator-facing note; 500 matches the v1.9 void-reason cap
_CARD_BRAND_MAX       = 50        # 'Visa', 'Mastercard', etc.
_CARD_LAST4_LEN       = 4
_AUTH_CODE_MAX        = 50

# Per-cart cents bounds. ge=0 because zero-cents is meaningful for a
# fully-discounted line; the upper bound matches NUMERIC(12,2) cents
# (10**12 - 1) so an integer overflow in the POS Service cannot produce
# a value Sentry silently truncates.
_CENTS_MIN = 0
_CENTS_MAX = 10**12 - 1


class CheckoutLine(BaseModel):
    """One line in a counter sale.

    unit_price_cents / tax_cents / line_total_cents are trust-the-caller
    archival fields. Sentry stores them in audit_log.details (no per-
    line price columns; mig 056 did not add them) and never recomputes
    or validates the total. Pricing is the POS Service's domain.
    """

    model_config = ConfigDict(extra="forbid")

    sku:               str           = Field(..., min_length=1, max_length=_SKU_MAX)
    warehouse_id:      str           = Field(..., min_length=1, max_length=_WAREHOUSE_CODE_MAX)
    bin_id:            str           = Field(..., min_length=1, max_length=_BIN_CODE_MAX)
    quantity:          int           = Field(..., ge=_QTY_MIN, le=_QTY_MAX)
    unit_price_cents:  int           = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    tax_cents:         int           = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    line_total_cents:  int           = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    fulfillment_note:  Optional[str] = Field(None, max_length=_FULFILLMENT_NOTE_MAX)


class CardTender(BaseModel):
    """Card payment tender. The accepted card-side fields are an
    explicit allowlist: brand, last4, auth_code, external_ref. Any
    other field (card_pan, full_track, expiry, cvv) fails the
    extra='forbid' gate at the Pydantic boundary so Sentry never
    accepts PAN-shaped data on the wire (PCI scope guard).
    """

    model_config = ConfigDict(extra="forbid")

    type:         Literal["card"] = "card"
    amount_cents: int             = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    card_brand:   str             = Field(..., min_length=1, max_length=_CARD_BRAND_MAX)
    card_last4:   str             = Field(..., min_length=_CARD_LAST4_LEN, max_length=_CARD_LAST4_LEN)
    auth_code:    str             = Field(..., min_length=1, max_length=_AUTH_CODE_MAX)
    external_ref: str             = Field(..., min_length=1, max_length=_EXTERNAL_TXN_REF_MAX)


class CashTender(BaseModel):
    """Cash payment tender. amount_tendered_cents and change_cents are
    the cashier-facing breakdown: tendered = amount + change."""

    model_config = ConfigDict(extra="forbid")

    type:                  Literal["cash"] = "cash"
    amount_cents:          int             = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    amount_tendered_cents: int             = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    change_cents:          int             = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)


# Pydantic 2 discriminated union: the `type` field selects the model.
# A tender carrying `type: "card"` parses against CardTender (PAN
# rejected by extra='forbid'); `type: "cash"` parses against
# CashTender. An unknown `type` fails 422 invalid_body.
Tender = Annotated[
    Union[CardTender, CashTender],
    Field(discriminator="type"),
]


class PaymentSummary(BaseModel):
    """Header-level totals + tender breakdown. Trust-the-caller; Sentry
    archives the structure in audit_log.details and never recomputes."""

    model_config = ConfigDict(extra="forbid")

    method:         Literal["card", "cash"]
    subtotal_cents: int          = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    tax_cents:      int          = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    total_cents:    int          = Field(..., ge=_CENTS_MIN, le=_CENTS_MAX)
    tenders:        List[Tender] = Field(..., min_length=1, max_length=8)


class CheckoutBody(BaseModel):
    """POST /api/v1/pos/checkout body.

    idempotency_key is validated as UUID4 (not raw str) so a non-UUID
    value surfaces as 422 invalid_body instead of slipping into the
    cache as opaque bytes. completed_at is a wire timestamp (the
    cashier's stated completion time); Sentry uses it as
    sales_orders.shipped_at. created_at on the row is NOW().
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key:   UUID4
    external_txn_ref:  str             = Field(..., min_length=1, max_length=_EXTERNAL_TXN_REF_MAX)
    cashier_id:        str             = Field(..., min_length=1, max_length=_CASHIER_ID_MAX)
    terminal_id:       str             = Field(..., min_length=1, max_length=_TERMINAL_ID_MAX)
    completed_at:      datetime
    payment_summary:   PaymentSummary
    lines:             List[CheckoutLine] = Field(..., min_length=_LINES_MIN, max_length=_LINES_MAX)


# ----------------------------------------------------------------------
# Refund body
# ----------------------------------------------------------------------

# Original SO numbers issued by the POS surface follow "SO-POS-{integer}"
# (see routes/pos.py checkout). Refusing other shapes at the schema
# boundary prevents a non-POS so_number from ever reaching the DB
# query and getting conflated with 404 original_so_not_found.
_ORIGINAL_SO_RE = r"^SO-POS-\d+$"


class RefundBody(BaseModel):
    """POST /api/v1/pos/refund body.

    Full-order refund only in v1. The request body does NOT include
    line specifications: Sentry derives the line set from the
    original SO via the POS_CHECKOUT audit_log entry. external_refund_
    ref is the Windcave (or cash) reference for the refund leg
    itself; original_external_txn_ref is the original sale's
    DpsTxnRef, included for cross-check (Sentry does not require it
    to match but it is captured in the refund's audit_log details).
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key:           UUID4
    original_so_id:            str             = Field(..., pattern=_ORIGINAL_SO_RE, max_length=_SKU_MAX + 16)
    original_external_txn_ref: str             = Field(..., min_length=1, max_length=_EXTERNAL_TXN_REF_MAX)
    external_refund_ref:       str             = Field(..., min_length=1, max_length=_EXTERNAL_TXN_REF_MAX)
    cashier_id:                str             = Field(..., min_length=1, max_length=_CASHIER_ID_MAX)
    terminal_id:               str             = Field(..., min_length=1, max_length=_TERMINAL_ID_MAX)
    completed_at:              datetime
    refund_summary:            PaymentSummary
