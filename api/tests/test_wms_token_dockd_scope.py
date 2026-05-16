"""Cross-direction + slug scope tests for @require_wms_token on the
v1.9.0 dockd surface.

Dockd is a third direction alongside outbound polling (V150) and
inbound POST (V170). A dockd token MUST carry the `dockd.dispatch`
slug in `endpoints` AND MUST NOT carry any outbound (event_types)
or inbound (source_system / inbound_resources) markers. Mixed-
direction tokens are explicitly rejected.

Probe-app fixture follows the same shape as
test_wms_token_inbound_scope.py: routes registered under real
production Flask endpoint names so the decorator's
path-or-endpoint dispatch routes correctly.
"""

import os
import sys
import uuid as _uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from flask import Flask, g, jsonify

from _wms_token_helpers import delete_token, insert_token
from middleware.auth_middleware import require_wms_token
from services import token_cache


def _fresh_source_system() -> str:
    return f"dockd-scope-test-{_uuid.uuid4().hex[:8]}"


@pytest.fixture
def probe_app():
    """Three probe routes registered under the real production dockd
    Flask endpoint names so the decorator routes correctly."""
    app = Flask("test-wms-dockd-scope")

    @app.route("/probe-dockd-get", endpoint="dockd.get_order")
    @require_wms_token
    def probe_get():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-dockd-ship", endpoint="dockd.ship_order", methods=["POST"])
    @require_wms_token
    def probe_ship():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-dockd-void", endpoint="dockd.void_ship", methods=["POST"])
    @require_wms_token
    def probe_void():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-outbound", endpoint="polling.poll_events")
    @require_wms_token
    def probe_outbound():
        return jsonify({"token_id": g.current_token["token_id"]})

    return app.test_client()


@pytest.fixture(autouse=True)
def _clear_cache_and_scope_allowlist():
    import psycopg2 as _pg
    from _wms_token_helpers import DATABASE_URL as _DB

    def _wipe_scope():
        c = _pg.connect(_DB)
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "DELETE FROM inbound_source_systems_allowlist "
            " WHERE source_system LIKE 'dockd-scope-test-%'"
        )
        c.close()

    token_cache.clear()
    _wipe_scope()
    yield
    token_cache.clear()
    _wipe_scope()


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestPureDockdTokenHappyPath:
    def test_pure_dockd_token_can_hit_get_order(self, probe_app):
        token_id = insert_token(
            plaintext="pure-dockd-get",
            endpoints=["dockd.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-dockd-get",
                headers={"X-WMS-Token": "pure-dockd-get"},
            )
            assert resp.status_code == 200
            assert resp.get_json()["token_id"] == token_id
        finally:
            delete_token(token_id)

    def test_pure_dockd_token_can_hit_ship_order(self, probe_app):
        token_id = insert_token(
            plaintext="pure-dockd-ship",
            endpoints=["dockd.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.post(
                "/probe-dockd-ship",
                headers={"X-WMS-Token": "pure-dockd-ship"},
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)

    def test_pure_dockd_token_can_hit_void_ship(self, probe_app):
        token_id = insert_token(
            plaintext="pure-dockd-void",
            endpoints=["dockd.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.post(
                "/probe-dockd-void",
                headers={"X-WMS-Token": "pure-dockd-void"},
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Cross-direction guards
# ----------------------------------------------------------------------


class TestCrossDirectionFromOutbound:
    def test_outbound_only_token_at_dockd_get_rejected(self, probe_app):
        """Token with event_types set hitting dockd surface -> 403
        cross_direction_scope_violation. Outbound polling tokens have
        no business on the dockd surface."""
        token_id = insert_token(
            plaintext="ob-only-at-dockd",
            endpoints=["dockd.dispatch"],
            event_types=["ship.confirmed"],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-dockd-get",
                headers={"X-WMS-Token": "ob-only-at-dockd"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cross_direction_scope_violation"
        finally:
            delete_token(token_id)


class TestCrossDirectionFromInbound:
    def test_inbound_only_token_at_dockd_ship_rejected(self, probe_app):
        """Token with source_system + inbound_resources set hitting
        dockd surface -> 403 cross_direction_scope_violation."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="ib-only-at-dockd",
            endpoints=["dockd.dispatch"],
            event_types=[],
            inbound_resources=["sales_orders"],
            source_system=ss,
        )
        try:
            resp = probe_app.post(
                "/probe-dockd-ship",
                headers={"X-WMS-Token": "ib-only-at-dockd"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cross_direction_scope_violation"
        finally:
            delete_token(token_id)

    def test_token_with_only_source_system_at_dockd_rejected(self, probe_app):
        """source_system without inbound_resources is unusual but the
        cross-direction check rejects ANY inbound marker on a dockd
        request, not just both-set."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="ss-only-at-dockd",
            endpoints=["dockd.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=ss,
        )
        try:
            resp = probe_app.post(
                "/probe-dockd-void",
                headers={"X-WMS-Token": "ss-only-at-dockd"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cross_direction_scope_violation"
        finally:
            delete_token(token_id)


class TestCrossDirectionFromDockd:
    def test_pure_dockd_token_at_outbound_rejected(self, probe_app):
        """A dockd-only token (event_types empty) hitting outbound
        polling -> 403 endpoint_scope_violation. The dockd token has
        no V150 slug in its endpoints array, so the existing V-200
        slug enforcement catches this without needing a new
        cross-direction error_kind."""
        token_id = insert_token(
            plaintext="dockd-only-at-outbound",
            endpoints=["dockd.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-outbound",
                headers={"X-WMS-Token": "dockd-only-at-outbound"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "endpoint_scope_violation"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Slug-scope guard
# ----------------------------------------------------------------------


class TestEndpointSlugScope:
    def test_token_without_dockd_dispatch_slug_rejected(self, probe_app):
        """Pure-direction token (no inbound / outbound markers) but
        endpoints list does NOT include dockd.dispatch -> 403
        endpoint_scope_violation."""
        token_id = insert_token(
            plaintext="no-slug-at-dockd",
            endpoints=[],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-dockd-get",
                headers={"X-WMS-Token": "no-slug-at-dockd"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "endpoint_scope_violation"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Auth failures still take precedence
# ----------------------------------------------------------------------


class TestAuthFailuresFirst:
    def test_missing_token_at_dockd_returns_invalid_token(self, probe_app):
        resp = probe_app.get("/probe-dockd-get")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid_token"

    def test_unknown_token_at_dockd_returns_invalid_token(self, probe_app):
        resp = probe_app.get(
            "/probe-dockd-get",
            headers={"X-WMS-Token": "never-issued"},
        )
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid_token"
