"""Tests for the connector interface contract and registry.

Covers:
- Properly implemented connectors register successfully
- Connectors missing required methods raise TypeError at registration
- Registry discover/list/get operations
- Result types validate correctly
- Example connector implements the full interface
"""

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from connectors import ConnectorRegistry
from connectors.base import (
    BaseConnector,
    ConnectionResult,
    PushResult,
    SyncResult,
)
from connectors.example import ExampleConnector


# ---------------------------------------------------------------------------
# Helpers -- minimal connector implementations for testing
# ---------------------------------------------------------------------------


class CompleteConnector(BaseConnector):
    """A fully implemented connector for testing registration."""

    def sync_orders(self, since):
        return SyncResult(success=True, records_synced=3)

    def sync_items(self, since):
        return SyncResult(success=True, records_synced=5)

    def sync_inventory(self, since):
        return SyncResult(success=True, records_synced=0)

    def push_fulfillment(self, order_id, tracking, carrier):
        return PushResult(success=True, external_id="EXT-123")

    def test_connection(self):
        return ConnectionResult(connected=True, message="OK")

    def get_config_schema(self):
        return {"api_key": {"type": "string", "required": True, "label": "Key"}}

    def get_capabilities(self):
        return ["sync_orders", "sync_items", "push_fulfillment"]


# ---------------------------------------------------------------------------
# Result type validation
# ---------------------------------------------------------------------------


class TestSyncResult:
    def test_valid(self):
        r = SyncResult(success=True, records_synced=10)
        assert r.success is True
        assert r.records_synced == 10
        assert r.errors == []

    def test_with_errors(self):
        r = SyncResult(success=False, errors=["timeout", "rate limited"])
        assert r.success is False
        assert len(r.errors) == 2

    def test_defaults(self):
        r = SyncResult(success=True)
        assert r.records_synced == 0
        assert r.errors == []

    def test_negative_records_rejected(self):
        with pytest.raises(Exception):
            SyncResult(success=True, records_synced=-1)


class TestPushResult:
    def test_success(self):
        r = PushResult(success=True, external_id="FUL-456")
        assert r.external_id == "FUL-456"
        assert r.error is None

    def test_failure(self):
        r = PushResult(success=False, error="Order not found in ERP")
        assert r.success is False
        assert r.error == "Order not found in ERP"

    def test_defaults(self):
        r = PushResult(success=True)
        assert r.external_id is None
        assert r.error is None


class TestConnectionResult:
    def test_connected(self):
        r = ConnectionResult(connected=True, message="Connected as account 12345")
        assert r.connected is True

    def test_failed(self):
        r = ConnectionResult(connected=False, message="Invalid API key")
        assert r.connected is False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_complete_connector(self):
        """A fully implemented connector registers without errors."""
        reg = ConnectorRegistry()
        reg.register("test", CompleteConnector)
        assert "test" in reg.list_all()

    def test_register_not_a_subclass(self):
        """Registering a class that doesn't extend BaseConnector raises TypeError."""
        reg = ConnectorRegistry()
        with pytest.raises(TypeError):
            reg.register("bad", dict)

    def test_register_missing_methods(self):
        """A connector with unimplemented abstract methods raises TypeError at registration."""

        class IncompleteConnector(BaseConnector):
            def sync_orders(self, since):
                return SyncResult(success=True)

            # Missing: sync_items, sync_inventory, push_fulfillment,
            #          test_connection, get_config_schema, get_capabilities

        reg = ConnectorRegistry()
        with pytest.raises(TypeError, match="missing required methods"):
            reg.register("incomplete", IncompleteConnector)

    def test_register_not_a_class(self):
        """Registering a non-class object raises TypeError."""
        reg = ConnectorRegistry()
        with pytest.raises(TypeError):
            reg.register("instance", CompleteConnector(config={}))


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_get_registered(self):
        reg = ConnectorRegistry()
        reg.register("test", CompleteConnector)
        assert reg.get("test") is CompleteConnector

    def test_get_missing_raises(self):
        reg = ConnectorRegistry()
        with pytest.raises(KeyError, match="nope"):
            reg.get("nope")

    def test_list_all_returns_copy(self):
        reg = ConnectorRegistry()
        reg.register("a", CompleteConnector)
        all_connectors = reg.list_all()
        assert "a" in all_connectors
        # Mutating the returned dict should not affect the registry
        all_connectors.pop("a")
        assert "a" in reg.list_all()

    def test_list_all_empty(self):
        reg = ConnectorRegistry()
        assert reg.list_all() == {}

    def test_discover_does_not_crash(self):
        """discover() should run without errors even with no connector modules."""
        reg = ConnectorRegistry()
        reg.discover()
        # Example connector is excluded from auto-discovery, so registry stays empty
        # (unless other connector modules exist in the directory)

    def test_register_overwrites(self):
        """Registering the same name twice replaces the previous connector."""
        reg = ConnectorRegistry()
        reg.register("test", CompleteConnector)
        reg.register("test", CompleteConnector)
        assert reg.get("test") is CompleteConnector


# ---------------------------------------------------------------------------
# Example connector
# ---------------------------------------------------------------------------


class TestExampleConnector:
    def test_implements_full_interface(self):
        """ExampleConnector can be instantiated and all methods work."""
        conn = ExampleConnector(config={"api_key": "test", "base_url": "http://example.com"})
        now = datetime.now(timezone.utc)

        orders = conn.sync_orders(now)
        assert isinstance(orders, SyncResult)
        assert orders.success is True

        items = conn.sync_items(now)
        assert isinstance(items, SyncResult)

        inventory = conn.sync_inventory(now)
        assert isinstance(inventory, SyncResult)

        fulfillment = conn.push_fulfillment("ORD-1", "TRACK-1", "UPS")
        assert isinstance(fulfillment, PushResult)
        assert fulfillment.success is True

        connection = conn.test_connection()
        assert isinstance(connection, ConnectionResult)
        assert connection.connected is True

    def test_config_schema(self):
        """Config schema returns expected fields."""
        conn = ExampleConnector(config={})
        schema = conn.get_config_schema()
        assert "api_key" in schema
        assert "base_url" in schema
        assert schema["api_key"]["required"] is True

    def test_capabilities(self):
        """Capabilities list includes all four operations."""
        conn = ExampleConnector(config={})
        caps = conn.get_capabilities()
        assert "sync_orders" in caps
        assert "sync_items" in caps
        assert "sync_inventory" in caps
        assert "push_fulfillment" in caps

    def test_registers_successfully(self):
        """ExampleConnector can be registered (even though it isn't by default)."""
        reg = ConnectorRegistry()
        reg.register("example", ExampleConnector)
        assert reg.get("example") is ExampleConnector

    def test_stores_config(self):
        """Config dict is accessible via self.config."""
        config = {"api_key": "abc", "base_url": "https://api.test.com"}
        conn = ExampleConnector(config=config)
        assert conn.config["api_key"] == "abc"
        assert conn.config["base_url"] == "https://api.test.com"
