"""CSV/JSON import row schemas (V-015).

Each schema validates one record from the /api/admin/import/<type>
endpoint. Text fields reject leading characters that would turn the
cell into a formula when the data is later exported and opened in
a spreadsheet (=, +, -, @, tab, CR). Numeric fields are coerced by
pydantic, so values like "abc" are rejected before reaching the
database layer.

Field names accept the variants the original CSV import allowed
(``name`` or ``item_name``; ``quantity`` or ``qty``; etc.) via
``model_config = ConfigDict(populate_by_name=True)`` alongside
``alias`` declarations where needed. Unknown keys are silently
ignored -- exotic columns in a vendor-produced spreadsheet should
not cause a 400 on an otherwise valid row.
"""

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Leading characters that a spreadsheet treats as the start of a formula.
# Also includes ASCII tab and carriage return which some apps interpret
# as formula continuations.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _reject_formula_prefix(value):
    """Raise if ``value`` (a str) starts with a formula-injection prefix."""
    if value is None:
        return value
    as_str = str(value)
    if as_str and as_str[0] in _FORMULA_PREFIXES:
        raise ValueError(
            f"Cell cannot start with {as_str[0]!r} (formula injection prevention)"
        )
    return as_str


class _BaseImportRow(BaseModel):
    """Base for all CSV import rows. Extra keys are ignored so real-world
    spreadsheets with odd columns still import the fields we care about.
    Text fields run through a formula-prefix sanitizer."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class ItemImportRow(_BaseImportRow):
    sku: str = Field(..., min_length=1, max_length=128)
    item_name: Optional[str] = Field(None, max_length=256)
    name: Optional[str] = Field(None, max_length=256)  # synonym accepted
    description: Optional[str] = Field(None, max_length=1000)
    upc: Optional[str] = Field(None, max_length=128)
    category: Optional[str] = Field(None, max_length=128)
    weight_lbs: Optional[Decimal] = Field(None, ge=0, le=99999)
    weight: Optional[Decimal] = Field(None, ge=0, le=99999)  # synonym accepted
    default_bin: Optional[str] = Field(None, max_length=64)
    quantity: Optional[int] = Field(None, ge=0, le=1000000)
    qty: Optional[int] = Field(None, ge=0, le=1000000)  # synonym accepted

    @field_validator(
        "sku", "item_name", "name", "description", "upc", "category", "default_bin",
        mode="before",
    )
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)

    def resolved_name(self) -> Optional[str]:
        return self.item_name or self.name

    def resolved_weight(self):
        return self.weight_lbs if self.weight_lbs is not None else self.weight

    def resolved_quantity(self) -> Optional[int]:
        return self.quantity if self.quantity is not None else self.qty


# ---------------------------------------------------------------------------
# Bins
# ---------------------------------------------------------------------------


class BinImportRow(_BaseImportRow):
    bin_code: str = Field(..., min_length=1, max_length=64)
    bin_barcode: Optional[str] = Field(None, max_length=128)
    zone: Optional[str] = Field(None, max_length=64)
    zone_id: Optional[int] = Field(None, gt=0)
    warehouse_id: Optional[int] = Field(None, gt=0)
    bin_type: Optional[str] = Field(None, max_length=32)
    aisle: Optional[str] = Field(None, max_length=32)
    row_num: Optional[int] = Field(None, ge=0)
    level_num: Optional[int] = Field(None, ge=0)
    pick_sequence: Optional[int] = Field(None, ge=0)
    putaway_sequence: Optional[int] = Field(None, ge=0)
    description: Optional[str] = Field(None, max_length=200)

    @field_validator(
        "bin_code", "bin_barcode", "zone", "bin_type", "aisle", "description",
        mode="before",
    )
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


class PurchaseOrderImportRow(_BaseImportRow):
    po_number: str = Field(..., min_length=1, max_length=128)
    sku: str = Field(..., min_length=1, max_length=128)
    quantity: Optional[int] = Field(None, gt=0, le=1000000)
    quantity_expected: Optional[int] = Field(None, gt=0, le=1000000)
    warehouse_id: Optional[int] = Field(None, gt=0)
    vendor: Optional[str] = Field(None, max_length=200)
    expected_date: Optional[str] = Field(None, max_length=32)

    @field_validator("po_number", "sku", "vendor", "expected_date", mode="before")
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)

    def resolved_quantity(self) -> Optional[int]:
        return self.quantity if self.quantity is not None else self.quantity_expected


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


class SalesOrderImportRow(_BaseImportRow):
    so_number: str = Field(..., min_length=1, max_length=128)
    sku: str = Field(..., min_length=1, max_length=128)
    quantity: Optional[int] = Field(None, gt=0, le=1000000)
    quantity_ordered: Optional[int] = Field(None, gt=0, le=1000000)
    warehouse_id: Optional[int] = Field(None, gt=0)
    customer: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_address: Optional[str] = Field(None, max_length=512)
    # v1.8.0 (#288): per-order billing + shipping addresses, one column
    # per component. CSV column names match canonical column names so
    # import / export round-trips. max_length matches the canonical
    # VARCHAR exactly. Each field is independently formula-protected
    # via _BaseImportRow.
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

    @field_validator(
        "so_number", "sku", "customer", "customer_phone", "customer_address",
        "billing_address_name", "billing_address_line1", "billing_address_line2",
        "billing_address_city", "billing_address_state",
        "billing_address_postal_code", "billing_address_country",
        "billing_address_phone",
        "shipping_address_name", "shipping_address_line1", "shipping_address_line2",
        "shipping_address_city", "shipping_address_state",
        "shipping_address_postal_code", "shipping_address_country",
        "shipping_address_phone",
        mode="before",
    )
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)

    def resolved_quantity(self) -> Optional[int]:
        return self.quantity if self.quantity is not None else self.quantity_ordered


# ---------------------------------------------------------------------------
# Transfer orders (v1.8.0 #291)
# ---------------------------------------------------------------------------


class TransferOrderImportRow(_BaseImportRow):
    """One row in the TO CSV import. Header-level fields (source +
    destination warehouse code, optional notes) live on the import
    request, not the row, so the route's header-consistency check is
    automatic by request-shape rather than row-by-row aggregation."""

    sku: str = Field(..., min_length=1, max_length=128)
    quantity: int = Field(..., gt=0, le=1000000)

    @field_validator("sku", mode="before")
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)


# ---------------------------------------------------------------------------
# Inventory adjustments (v1.10.1 #329)
# ---------------------------------------------------------------------------


class InventoryAdjustmentImportRow(_BaseImportRow):
    """One row in the inventory adjustment CSV import. Each row resolves
    to a single auto-approved inventory_adjustments insert plus the
    matching inventory on-hand mutation. qty is signed: positive adds,
    negative subtracts (with a sufficient-stock check at apply time).
    memo lands in inventory_adjustments.reason_detail; reason_code is
    fixed to CORRECTION on this path."""

    sku: str = Field(..., min_length=1, max_length=128)
    warehouse: str = Field(..., min_length=1, max_length=64)
    bin: str = Field(..., min_length=1, max_length=64)
    qty: int = Field(..., ge=-1000000, le=1000000)
    memo: Optional[str] = Field(None, max_length=500)

    @field_validator("sku", "warehouse", "bin", "memo", mode="before")
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)

    @field_validator("qty")
    @classmethod
    def _qty_nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("qty must be non-zero")
        return v
