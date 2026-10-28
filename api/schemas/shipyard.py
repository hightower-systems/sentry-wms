"""Request schemas for the shipyard warehouse domain."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from constants import (
    SHIPYARD_DISPATCH_SOURCE_EXCEL,
    SHIPYARD_DISPATCH_SOURCE_IMAGE_OCR,
    SHIPYARD_DISPATCH_SOURCE_MANUAL,
    SHIPYARD_SHORTAGE_IN_TRANSIT,
    SHIPYARD_SHORTAGE_REPORTED,
    SHIPYARD_STATUS_ACTIVE,
    SHIPYARD_STATUS_INACTIVE,
)


def _strip_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()


def _strip_required_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("must not be blank")
    return text


class CreateProjectBindingRequest(BaseModel):
    ship_no: str = Field(..., min_length=1, max_length=64)
    engineering_no: str = Field(default="", max_length=64)
    production_drawing_no: str = Field(default="", max_length=128)
    manufacturer_name: str = Field(default="", max_length=200)
    effective_from: AwareDatetime | None = None

    @field_validator("ship_no")
    @classmethod
    def strip_required_ship_no(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator(
        "engineering_no",
        "production_drawing_no",
        "manufacturer_name",
    )
    @classmethod
    def strip_binding_fields(cls, value: str) -> str:
        return value.strip()


class UpdateProjectBindingRequest(BaseModel):
    ship_no: str | None = Field(None, min_length=1, max_length=64)
    engineering_no: str | None = Field(None, max_length=64)
    production_drawing_no: str | None = Field(None, max_length=128)
    manufacturer_name: str | None = Field(None, max_length=200)
    status: str | None = Field(None, max_length=16)
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None

    @field_validator("ship_no")
    @classmethod
    def strip_optional_ship_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required_text(value)

    @field_validator(
        "engineering_no",
        "production_drawing_no",
        "manufacturer_name",
    )
    @classmethod
    def strip_optional_binding_fields(cls, value: str | None) -> str | None:
        return _strip_text(value)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in (
            SHIPYARD_STATUS_ACTIVE,
            SHIPYARD_STATUS_INACTIVE,
        ):
            raise ValueError("status must be ACTIVE or INACTIVE")
        return value


class CreateZoneShipMappingRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    bin_id: int = Field(..., gt=0)
    ship_no: str = Field(..., min_length=1, max_length=64)
    is_primary: bool = True
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None

    @field_validator("ship_no")
    @classmethod
    def strip_ship_no(cls, value: str) -> str:
        return _strip_required_text(value)


class UpdateZoneShipMappingRequest(BaseModel):
    bin_id: int | None = Field(None, gt=0)
    ship_no: str | None = Field(None, min_length=1, max_length=64)
    is_primary: bool | None = None
    status: str | None = Field(None, max_length=16)
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None

    @field_validator("ship_no")
    @classmethod
    def strip_optional_ship_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required_text(value)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in (
            SHIPYARD_STATUS_ACTIVE,
            SHIPYARD_STATUS_INACTIVE,
        ):
            raise ValueError("status must be ACTIVE or INACTIVE")
        return value


class ConfirmInboundManifestRequest(BaseModel):
    """Confirmation intentionally carries no editable location or quantity."""


class VoidInboundManifestRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return _strip_required_text(value)


class PalletScanRequest(BaseModel):
    tag_suffix: str | None = Field(None, pattern=r"^[0-9]+-[0-9]+$")
    qr_payload: str | None = Field(None, min_length=1, max_length=4096)
    ship_no: str | None = Field(None, max_length=64)
    engineering_no: str | None = Field(None, max_length=64)
    production_drawing_no: str | None = Field(None, max_length=128)
    qr_pallet_no: str | None = Field(None, max_length=128)
    batch_no: str | None = Field(None, max_length=128)
    manufacturer_name: str | None = Field(None, max_length=200)
    installation_pallet_no: str | None = Field(None, max_length=128)
    installation_pallet_name: str | None = Field(None, max_length=255)
    destination: str | None = Field(None, max_length=200)
    label_quantity: Decimal | None = Field(
        None,
        gt=0,
        max_digits=18,
        decimal_places=3,
    )

    @field_validator("tag_suffix", mode="before")
    @classmethod
    def strip_optional_suffix(cls, value: str | None) -> str | None:
        return _strip_text(value) or None

    @field_validator("qr_payload", mode="before")
    @classmethod
    def strip_qr_payload(cls, value: str | None) -> str | None:
        return _strip_text(value) or None

    @field_validator(
        "ship_no",
        "engineering_no",
        "production_drawing_no",
        "qr_pallet_no",
        "batch_no",
        "manufacturer_name",
        "installation_pallet_no",
        "installation_pallet_name",
        "destination",
    )
    @classmethod
    def strip_optional_scan_fields(cls, value: str | None) -> str | None:
        return _strip_text(value) or None


class ReceiptPalletRequest(PalletScanRequest):
    photo_id: UUID
    label_recognition_id: UUID | None = None


class ReceiveTicketRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    received_quantity: Decimal = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=3,
    )
    difference_reason: str | None = Field(None, max_length=500)
    client_device_id: str | None = Field(None, max_length=128)
    pallets: list[ReceiptPalletRequest] = Field(..., min_length=1, max_length=100)
    group_photo_id: UUID | None = None
    area_photo_id: UUID | None = None
    difference_photo_id: UUID | None = None

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def strip_idempotency_key(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator(
        "difference_reason",
        "client_device_id",
    )
    @classmethod
    def strip_optional_receipt_fields(cls, value: str | None) -> str | None:
        return _strip_text(value) or None


class ShortageReportRequest(BaseModel):
    status: Literal[
        SHIPYARD_SHORTAGE_REPORTED,
        SHIPYARD_SHORTAGE_IN_TRANSIT,
    ]
    note: str = Field(..., min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def strip_shortage_note(cls, value: str) -> str:
        return _strip_required_text(value)


class ConfirmDistributionImportRequest(BaseModel):
    """Distribution imports intentionally carry no editable rows on confirm."""


class VoidDistributionImportRequest(VoidInboundManifestRequest):
    """Reason required when voiding a distribution-demand import."""


class DispatchLineRequest(BaseModel):
    line_no: str = Field(..., min_length=1, max_length=32)
    engineering_no: str = Field(..., min_length=1, max_length=64)
    pallet_no: str = Field(..., min_length=1, max_length=128)
    material_spec_raw: str = Field(..., min_length=1, max_length=2000)
    material_code: str | None = Field(None, max_length=128)
    material_name: str | None = Field(None, max_length=255)
    specification: str | None = Field(None, max_length=2000)
    quantity: Decimal = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=3,
    )
    unit: str = Field(..., min_length=1, max_length=32)
    expected_arrival_date: date | None = None
    keeper_name: str | None = Field(None, max_length=100)
    keeper_phone: str | None = Field(None, max_length=50)
    remarks: str | None = Field(None, max_length=500)
    demand_line_id: UUID | None = None

    @field_validator(
        "line_no",
        "engineering_no",
        "pallet_no",
        "material_spec_raw",
        "unit",
    )
    @classmethod
    def strip_required_dispatch_fields(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator(
        "material_code",
        "material_name",
        "specification",
        "keeper_name",
        "keeper_phone",
        "remarks",
    )
    @classmethod
    def strip_optional_dispatch_fields(cls, value: str | None) -> str | None:
        return _strip_text(value) or None


class CreateDispatchRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    dispatch_no: str = Field(..., min_length=1, max_length=128)
    dispatch_date: date
    source_type: Literal[
        SHIPYARD_DISPATCH_SOURCE_MANUAL,
        SHIPYARD_DISPATCH_SOURCE_EXCEL,
        SHIPYARD_DISPATCH_SOURCE_IMAGE_OCR,
    ] = SHIPYARD_DISPATCH_SOURCE_MANUAL
    source_file_name: str | None = Field(None, max_length=255)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    lines: list[DispatchLineRequest] = Field(..., min_length=1, max_length=200)

    @field_validator("dispatch_no", "idempotency_key")
    @classmethod
    def strip_required_dispatch_document_fields(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator("source_file_name")
    @classmethod
    def strip_optional_dispatch_source(cls, value: str | None) -> str | None:
        return _strip_text(value) or None


class MatchDispatchLineRequest(BaseModel):
    demand_line_id: UUID


class CreateDeliveryNoteRequest(BaseModel):
    delivery_note_no: str = Field(..., min_length=1, max_length=128)
    issue_date: date
    idempotency_key: str = Field(..., min_length=8, max_length=128)

    @field_validator("delivery_note_no", "idempotency_key")
    @classmethod
    def strip_required_delivery_fields(cls, value: str) -> str:
        return _strip_required_text(value)


class ConfirmOutboundManifestRequest(BaseModel):
    """Confirmation uses the server-side allocation preview."""


class OutboundPalletScanRequest(BaseModel):
    tag_suffix: str = Field(..., pattern=r"^[1-9][0-9]*-[1-9][0-9]*$")
    qr_payload: str | None = Field(None, min_length=1, max_length=4096)
    ship_no: str | None = Field(None, max_length=64)
    engineering_no: str | None = Field(None, max_length=64)
    production_drawing_no: str | None = Field(None, max_length=128)
    material_code: str | None = Field(None, min_length=1, max_length=128)
    item_name: str | None = Field(None, min_length=1, max_length=255)
    batch_no: str | None = Field(None, max_length=128)
    label_quantity: Decimal | None = Field(
        None,
        gt=0,
        max_digits=18,
        decimal_places=3,
    )

    @field_validator("tag_suffix")
    @classmethod
    def strip_outbound_scan_suffix(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator(
        "qr_payload",
        "ship_no",
        "engineering_no",
        "production_drawing_no",
        "material_code",
        "item_name",
        "batch_no",
    )
    @classmethod
    def strip_outbound_scan_optional(cls, value: str | None) -> str | None:
        return _strip_text(value) or None


class OutboundPalletEvidenceRequest(OutboundPalletScanRequest):
    photo_id: UUID
    label_recognition_id: UUID | None = None


class ConfirmOutboundTaskRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    client_device_id: str | None = Field(None, max_length=128)
    pallets: list[OutboundPalletEvidenceRequest] = Field(
        ...,
        min_length=1,
        max_length=100,
    )

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def strip_outbound_idempotency_key(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator("client_device_id")
    @classmethod
    def strip_outbound_client_device(cls, value: str | None) -> str | None:
        return _strip_text(value) or None


class ConfirmOutboundLoadingRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    client_device_id: str | None = Field(None, max_length=128)
    notes: str | None = Field(None, max_length=500)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def strip_loading_idempotency_key(cls, value: str) -> str:
        return _strip_required_text(value)

    @field_validator("client_device_id", "notes")
    @classmethod
    def strip_loading_optional(cls, value: str | None) -> str | None:
        return _strip_text(value) or None
