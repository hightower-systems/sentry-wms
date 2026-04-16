"""Base connector interface and result types for ERP/commerce integrations.

This module defines the contract that all Sentry WMS connectors must implement.
Connectors bridge external systems (NetSuite, BigCommerce, Shopify, etc.) with
the WMS by providing a standard interface for syncing orders, items, inventory,
and pushing fulfillment data back to the source system.

Result types use pydantic for validation and serialization.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Result types -- returned by connector methods
# ---------------------------------------------------------------------------


class SyncResult(BaseModel):
    """Outcome of a sync operation (orders, items, or inventory).

    Connectors return this from sync_orders(), sync_items(), and
    sync_inventory() to report how many records were pulled and
    whether any errors occurred during the sync.
    """

    success: bool = Field(..., description="True if the sync completed without fatal errors")
    records_synced: int = Field(0, ge=0, description="Number of records successfully synced")
    errors: list[str] = Field(default_factory=list, description="Non-fatal error messages encountered during sync")


class PushResult(BaseModel):
    """Outcome of pushing data back to the external system.

    Returned by push_fulfillment() to confirm that the ERP received
    the shipment/tracking data. external_id is the identifier the
    ERP assigned (e.g. NetSuite fulfillment ID).
    """

    success: bool = Field(..., description="True if the push was accepted by the external system")
    external_id: Optional[str] = Field(None, description="ID assigned by the external system, if any")
    error: Optional[str] = Field(None, description="Error message if the push failed")


class ConnectionResult(BaseModel):
    """Outcome of a connection test.

    Returned by test_connection() so the admin panel can show
    whether credentials and endpoints are valid before enabling a connector.
    """

    connected: bool = Field(..., description="True if the connection test succeeded")
    message: str = Field(..., description="Human-readable status message")


# ---------------------------------------------------------------------------
# Abstract base class -- the interface contract
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Abstract base class that all connectors must implement.

    Each connector represents an integration with one external system.
    Subclasses must implement every abstract method. The registry will
    refuse to register a class that does not fully implement this interface.

    Connectors are stateless -- they receive configuration (API keys,
    endpoints, etc.) at instantiation and use it for every call. No
    mutable state should be stored between method calls.
    """

    def __init__(self, config: dict):
        """Initialize the connector with its configuration.

        Args:
            config: Dictionary of settings for this connector instance
                    (API keys, base URLs, tenant IDs, etc.). The shape
                    is defined by get_config_schema().
        """
        self.config = config

    @abstractmethod
    def sync_orders(self, since: datetime) -> SyncResult:
        """Pull new or updated sales orders from the external system.

        Args:
            since: Only fetch orders created or modified after this timestamp.

        Returns:
            SyncResult with the count of orders synced and any errors.
        """

    @abstractmethod
    def sync_items(self, since: datetime) -> SyncResult:
        """Pull item master data (SKUs, descriptions, UPCs) from the external system.

        Args:
            since: Only fetch items created or modified after this timestamp.

        Returns:
            SyncResult with the count of items synced and any errors.
        """

    @abstractmethod
    def sync_inventory(self, since: datetime) -> SyncResult:
        """Pull inventory levels from the external system.

        Some systems push inventory to the WMS (e.g. initial stock counts),
        others are pull-only. Connectors that don't support this should
        omit 'sync_inventory' from get_capabilities() and return
        SyncResult(success=True, records_synced=0) here.

        Args:
            since: Only fetch inventory changes after this timestamp.

        Returns:
            SyncResult with the count of inventory records synced and any errors.
        """

    @abstractmethod
    def push_fulfillment(self, order_id: str, tracking: str, carrier: str) -> PushResult:
        """Push shipment confirmation back to the external system.

        Called after Sentry WMS ships an order. The connector should
        create a fulfillment record in the ERP with the tracking info.

        Args:
            order_id: The external system's order identifier.
            tracking: Tracking number for the shipment.
            carrier: Carrier name (e.g. "UPS", "FedEx").

        Returns:
            PushResult with the external fulfillment ID if successful.
        """

    @abstractmethod
    def test_connection(self) -> ConnectionResult:
        """Verify that the connector's credentials and endpoints are valid.

        Called from the admin panel when setting up or troubleshooting
        a connector. Should make a lightweight API call (e.g. fetch
        account info) to confirm the connection works.

        Returns:
            ConnectionResult indicating success or failure with a message.
        """

    @abstractmethod
    def get_config_schema(self) -> dict:
        """Return the configuration fields this connector needs.

        The admin panel uses this to render a setup form. Each key is
        a field name, and the value describes the field.

        Example return value::

            {
                "api_key": {"type": "string", "required": True, "label": "API Key"},
                "base_url": {"type": "string", "required": True, "label": "API Base URL"},
                "account_id": {"type": "string", "required": False, "label": "Account ID"},
            }

        Returns:
            Dict mapping field names to their type/label/required metadata.
        """

    @abstractmethod
    def get_capabilities(self) -> list[str]:
        """Declare which operations this connector supports.

        Not all external systems support all sync directions. For example,
        a POS system might only support sync_orders and push_fulfillment
        but not sync_items or sync_inventory.

        Valid capability strings:
            - "sync_orders"
            - "sync_items"
            - "sync_inventory"
            - "push_fulfillment"

        Returns:
            List of capability strings this connector supports.
        """
