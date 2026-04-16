"""Background sync tasks for connector operations.

Each task loads a connector from the registry, calls the appropriate
sync method, and handles retries on failure. Tasks are designed to be
called from the API (e.g. admin panel triggers a sync) and run in the
Celery worker process, never blocking the Flask request thread.

Vault integration (Phase 3) and sync state tracking (Phase 4) are
stubbed out with TODO comments -- the task infrastructure and retry
logic is what matters in this phase.
"""

import logging

from jobs import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_orders(self, connector_name: str, warehouse_id: int):
    """Pull new/updated sales orders from an external system.

    Args:
        connector_name: Registry key for the connector (e.g. "netsuite").
        warehouse_id: Target warehouse to associate synced orders with.
    """
    try:
        # 1. Load connector class from registry
        from connectors import registry
        connector_cls = registry.get(connector_name)

        # 2. Load credentials from vault (Phase 3 -- stub for now)
        # TODO: credentials = vault.get_credentials(connector_name, warehouse_id)
        config = {}

        # 3. Instantiate connector
        connector = connector_cls(config=config)

        # 4. Update sync state to "running" (Phase 4 -- stub for now)
        # TODO: sync_state.update(connector_name, warehouse_id, "orders", status="running")

        # 5. Call connector.sync_orders()
        from datetime import datetime, timezone
        result = connector.sync_orders(since=datetime.now(timezone.utc))

        # 6. Update sync state with result (Phase 4 -- stub for now)
        # TODO: sync_state.update(connector_name, warehouse_id, "orders",
        #                         status="complete", records=result.records_synced)

        logger.info(
            "sync_orders complete: connector=%s warehouse=%d records=%d",
            connector_name, warehouse_id, result.records_synced,
        )
        return {"success": result.success, "records_synced": result.records_synced}

    except Exception as exc:
        logger.error("sync_orders failed: connector=%s error=%s", connector_name, str(exc))
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_items(self, connector_name: str, warehouse_id: int):
    """Pull item master data from an external system.

    Args:
        connector_name: Registry key for the connector.
        warehouse_id: Target warehouse context.
    """
    try:
        from connectors import registry
        connector_cls = registry.get(connector_name)

        # TODO: credentials = vault.get_credentials(connector_name, warehouse_id)
        config = {}

        connector = connector_cls(config=config)

        # TODO: sync_state.update(connector_name, warehouse_id, "items", status="running")

        from datetime import datetime, timezone
        result = connector.sync_items(since=datetime.now(timezone.utc))

        # TODO: sync_state.update(connector_name, warehouse_id, "items",
        #                         status="complete", records=result.records_synced)

        logger.info(
            "sync_items complete: connector=%s warehouse=%d records=%d",
            connector_name, warehouse_id, result.records_synced,
        )
        return {"success": result.success, "records_synced": result.records_synced}

    except Exception as exc:
        logger.error("sync_items failed: connector=%s error=%s", connector_name, str(exc))
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_inventory(self, connector_name: str, warehouse_id: int):
    """Pull inventory levels from an external system.

    Args:
        connector_name: Registry key for the connector.
        warehouse_id: Target warehouse context.
    """
    try:
        from connectors import registry
        connector_cls = registry.get(connector_name)

        # TODO: credentials = vault.get_credentials(connector_name, warehouse_id)
        config = {}

        connector = connector_cls(config=config)

        # TODO: sync_state.update(connector_name, warehouse_id, "inventory", status="running")

        from datetime import datetime, timezone
        result = connector.sync_inventory(since=datetime.now(timezone.utc))

        # TODO: sync_state.update(connector_name, warehouse_id, "inventory",
        #                         status="complete", records=result.records_synced)

        logger.info(
            "sync_inventory complete: connector=%s warehouse=%d records=%d",
            connector_name, warehouse_id, result.records_synced,
        )
        return {"success": result.success, "records_synced": result.records_synced}

    except Exception as exc:
        logger.error("sync_inventory failed: connector=%s error=%s", connector_name, str(exc))
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def push_fulfillment(self, connector_name: str, order_id: str, tracking: str, carrier: str):
    """Push shipment confirmation back to an external system.

    Args:
        connector_name: Registry key for the connector.
        order_id: External system's order identifier.
        tracking: Tracking number for the shipment.
        carrier: Carrier name (e.g. "UPS").
    """
    try:
        from connectors import registry
        connector_cls = registry.get(connector_name)

        # TODO: credentials = vault.get_credentials(connector_name)
        config = {}

        connector = connector_cls(config=config)

        result = connector.push_fulfillment(
            order_id=order_id,
            tracking=tracking,
            carrier=carrier,
        )

        logger.info(
            "push_fulfillment complete: connector=%s order=%s success=%s external_id=%s",
            connector_name, order_id, result.success, result.external_id,
        )
        return {"success": result.success, "external_id": result.external_id}

    except Exception as exc:
        logger.error("push_fulfillment failed: connector=%s order=%s error=%s", connector_name, order_id, str(exc))
        raise self.retry(exc=exc)
