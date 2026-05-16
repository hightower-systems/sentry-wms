"""Vendor request schemas.

The canonical vendors table (db/schema.sql line 1221, mig 042) ships
without an admin-facing CRUD path upstream. avid-overhaul-mk1 P5
adds one so the Items page can link a SKU to a single canonical
vendor instead of carrying a free-text vendor name. Same locked
fields as the table itself; is_active defaults to True on create so
new vendors immediately participate in items / PO pickers.
"""

from typing import Optional

from pydantic import BaseModel, Field


class CreateVendorRequest(BaseModel):
    vendor_name: str = Field(..., min_length=1, max_length=200)
    contact_name: Optional[str] = Field(None, max_length=200)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=50)
    billing_address: Optional[str] = Field(None, max_length=2000)
    remit_to_address: Optional[str] = Field(None, max_length=2000)
    tax_id: Optional[str] = Field(None, max_length=64)
    payment_terms: Optional[str] = Field(None, max_length=64)
    is_active: Optional[bool] = Field(True)


class UpdateVendorRequest(BaseModel):
    vendor_name: Optional[str] = Field(None, min_length=1, max_length=200)
    contact_name: Optional[str] = Field(None, max_length=200)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=50)
    billing_address: Optional[str] = Field(None, max_length=2000)
    remit_to_address: Optional[str] = Field(None, max_length=2000)
    tax_id: Optional[str] = Field(None, max_length=64)
    payment_terms: Optional[str] = Field(None, max_length=64)
    is_active: Optional[bool] = Field(None)
