"""Tests for Celery app configuration and sync tasks.

All tests run in eager mode (CELERY_ALWAYS_EAGER) so they execute
synchronously without needing a running Redis instance.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from jobs import celery_app
from jobs.sync_tasks import sync_orders, sync_items, sync_inventory, push_fulfillment


@pytest.fixture(autouse=True)
def _eager_celery():
    """Run all celery tasks synchronously for testing."""
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
    )
    yield


# ---------------------------------------------------------------------------
# Celery app initialization
# ---------------------------------------------------------------------------


class TestCeleryApp:
    def test_app_initializes(self):
        """Celery app should be importable and configured."""
        assert celery_app is not None
        assert celery_app.main == "sentry_wms"

    def test_serializer_is_json(self):
        """Task serializer should be JSON for safe cross-process communication."""
        assert celery_app.conf.task_serializer == "json"

    def test_utc_enabled(self):
        """Celery should use UTC to avoid timezone confusion in sync timestamps."""
        assert celery_app.conf.enable_utc is True


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------


class TestTaskDiscovery:
    def test_sync_orders_registered(self):
        """sync_orders task should be discoverable by Celery."""
        assert "jobs.sync_tasks.sync_orders" in celery_app.tasks

    def test_sync_items_registered(self):
        assert "jobs.sync_tasks.sync_items" in celery_app.tasks

    def test_sync_inventory_registered(self):
        assert "jobs.sync_tasks.sync_inventory" in celery_app.tasks

    def test_push_fulfillment_registered(self):
        assert "jobs.sync_tasks.push_fulfillment" in celery_app.tasks


# ---------------------------------------------------------------------------
# Task execution in eager mode
# ---------------------------------------------------------------------------


class TestSyncTasks:
    """Run tasks synchronously using a registered example connector."""

    @pytest.fixture(autouse=True)
    def _register_example(self):
        """Register the example connector for task tests."""
        from connectors import registry
        from connectors.example import ExampleConnector
        registry.register("example", ExampleConnector)
        yield
        # Clean up - remove from registry
        registry._connectors.pop("example", None)

    def test_sync_orders_eager(self):
        result = sync_orders.apply(args=["example", 1]).get()
        assert result["success"] is True
        assert result["records_synced"] == 0

    def test_sync_items_eager(self):
        result = sync_items.apply(args=["example", 1]).get()
        assert result["success"] is True
        assert result["records_synced"] == 0

    def test_sync_inventory_eager(self):
        result = sync_inventory.apply(args=["example", 1]).get()
        assert result["success"] is True
        assert result["records_synced"] == 0

    def test_push_fulfillment_eager(self):
        result = push_fulfillment.apply(args=["example", 1, "ORD-1", "TRACK-1", "UPS"]).get()
        assert result["success"] is True

    def test_unknown_connector_raises(self):
        """Task should raise when connector is not in the registry."""
        from celery.exceptions import Retry
        with pytest.raises(Retry):
            sync_orders.apply(args=["nonexistent", 1]).get()

    def test_task_returns_dict(self):
        """Task results should be JSON-serializable dicts."""
        result = sync_orders.apply(args=["example", 1]).get()
        assert isinstance(result, dict)
        assert "success" in result
        assert "records_synced" in result

    def test_push_fulfillment_returns_external_id(self):
        """push_fulfillment result should include external_id field."""
        result = push_fulfillment.apply(args=["example", 1, "ORD-99", "TRK-99", "FedEx"]).get()
        assert "external_id" in result
