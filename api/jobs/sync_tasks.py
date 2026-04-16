"""Background sync tasks for connector operations.

Each task loads a connector from the registry, loads credentials from
the vault, tracks state via sync_state_service, and handles retries.
Tasks run in the Celery worker process, never blocking Flask requests.

State machine per task:
    idle -> running -> idle (success, consecutive_errors reset to 0)
    idle -> running -> idle (error, consecutive_errors incremented)
    ... after 3 consecutive errors: status flips to 'error' (sticky)

Duplicate run prevention: set_running raises DuplicateRunError if a
sync is already running. The task skips the retry in that case.
"""

import logging
from datetime import datetime, timezone

from celery.exceptions import Ignore

from connectors import registry
from jobs import celery_app
from services.credential_vault import get_all_credentials_standalone
from services.sync_state_service import (
    DuplicateRunError,
    get_last_success_standalone,
    set_error_standalone,
    set_running_standalone,
    set_success_standalone,
)

logger = logging.getLogger(__name__)

# Earliest reasonable 'since' when a connector has never been synced.
# Connectors that treat the absence of 'since' specially should handle this.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _run_sync(self, connector_name: str, warehouse_id: int, sync_type: str, method_name: str):
    """Shared implementation for sync_orders / sync_items / sync_inventory."""
    try:
        set_running_standalone(connector_name, warehouse_id, sync_type)
    except DuplicateRunError as exc:
        # Another worker is already running this sync. Don't retry.
        logger.info("%s skipped: %s", sync_type, exc)
        raise Ignore()

    try:
        connector_cls = registry.get(connector_name)
        config = get_all_credentials_standalone(connector_name, warehouse_id)
        connector = connector_cls(config=config)

        since = get_last_success_standalone(connector_name, warehouse_id, sync_type) or _EPOCH
        method = getattr(connector, method_name)
        result = method(since=since)

        if result.success:
            set_success_standalone(connector_name, warehouse_id, sync_type)
            logger.info(
                "%s complete: connector=%s warehouse=%d records=%d",
                sync_type, connector_name, warehouse_id, result.records_synced,
            )
        else:
            error_msg = "; ".join(result.errors) if result.errors else "sync returned success=False"
            set_error_standalone(connector_name, warehouse_id, sync_type, error_msg)
            logger.warning(
                "%s returned errors: connector=%s warehouse=%d errors=%s",
                sync_type, connector_name, warehouse_id, error_msg,
            )

        return {"success": result.success, "records_synced": result.records_synced}

    except Exception as exc:
        set_error_standalone(connector_name, warehouse_id, sync_type, str(exc))
        logger.error("%s failed: connector=%s error=%s", sync_type, connector_name, str(exc))
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_orders(self, connector_name: str, warehouse_id: int):
    """Pull new/updated sales orders from an external system."""
    return _run_sync(self, connector_name, warehouse_id, "orders", "sync_orders")


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_items(self, connector_name: str, warehouse_id: int):
    """Pull item master data from an external system."""
    return _run_sync(self, connector_name, warehouse_id, "items", "sync_items")


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_inventory(self, connector_name: str, warehouse_id: int):
    """Pull inventory levels from an external system."""
    return _run_sync(self, connector_name, warehouse_id, "inventory", "sync_inventory")


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def push_fulfillment(self, connector_name: str, warehouse_id: int, order_id: str, tracking: str, carrier: str):
    """Push shipment confirmation back to an external system.

    Uses the 'fulfillment' sync_type for state tracking so operators
    can monitor whether outbound fulfillment pushes are succeeding.
    """
    try:
        set_running_standalone(connector_name, warehouse_id, "fulfillment")
    except DuplicateRunError as exc:
        logger.info("fulfillment skipped: %s", exc)
        raise Ignore()

    try:
        connector_cls = registry.get(connector_name)
        config = get_all_credentials_standalone(connector_name, warehouse_id)
        connector = connector_cls(config=config)

        result = connector.push_fulfillment(order_id=order_id, tracking=tracking, carrier=carrier)

        if result.success:
            set_success_standalone(connector_name, warehouse_id, "fulfillment")
            logger.info(
                "push_fulfillment complete: connector=%s order=%s external_id=%s",
                connector_name, order_id, result.external_id,
            )
        else:
            error_msg = result.error or "push returned success=False"
            set_error_standalone(connector_name, warehouse_id, "fulfillment", error_msg)
            logger.warning(
                "push_fulfillment returned error: connector=%s order=%s error=%s",
                connector_name, order_id, error_msg,
            )

        return {"success": result.success, "external_id": result.external_id}

    except Exception as exc:
        set_error_standalone(connector_name, warehouse_id, "fulfillment", str(exc))
        logger.error("push_fulfillment failed: connector=%s order=%s error=%s", connector_name, order_id, str(exc))
        raise self.retry(exc=exc)
