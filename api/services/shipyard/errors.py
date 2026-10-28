"""Typed errors shared by the shipyard API and service layer."""

from __future__ import annotations

from typing import Any


class ShipyardServiceError(Exception):
    code = "shipyard_error"
    status_code = 400

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ShipyardNotFoundError(ShipyardServiceError):
    code = "shipyard_not_found"
    status_code = 404


class ShipyardConflictError(ShipyardServiceError):
    code = "shipyard_conflict"
    status_code = 409


class ShipyardValidationError(ShipyardServiceError):
    code = "shipyard_validation_error"
    status_code = 400


class ShipyardStateError(ShipyardServiceError):
    code = "shipyard_state_conflict"
    status_code = 409


class ManifestNotFoundError(ShipyardNotFoundError):
    code = "manifest_not_found"


class ManifestStateError(ShipyardStateError):
    code = "manifest_state_conflict"


class DuplicateManifestFileError(ShipyardConflictError):
    code = "duplicate_manifest_file"


class ZoneConfigurationError(ShipyardServiceError):
    code = "zone_configuration_required"
    status_code = 422


class BindingConfigurationError(ShipyardServiceError):
    code = "binding_configuration_required"
    status_code = 422


class WarehouseNotFoundError(ShipyardNotFoundError):
    code = "warehouse_not_found"


class ConfigConflictError(ShipyardConflictError):
    code = "shipyard_config_conflict"


class TicketNotFoundError(ShipyardNotFoundError):
    code = "ticket_not_found"


class TicketStateError(ShipyardStateError):
    code = "ticket_state_conflict"


class PalletValidationError(ShipyardServiceError):
    code = "pallet_validation_failed"
    status_code = 422


class PalletDuplicateError(ShipyardConflictError):
    code = "pallet_duplicate"


class PhotoValidationError(ShipyardServiceError):
    code = "photo_validation_failed"
    status_code = 422


class PhotoNotFoundError(ShipyardNotFoundError):
    code = "photo_not_found"


class ShortageStateError(ShipyardStateError):
    code = "shortage_state_conflict"


class DistributionImportNotFoundError(ShipyardNotFoundError):
    code = "distribution_import_not_found"


class DistributionImportStateError(ShipyardStateError):
    code = "distribution_import_state_conflict"


class DuplicateDistributionFileError(ShipyardConflictError):
    code = "duplicate_distribution_file"


class DispatchNotFoundError(ShipyardNotFoundError):
    code = "dispatch_not_found"


class DispatchStateError(ShipyardStateError):
    code = "dispatch_state_conflict"


class DeliveryNoteNotFoundError(ShipyardNotFoundError):
    code = "delivery_note_not_found"


class DeliveryNoteStateError(ShipyardStateError):
    code = "delivery_note_state_conflict"


class DispatchMatchError(ShipyardValidationError):
    code = "dispatch_match_failed"
    status_code = 422


class OutboundManifestNotFoundError(ShipyardNotFoundError):
    code = "outbound_manifest_not_found"


class OutboundManifestStateError(ShipyardStateError):
    code = "outbound_manifest_state_conflict"


class DuplicateOutboundManifestFileError(ShipyardConflictError):
    code = "duplicate_outbound_manifest_file"


class OutboundOrderNotFoundError(ShipyardNotFoundError):
    code = "outbound_order_not_found"


class OutboundTaskNotFoundError(ShipyardNotFoundError):
    code = "outbound_task_not_found"


class OutboundStateError(ShipyardStateError):
    code = "outbound_state_conflict"


class OutboundValidationError(ShipyardValidationError):
    code = "outbound_validation_failed"
    status_code = 422


class OcrUnavailableError(ShipyardServiceError):
    code = "ocr_unavailable"
    status_code = 503


class OcrProviderError(ShipyardServiceError):
    code = "ocr_provider_error"
    status_code = 502


class LabelRecognitionNotFoundError(ShipyardNotFoundError):
    code = "label_recognition_not_found"
