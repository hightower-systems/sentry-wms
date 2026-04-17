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

    @field_validator(
        "so_number", "sku", "customer", "customer_phone", "customer_address",
        mode="before",
    )
    @classmethod
    def _no_formula(cls, v):
        return _reject_formula_prefix(v)

    def resolved_quantity(self) -> Optional[int]:
        return self.quantity if self.quantity is not None else self.quantity_ordered
