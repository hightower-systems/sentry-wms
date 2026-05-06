"""Cross-direction + per-resource scope tests for @require_wms_token (v1.7.0).

The decorator's v1.5.1 endpoint-slug enforcement is unchanged for the
outbound surface; v1.7 layers on:

- Inbound POST routes (Flask endpoint in V170_INBOUND_RESOURCE_BY_ENDPOINT
  OR path prefix /api/v1/inbound/) require both source_system and
  inbound_resources on the token. Empty array = deny all (Decision-S).
- An outbound-only token (no source_system, no inbound_resources)
  hitting an inbound route → 403 cross_direction_scope_violation.
- An inbound-only token (has inbound_resources, no event_types)
  hitting an outbound route → 403 cross_direction_scope_violation.
- An inbound token whose inbound_resources array does not list the
  target resource → 403 inbound_resource_scope_violation.

Probe-app fixture follows the same shape as test_wms_token_decorator.py:
register both an inbound and an outbound endpoint under their real
Flask endpoint names; the decorator's path-or-endpoint dispatch
recognises both.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uuid as _uuid

import pytest
from flask import Flask, g, jsonify

from _wms_token_helpers import delete_token, insert_token
from middleware.auth_middleware import require_wms_token
from services import token_cache


def _fresh_source_system() -> str:
    return f"scope-test-{_uuid.uuid4().hex[:8]}"


@pytest.fixture
def probe_app():
    """Two probe routes under real production Flask endpoint names so
    the decorator's path-or-endpoint dispatch routes correctly:

    - /probe-inbound -> endpoint inbound.post_sales_orders (treated
      as the /api/v1/inbound/sales_orders surface).
    - /probe-outbound -> endpoint polling.poll_events (treated as
      the /api/v1/events surface)."""
    app = Flask("test-wms-inbound-scope")

    @app.route("/probe-inbound", endpoint="inbound.post_sales_orders", methods=["POST"])
    @require_wms_token
    def probe_inbound():
        return jsonify(
            {
                "token_id": g.current_token["token_id"],
                "source_system": g.current_token["source_system"],
                "inbound_resources": g.current_token["inbound_resources"],
            }
        )

    @app.route("/probe-outbound", endpoint="polling.poll_events")
    @require_wms_token
    def probe_outbound():
        return jsonify({"token_id": g.current_token["token_id"]})

    return app.test_client()


@pytest.fixture(autouse=True)
def _clear_cache_and_scope_allowlist():
    """Wipe the token cache and any leftover scope-test-* allowlist rows.
    The insert_token helper auto-INSERTs an allowlist row whenever a
    source_system is supplied (so the FK is satisfied), but no symmetric
    delete exists -- those rows would leak into the next file's tests
    (e.g., the mapping-loader boot cross-check that asserts every
    allowlisted source_system has a matching doc on disk).
    """
    import psycopg2 as _pg
    from _wms_token_helpers import DATABASE_URL as _DB

    def _wipe_scope():
        c = _pg.connect(_DB)
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "DELETE FROM inbound_source_systems_allowlist "
            " WHERE source_system LIKE 'scope-test-%'"
        )
        c.close()

    token_cache.clear()
    _wipe_scope()
    yield
    token_cache.clear()
    _wipe_scope()


# ----------------------------------------------------------------------
# Cross-direction
# ----------------------------------------------------------------------


class TestCrossDirectionInboundReject:
    def test_outbound_only_token_rejected_at_inbound(self, probe_app):
        """Outbound token (event_types set, no source_system, empty
        inbound_resources) hitting an inbound route -> 403
        cross_direction_scope_violation."""
        token_id = insert_token(
            plaintext="outbound-only",
            event_types=["receipt.completed"],
        )
        try:
            resp = probe_app.post(
                "/probe-inbound", headers={"X-WMS-Token": "outbound-only"}
            )
            assert resp.status_code == 403
            assert resp.get_json() == {"error": "cross_direction_scope_violation"}
        finally:
            delete_token(token_id)

    def test_inbound_token_missing_source_system_rejected(self, probe_app):
        """An admin-malformed token: inbound_resources set but
        source_system NULL. The decorator refuses without leaking
        which dimension was missing."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="missing-source",
            source_system=None,
            inbound_resources=["sales_orders"],
        )
        try:
            resp = probe_app.post(
                "/probe-inbound", headers={"X-WMS-Token": "missing-source"}
            )
            assert resp.status_code == 403
            assert resp.get_json() == {"error": "cross_direction_scope_violation"}
        finally:
            delete_token(token_id)

    def test_inbound_token_empty_resources_rejected(self, probe_app):
        """source_system set but inbound_resources = [] (Decision-S
        empty = deny all)."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="empty-resources",
            source_system=ss,
            inbound_resources=[],
        )
        try:
            resp = probe_app.post(
                "/probe-inbound", headers={"X-WMS-Token": "empty-resources"}
            )
            assert resp.status_code == 403
            assert resp.get_json() == {"error": "cross_direction_scope_violation"}
        finally:
            delete_token(token_id)


class TestCrossDirectionOutboundReject:
    def test_inbound_only_token_rejected_at_outbound(self, probe_app):
        """Inbound-only token (has inbound_resources, no event_types)
        hitting an outbound route -> 403."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="inbound-only",
            source_system=ss,
            inbound_resources=["sales_orders"],
            event_types=[],
        )
        try:
            resp = probe_app.get(
                "/probe-outbound", headers={"X-WMS-Token": "inbound-only"}
            )
            assert resp.status_code == 403
            assert resp.get_json() == {"error": "cross_direction_scope_violation"}
        finally:
            delete_token(token_id)

    def test_token_with_both_directions_passes_outbound(self, probe_app):
        """A connector-framework-style token that opts into both
        directions still passes the outbound surface."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="both-dirs",
            source_system=ss,
            inbound_resources=["sales_orders"],
            event_types=["receipt.completed"],
        )
        try:
            resp = probe_app.get(
                "/probe-outbound", headers={"X-WMS-Token": "both-dirs"}
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Per-resource scope
# ----------------------------------------------------------------------


class TestInboundResourceScope:
    def test_resource_in_array_passes(self, probe_app):
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="has-sales-orders",
            source_system=ss,
            inbound_resources=["sales_orders"],
        )
        try:
            resp = probe_app.post(
                "/probe-inbound", headers={"X-WMS-Token": "has-sales-orders"}
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["source_system"] == ss
            assert body["inbound_resources"] == ["sales_orders"]
        finally:
            delete_token(token_id)

    def test_resource_missing_returns_inbound_resource_scope_violation(self, probe_app):
        """Token's inbound_resources lists 'items' but the route is
        /api/v1/inbound/sales_orders -> 403."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="only-items",
            source_system=ss,
            inbound_resources=["items"],
        )
        try:
            resp = probe_app.post(
                "/probe-inbound", headers={"X-WMS-Token": "only-items"}
            )
            assert resp.status_code == 403
            assert resp.get_json() == {"error": "inbound_resource_scope_violation"}
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Token cache surface
# ----------------------------------------------------------------------


class TestTokenCacheCarriesV17Columns:
    def test_source_system_inbound_resources_mapping_override_in_cache(self):
        """The decorator + handlers read these columns from the cached
        dict; this test pins the column-set in token_cache._fetch_by_hash."""
        from middleware.auth_middleware import _hash_token
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="cache-shape",
            source_system=ss,
            inbound_resources=["sales_orders", "items"],
            mapping_override=True,
        )
        try:
            row = token_cache.get_by_hash(_hash_token("cache-shape"))
            assert row is not None
            assert row["source_system"] == ss
            assert row["inbound_resources"] == ["sales_orders", "items"]
            assert row["mapping_override"] is True
        finally:
            delete_token(token_id)
