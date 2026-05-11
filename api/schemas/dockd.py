"""v1.9.0 dockd request body schemas.

Strict-typed Pydantic models with extra='forbid'. The dockd surface
contract is DRAFT-v1; additive changes only without a header bump.
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, UUID4


# Tracking / carrier / ship_method bounds match item_fulfillments column
# widths so a Pydantic-validated body cannot fail at INSERT on column
# length: tracking_number VARCHAR(100), carrier VARCHAR(50),
# ship_method VARCHAR(50) (per db/schema.sql).
_TRACKING_MAX = 100
_CARRIER_MAX = 50
_SHIP_METHOD_MAX = 50
# Operator username matches users.username and the audit_log.user_id /
# item_fulfillments.shipped_by VARCHAR(100) widths.
_USERNAME_MAX = 100
# Free-text reason on void; matches item_fulfillments.void_reason
# VARCHAR(500) added in mig 054.
_VOID_REASON_MAX = 500


class Dimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    l: float = Field(..., gt=0, description="length in inches")
    w: float = Field(..., gt=0, description="width in inches")
    h: float = Field(..., gt=0, description="height in inches")


class ShipBody(BaseModel):
    """POST /api/v1/dockd/orders/<so_number>/ship body.

    idempotency_key is validated as UUID4 (not raw str) so a non-UUID
    value surfaces as 422 invalid_body instead of slipping into the
    cache as opaque bytes.
    """

    model_config = ConfigDict(extra="forbid")

    tracking: str = Field(..., min_length=1, max_length=_TRACKING_MAX)
    carrier: str = Field(..., min_length=1, max_length=_CARRIER_MAX)
    ship_method: Optional[str] = Field(None, min_length=1, max_length=_SHIP_METHOD_MAX)
    operator_username: str = Field(..., min_length=1, max_length=_USERNAME_MAX)
    shipping_cost: Optional[Decimal] = Field(
        None, ge=0, max_digits=12, decimal_places=2
    )
    weight: Optional[float] = Field(None, gt=0)
    dims: Optional[Dimensions] = None
    manual_link: bool = False
    idempotency_key: UUID4


class VoidShipBody(BaseModel):
    """POST /api/v1/dockd/orders/<so_number>/void-ship body.

    Body is intentionally minimal: the void route reverts a previously-
    successful ship. The operator typed a free-text reason in the dockd
    UI ("wrong box dimensions", "label printed but never applied",
    etc.); the cap matches item_fulfillments.void_reason VARCHAR(500).
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=_VOID_REASON_MAX)
    operator_username: str = Field(..., min_length=1, max_length=_USERNAME_MAX)
    idempotency_key: UUID4
