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
from utils.log_sanitize import scrub_secrets

logger = logging.getLogger(__name__)

# Earliest reasonable 'since' when a connector has never been synced.
# Connectors that treat the absence of 'since' specially should handle this.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _run_sync(self, connector_name: str, warehouse_id: int, sync_type: str, method_name: str):
    """Shared implementation for sync_orders / sync_items / sync_inventory."""
    try:
        # V-102: set_running_standalone returns the run_id we must quote on
        # completion. If the sync runs long enough to be taken over by a
        # new run (V-012 RUNNING_TIMEOUT), our completion will no-op against
        # the new run's state instead of clobbering it back to 'idle'.
        run_id = set_running_standalone(connector_name, warehouse_id, sync_type)
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
            applied = set_success_standalone(connector_name, warehouse_id, sync_type, run_id)
            if applied:
                logger.info(
                    "%s complete: connector=%s warehouse=%d records=%d",
                    sync_type, connector_name, warehouse_id, result.records_synced,
                )
            else:
                logger.warning(
                    "%s completed but state was taken over by a newer run; "
                    "skipping success write: connector=%s warehouse=%d records=%d",
                    sync_type, connector_name, warehouse_id, result.records_synced,
                )
        else:
            error_msg = "; ".join(result.errors) if result.errors else "sync returned success=False"
            applied = set_error_standalone(connector_name, warehouse_id, sync_type, error_msg, run_id)
            if applied:
                logger.warning(
                    "%s returned errors: connector=%s warehouse=%d errors=%s",
                    sync_type, connector_name, warehouse_id, error_msg,
                )
            else:
                logger.warning(
                    "%s errored but state was taken over by a newer run; "
                    "skipping error write: connector=%s warehouse=%d errors=%s",
                    sync_type, connector_name, warehouse_id, error_msg,
                )

        return {"success": result.success, "records_synced": result.records_synced}

    except Exception as exc:
        # V-007: scrub URL userinfo / sensitive query values before the
        # exception string hits sync_state.last_error_message or the log.
        safe_error = scrub_secrets(exc)
        set_error_standalone(connector_name, warehouse_id, sync_type, safe_error, run_id)
        logger.error("%s failed: connector=%s error=%s", sync_type, connector_name, safe_error)
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
def fulfillment_health_check(self, connector_name: str, warehouse_id: int):
    """Verify fulfillment push capability.

    Runs the connector's test_connection to confirm the endpoint is
    reachable. Updates fulfillment sync state so operators can see
    whether outbound pushes would succeed. Real fulfillment pushes
    happen via push_fulfillment when orders ship.
    """
    try:
        run_id = set_running_standalone(connector_name, warehouse_id, "fulfillment")
    except DuplicateRunError as exc:
        logger.info("fulfillment health check skipped: %s", exc)
        raise Ignore()

    try:
        connector_cls = registry.get(connector_name)
        config = get_all_credentials_standalone(connector_name, warehouse_id)
        connector = connector_cls(config=config)

        result = connector.test_connection()
        if result.connected:
            applied = set_success_standalone(connector_name, warehouse_id, "fulfillment", run_id)
            if applied:
                logger.info(
                    "fulfillment health check ok: connector=%s warehouse=%d",
                    connector_name, warehouse_id,
                )
            else:
                logger.warning(
                    "fulfillment health check ok but state was taken over; skipping write: "
                    "connector=%s warehouse=%d",
                    connector_name, warehouse_id,
                )
        else:
            set_error_standalone(connector_name, warehouse_id, "fulfillment", result.message, run_id)
            logger.warning(
                "fulfillment health check failed: connector=%s message=%s",
                connector_name, result.message,
            )

        return {"success": result.connected, "message": result.message}

    except Exception as exc:
        safe_error = scrub_secrets(exc)
        set_error_standalone(connector_name, warehouse_id, "fulfillment", safe_error, run_id)
        logger.error("fulfillment health check failed: connector=%s error=%s", connector_name, safe_error)
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def push_fulfillment(self, connector_name: str, warehouse_id: int, order_id: str, tracking: str, carrier: str):
    """Push shipment confirmation back to an external system.

    Uses the 'fulfillment' sync_type for state tracking so operators
    can monitor whether outbound fulfillment pushes are succeeding.
    """
    try:
        run_id = set_running_standalone(connector_name, warehouse_id, "fulfillment")
    except DuplicateRunError as exc:
        logger.info("fulfillment skipped: %s", exc)
        raise Ignore()

    try:
        connector_cls = registry.get(connector_name)
        config = get_all_credentials_standalone(connector_name, warehouse_id)
        connector = connector_cls(config=config)

        result = connector.push_fulfillment(order_id=order_id, tracking=tracking, carrier=carrier)

        if result.success:
            applied = set_success_standalone(connector_name, warehouse_id, "fulfillment", run_id)
            if applied:
                logger.info(
                    "push_fulfillment complete: connector=%s order=%s external_id=%s",
                    connector_name, order_id, result.external_id,
                )
            else:
                logger.warning(
                    "push_fulfillment succeeded but state was taken over; skipping write: "
                    "connector=%s order=%s external_id=%s",
                    connector_name, order_id, result.external_id,
                )
        else:
            error_msg = result.error or "push returned success=False"
            set_error_standalone(connector_name, warehouse_id, "fulfillment", error_msg, run_id)
            logger.warning(
                "push_fulfillment returned error: connector=%s order=%s error=%s",
                connector_name, order_id, error_msg,
            )

        return {"success": result.success, "external_id": result.external_id}

    except Exception as exc:
        safe_error = scrub_secrets(exc)
        set_error_standalone(connector_name, warehouse_id, "fulfillment", safe_error, run_id)
        logger.error("push_fulfillment failed: connector=%s order=%s error=%s", connector_name, order_id, safe_error)
        raise self.retry(exc=exc)
