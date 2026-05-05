"""Admin /api/admin/tokens v1.7.0 inbound surface tests.

Covers the schema + handler extensions for Pipe B inbound tokens:

- /admin/scope-catalog returns inbound_resources + source_systems
- POST creates an inbound-only token (endpoints empty,
  source_system + inbound_resources set, mapping_override flag)
- POST creates a both-directions token
- POST refuses an unknown source_system with the
  unknown_source_system body shape (pre-INSERT, before the FK
  fires)
- POST refuses inbound_resources without source_system
  (model_validator)
- POST refuses mapping_override on an outbound-only token
  (mapping_override implies inbound)
- GET listing surfaces source_system / inbound_resources /
  mapping_override per row
- DELETE captures the inbound columns into audit_log.details.previous_scope
"""

import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context


def _allowlist(ss: str) -> None:
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (ss,),
    )
    cur.close()


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


@pytest.fixture
def ss():
    return f"admintest-{uuid.uuid4().hex[:8]}"


# ----------------------------------------------------------------------
# Scope catalog surface
# ----------------------------------------------------------------------


class TestScopeCatalogInboundExtensions:
    def test_inbound_resources_lists_five_keys(self, client, auth_headers):
        resp = client.get("/api/admin/scope-catalog", headers=auth_headers)
        body = resp.get_json()
        # Plural keys: matches the resource-key dispatch
        # (V170_INBOUND_RESOURCE_BY_ENDPOINT.values()) and the
        # wms_tokens.inbound_resources array shape.
        assert set(body["inbound_resources"]) == {
            "sales_orders", "items", "customers", "vendors", "purchase_orders",
        }

    def test_source_systems_reflects_allowlist(self, client, auth_headers, ss):
        _allowlist(ss)
        resp = client.get("/api/admin/scope-catalog", headers=auth_headers)
        body = resp.get_json()
        labels = [r["source_system"] for r in body["source_systems"]]
        assert ss in labels


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------


def _post_create(client, auth_headers, payload):
    return client.post(
        "/api/admin/tokens",
        headers={**auth_headers, "Content-Type": "application/json"},
        data=json.dumps(payload),
    )


class TestCreateInboundToken:
    def test_inbound_only_token_with_no_endpoints(self, client, auth_headers, ss):
        _allowlist(ss)
        resp = _post_create(client, auth_headers, {
            "token_name": f"inbound-{ss}",
            "source_system": ss,
            "inbound_resources": ["sales_orders", "customers"],
            "mapping_override": False,
        })
        assert resp.status_code == 201
        body = resp.get_json()
        rows = _query(
            "SELECT source_system, inbound_resources, mapping_override, "
            "       endpoints "
            "  FROM wms_tokens WHERE token_id = %s",
            (body["token_id"],),
        )
        assert rows
        source_system, inbound_resources, mapping_override, endpoints = rows[0]
        assert source_system == ss
        assert set(inbound_resources) == {"sales_orders", "customers"}
        assert mapping_override is False
        assert list(endpoints) == []

    def test_both_directions_token(self, client, auth_headers, ss):
        _allowlist(ss)
        resp = _post_create(client, auth_headers, {
            "token_name": f"both-{ss}",
            "endpoints": ["events.poll"],
            "source_system": ss,
            "inbound_resources": ["sales_orders"],
        })
        assert resp.status_code == 201
        body = resp.get_json()
        rows = _query(
            "SELECT endpoints, source_system, inbound_resources "
            "  FROM wms_tokens WHERE token_id = %s",
            (body["token_id"],),
        )
        endpoints, source_system, inbound_resources = rows[0]
        assert "events.poll" in endpoints
        assert source_system == ss
        assert "sales_orders" in inbound_resources

    def test_unknown_source_system_returns_400(self, client, auth_headers):
        # Allowlist row absent: server rejects with the labelled error.
        resp = _post_create(client, auth_headers, {
            "token_name": "unknown-ss-token",
            "source_system": f"nope-{uuid.uuid4().hex[:6]}",
            "inbound_resources": ["sales_orders"],
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "unknown_source_system"
        assert "source_system" in body

    def test_inbound_resources_without_source_system_rejected(
        self, client, auth_headers
    ):
        # The model_validator may raise for the "must be set together"
        # rule OR for the "at least one direction" rule depending on
        # which check fires first; either way the response is 400 with
        # a validation_error body.
        resp = _post_create(client, auth_headers, {
            "token_name": "half-config",
            "inbound_resources": ["sales_orders"],
        })
        assert resp.status_code == 400
        body = resp.get_json()
        # validate_body raises 400 with error='validation_error' for
        # Pydantic failures; the message lives in details[0]['msg'].
        msgs = " ".join(d.get("msg", "") for d in body.get("details", []))
        assert (
            "source_system and inbound_resources must be set together" in msgs
            or "at least one direction" in msgs
        )

    def test_mapping_override_without_inbound_rejected(
        self, client, auth_headers
    ):
        resp = _post_create(client, auth_headers, {
            "token_name": "ov-no-inbound",
            "endpoints": ["events.poll"],
            "mapping_override": True,
        })
        assert resp.status_code == 400

    def test_outbound_only_unchanged(self, client, auth_headers):
        """v1.5 wire shape preserved: an outbound-only token (endpoints
        set, no inbound fields) still creates cleanly."""
        resp = _post_create(client, auth_headers, {
            "token_name": "outbound-only",
            "endpoints": ["events.poll", "events.ack"],
        })
        assert resp.status_code == 201

    def test_no_direction_set_rejected(self, client, auth_headers):
        """A token with neither endpoints nor inbound_resources is a
        no-op token; refuse rather than silently create dead weight."""
        resp = _post_create(client, auth_headers, {
            "token_name": "no-direction",
        })
        assert resp.status_code == 400


# ----------------------------------------------------------------------
# Listing surface
# ----------------------------------------------------------------------


class TestListingSurface:
    def test_listing_carries_inbound_columns(self, client, auth_headers, ss):
        _allowlist(ss)
        create = _post_create(client, auth_headers, {
            "token_name": f"listing-{ss}",
            "source_system": ss,
            "inbound_resources": ["items"],
            "mapping_override": True,
        })
        token_id = create.get_json()["token_id"]
        resp = client.get("/api/admin/tokens", headers=auth_headers)
        body = resp.get_json()
        row = next(r for r in body["tokens"] if r["token_id"] == token_id)
        assert row["source_system"] == ss
        assert row["inbound_resources"] == ["items"]
        assert row["mapping_override"] is True


# ----------------------------------------------------------------------
# Delete audit
# ----------------------------------------------------------------------


class TestDeleteAuditTrail:
    def test_delete_captures_inbound_columns(self, client, auth_headers, ss):
        _allowlist(ss)
        create = _post_create(client, auth_headers, {
            "token_name": f"delete-{ss}",
            "source_system": ss,
            "inbound_resources": ["vendors"],
            "mapping_override": False,
        })
        token_id = create.get_json()["token_id"]
        resp = client.delete(
            f"/api/admin/tokens/{token_id}", headers=auth_headers,
        )
        assert resp.status_code == 204

        rows = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type = 'WMS_TOKEN' AND entity_id = %s "
            "   AND action_type = 'TOKEN_DELETE' "
            " ORDER BY created_at DESC LIMIT 1",
            (token_id,),
        )
        assert rows
        details = rows[0][0]
        prev = details["previous_scope"]
        assert prev["source_system"] == ss
        assert prev["inbound_resources"] == ["vendors"]
        assert prev["mapping_override"] is False
