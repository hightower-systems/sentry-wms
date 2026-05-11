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


class CreateSalesOrderRequest(BaseModel):
    so_number: str = Field(..., min_length=1, max_length=128)
    warehouse_id: int = Field(..., gt=0)
    lines: List[SOLineEntry] = Field(..., min_length=1)
    so_barcode: Optional[str] = Field(None, max_length=128)
    customer_name: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_address: Optional[str] = Field(None, max_length=512)
    ship_method: Optional[str] = Field(None, max_length=100)
    ship_address: Optional[str] = Field(None, max_length=512)
    ship_by_date: Optional[str] = Field(None, max_length=32)
    # v1.9.0: free-text operator-facing note. TEXT column has no
    # length cap; the schema-level 4 KB ceiling here keeps a single
    # bad PATCH from inflating an SO row indefinitely.
    memo: Optional[str] = Field(None, max_length=4096)


class UpdateSalesOrderRequest(BaseModel):
    so_number: Optional[str] = Field(None, min_length=1, max_length=128)
    so_barcode: Optional[str] = Field(None, max_length=128)
    customer_name: Optional[str] = Field(None, max_length=256)
    customer_phone: Optional[str] = Field(None, max_length=64)
    customer_address: Optional[str] = Field(None, max_length=512)
    ship_method: Optional[str] = Field(None, max_length=100)
    ship_address: Optional[str] = Field(None, max_length=512)
    ship_by_date: Optional[str] = Field(None, max_length=32)
    priority: Optional[int] = Field(None, ge=0, le=10)
    memo: Optional[str] = Field(None, max_length=4096)


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
