"""Example connector -- reference implementation for connector authors.

This module demonstrates how to implement the BaseConnector interface.
It is NOT registered by default and is not intended for production use.
Every method includes comments explaining what a real connector would
do at each step.

To use this as a starting point for a new connector:

    1. Copy this file and rename it (e.g. netsuite.py)
    2. Replace the stub implementations with real API calls
    3. Add `from connectors import registry` and call
       `registry.register("your_name", YourConnector)` at the bottom
    4. Your connector will be auto-discovered on startup
"""

from datetime import datetime

from connectors.base import (
    BaseConnector,
    ConnectionResult,
    PushResult,
    SyncResult,
)


class ExampleConnector(BaseConnector):
    """A minimal connector that returns empty results for every operation.

    This proves the registration pattern works and serves as a template.
    Real connectors would make HTTP requests to the external system's API
    using credentials from self.config.
    """

    def sync_orders(self, since: datetime) -> SyncResult:
        """Pull sales orders from the external system.

        A real connector would:
        1. Build an API request filtered by modified_date > since
        2. Paginate through all results
        3. Transform each external order into Sentry WMS format
        4. Insert or update sales_orders and sales_order_lines in the DB
        5. Return the count of records processed

        Common edge cases to handle:
        - Duplicate order numbers (upsert by external ID)
        - Orders with items not yet in the item master (sync_items first)
        - Rate limiting from the external API (backoff and retry)
        """
        return SyncResult(success=True, records_synced=0)

    def sync_items(self, since: datetime) -> SyncResult:
        """Pull item master data from the external system.

        A real connector would:
        1. Fetch items modified after `since` from the external API
        2. Map external fields to Sentry WMS fields (sku, item_name, upc, etc.)
        3. Upsert into the items table by SKU
        4. Handle field mapping differences (e.g. "product_name" -> "item_name")

        Important: item sync should run before order sync so that
        order line items can reference existing SKUs.
        """
        return SyncResult(success=True, records_synced=0)

    def sync_inventory(self, since: datetime) -> SyncResult:
        """Pull inventory levels from the external system.

        A real connector would:
        1. Fetch inventory adjustments or snapshots from the external API
        2. Map external locations to Sentry WMS warehouses/bins
        3. Update inventory quantities accordingly

        Not all systems support this -- some are order-only.
        If your connector doesn't support inventory sync, still implement
        this method but return SyncResult(success=True, records_synced=0)
        and omit "sync_inventory" from get_capabilities().
        """
        return SyncResult(success=True, records_synced=0)

    def push_fulfillment(self, order_id: str, tracking: str, carrier: str) -> PushResult:
        """Push shipment confirmation back to the external system.

        A real connector would:
        1. Look up the external order by order_id
        2. Create a fulfillment/shipment record via the external API
        3. Attach the tracking number and carrier
        4. Return the external fulfillment ID for reference

        Error handling considerations:
        - The external order may have been cancelled (return error, don't retry)
        - The API may be temporarily down (raise so the retry system handles it)
        - Duplicate fulfillment pushes should be idempotent
        """
        return PushResult(success=True, external_id=None)

    def test_connection(self) -> ConnectionResult:
        """Verify credentials and endpoint connectivity.

        A real connector would:
        1. Make a lightweight API call (e.g. GET /account or /ping)
        2. Verify the response indicates valid authentication
        3. Return a meaningful message ("Connected as account XYZ")

        This is called from the admin panel setup wizard. Keep it fast --
        don't pull large datasets, just confirm the credentials work.
        """
        return ConnectionResult(connected=True, message="Example connector - no real connection")

    def get_config_schema(self) -> dict:
        """Define the configuration fields for the admin setup form.

        A real connector would return fields like:
        - api_key: the authentication token or key
        - base_url: the API endpoint (some ERPs have per-tenant URLs)
        - account_id: tenant or company identifier

        The admin panel renders a form from this schema and stores
        the values in the connector_configs table.
        """
        return {
            "api_key": {
                "type": "string",
                "required": True,
                "label": "API Key",
                "description": "Your API authentication key",
            },
            "base_url": {
                "type": "string",
                "required": True,
                "label": "Base URL",
                "description": "API endpoint URL (e.g. https://api.example.com)",
            },
        }

    def get_capabilities(self) -> list[str]:
        """Declare supported operations.

        A real connector would list only the operations it actually
        implements with real API calls. For example, a connector for
        a system that only sends orders and accepts fulfillments would
        return ["sync_orders", "push_fulfillment"].

        Valid values: "sync_orders", "sync_items", "sync_inventory", "push_fulfillment"
        """
        return ["sync_orders", "sync_items", "sync_inventory", "push_fulfillment"]


# Auto-register the example connector so the admin panel has something
# to show out of the box. Real connectors (netsuite, shopify, etc.) would
# follow the same pattern.
from connectors import registry as _registry
_registry.register("example", ExampleConnector)
