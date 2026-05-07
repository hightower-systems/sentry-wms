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
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")

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


class TestConnectionMessageSanitization:
    """V-014: ConnectionResult.message must be length-capped and stripped
    of non-printable bytes so a misbehaving upstream cannot smuggle huge
    response bodies or control-character payloads back through the
    admin UI."""

    def test_truncates_long_message(self):
        from connectors.base import CONNECTION_MESSAGE_MAX_LEN

        raw = "A" * 10_000
        r = ConnectionResult(connected=True, message=raw)
        assert len(r.message) <= CONNECTION_MESSAGE_MAX_LEN
        assert r.message.endswith("...")

    def test_strips_control_characters(self):
        # Null bytes, bell, escape, BEL, FF etc. must be removed.
        raw = "ok\x00\x07\x1b[31mred\x1b[0m\x0cdone"
        r = ConnectionResult(connected=True, message=raw)
        assert "\x00" not in r.message
        assert "\x07" not in r.message
        assert "\x1b" not in r.message
        assert "\x0c" not in r.message
        # Visible content is preserved.
        assert "ok" in r.message
        assert "red" in r.message
        assert "done" in r.message

    def test_strips_non_ascii(self):
        raw = "connected to Acm\u00e9 Corp  \u2620"
        r = ConnectionResult(connected=True, message=raw)
        # Non-ASCII dropped but ASCII preserved.
        assert "\u00e9" not in r.message
        assert "\u2620" not in r.message
        assert "connected to" in r.message
        assert "Corp" in r.message

    def test_keeps_whitespace(self):
        # Tab, LF, CR are legitimate in multi-line status messages.
        raw = "line1\nline2\tindented\r\n"
        r = ConnectionResult(connected=True, message=raw)
        assert "\n" in r.message
        assert "\t" in r.message

    def test_message_field_is_capped_even_when_short(self):
        r = ConnectionResult(connected=True, message="ok")
        assert r.message == "ok"


class TestConnectionMessageScrubbing:
    """v1.8.0 (#52, #53): ConnectionResult.message runs through
    scrub_secrets at construction. Credentials in raw upstream errors
    are redacted before the value reaches the admin UI or the
    sync_state.last_error_message column."""

    def test_url_userinfo_scrubbed(self):
        r = ConnectionResult(
            connected=False,
            message="connect failed: https://alice:s3cret@erp.example.com/api",
        )
        assert "alice" not in r.message
        assert "s3cret" not in r.message
        assert "https://erp.example.com/api" in r.message

    def test_bearer_token_scrubbed(self):
        r = ConnectionResult(
            connected=False,
            message="401 from upstream Bearer abcdefghijklmnopqrstuvwxyz0123",
        )
        assert "abcdefghijklmnopqrstuvwxyz0123" not in r.message
        assert "Bearer <REDACTED>" in r.message

    def test_kv_password_scrubbed(self):
        r = ConnectionResult(
            connected=False,
            message="connection rejected: password=hunter2-very-secret",
        )
        assert "hunter2-very-secret" not in r.message
        assert "password=<REDACTED>" in r.message

    def test_aws_access_key_scrubbed(self):
        r = ConnectionResult(
            connected=False,
            message="S3 putObject denied for AKIAIOSFODNN7EXAMPLE",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in r.message
        assert "AKIA<REDACTED>" in r.message

    def test_jwt_scrubbed(self):
        r = ConnectionResult(
            connected=False,
            message="rejected: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature",
        )
        assert "eyJhbGciOiJIUzI1NiJ9" not in r.message
        assert "<JWT_REDACTED>" in r.message

    def test_redaction_tag_not_truncated_at_length_cap(self):
        # Pad so the credential lands across the cap boundary; without
        # scrub-before-truncate the redaction tag would be split.
        from connectors.base import CONNECTION_MESSAGE_MAX_LEN

        prefix = "x" * (CONNECTION_MESSAGE_MAX_LEN - 50)
        raw = f"{prefix} api_key=ABCDEFGHIJ1234567890LMNOPQR"
        r = ConnectionResult(connected=False, message=raw)
        assert "ABCDEFGHIJ1234567890LMNOPQR" not in r.message
        # Either the redaction tag survives intact, or the credential
        # was truncated away entirely. Never a partial tag.
        assert "<REDACT" not in r.message or "<REDACTED>" in r.message

    def test_plain_message_passes_through_unchanged(self):
        r = ConnectionResult(connected=True, message="Connected as Acme Corp")
        assert r.message == "Connected as Acme Corp"


class TestCarriageReturnAllowlist:
    """v1.8.0 (#55): \\r is permitted in ConnectionResult.message so
    Windows-origin upstream errors (which use \\r\\n line endings)
    survive intact. Safety on emit is guaranteed by JSON encoding,
    which escapes \\r to the two-character sequence \\\\r."""

    def test_cr_preserved_in_stored_message(self):
        r = ConnectionResult(connected=False, message="line1\r\nline2")
        assert "\r" in r.message
        assert "\n" in r.message
        assert "line1" in r.message
        assert "line2" in r.message

    def test_cr_escaped_on_json_emit(self):
        # Pydantic / json.dumps escapes the raw CR byte to "\\r" so a
        # message that reaches the admin UI cannot inject a literal
        # carriage return into a log line or response body.
        r = ConnectionResult(connected=False, message="line1\r\nline2")
        serialized = r.model_dump_json()
        assert "\\r" in serialized
        assert "\\n" in serialized
        # Raw CR/LF must not appear unescaped in the JSON payload.
        assert "\r" not in serialized
        assert "\n" not in serialized

    def test_lone_cr_preserved(self):
        # Mac-classic line endings or progress-bar-style overwrites:
        # the byte survives storage; emit is safe by JSON escaping.
        r = ConnectionResult(connected=True, message="loaded\rdone")
        assert "\r" in r.message


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

    def test_register_duplicate_name_raises(self):
        """V-010: registering the same name twice must raise, not overwrite.

        Silent overwrite was a supply-chain foothold -- a second import of
        a malicious module under the same name would win.
        """
        reg = ConnectorRegistry()
        reg.register("test", CompleteConnector)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("test", CompleteConnector)
        # Original registration stays intact
        assert reg.get("test") is CompleteConnector

    def test_register_duplicate_name_rejects_different_class(self):
        """V-010: duplicate check applies even when the second class differs."""
        class AnotherConnector(CompleteConnector):
            pass

        reg = ConnectorRegistry()
        reg.register("netsuite", CompleteConnector)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("netsuite", AnotherConnector)
        assert reg.get("netsuite") is CompleteConnector


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
