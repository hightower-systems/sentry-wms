"""Cross-direction + slug scope tests for @require_wms_token on the
v1.10.0 POS surface.

POS is a fourth direction alongside outbound polling (V150),
inbound POST (V170), and dockd (V190). A POS token MUST carry the
`pos.dispatch` slug in `endpoints` AND MUST NOT carry any outbound
(event_types) or inbound (source_system / inbound_resources)
markers. Mixed-direction tokens are explicitly rejected.

Probe-app fixture follows the same shape as
test_wms_token_dockd_scope.py: routes registered under real
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
    return f"pos-scope-test-{_uuid.uuid4().hex[:8]}"


@pytest.fixture
def probe_app():
    """Five probe routes: the four POS endpoints under their real
    production Flask endpoint names, plus an outbound probe so the
    "POS-only token at outbound" cross-direction case is exercisable.
    """
    app = Flask("test-wms-pos-scope")

    @app.route("/probe-pos-availability", endpoint="pos.availability")
    @require_wms_token
    def probe_availability():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-pos-validate-cart", endpoint="pos.validate_cart", methods=["POST"])
    @require_wms_token
    def probe_validate_cart():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-pos-checkout", endpoint="pos.checkout", methods=["POST"])
    @require_wms_token
    def probe_checkout():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-pos-refund", endpoint="pos.refund", methods=["POST"])
    @require_wms_token
    def probe_refund():
        return jsonify({"token_id": g.current_token["token_id"]})

    @app.route("/probe-outbound", endpoint="polling.poll_events")
    @require_wms_token
    def probe_outbound():
        return jsonify({"token_id": g.current_token["token_id"]})

    # An /api/v1/pos/* path bound to a NON-POS Flask endpoint so the
    # path-prefix-only fail-closed branch is exercisable. The test
    # body sends the POS token, the path matches /api/v1/pos/, and the
    # request.endpoint resolves to "anything.else" -> the decorator
    # must refuse with endpoint_scope_violation.
    @app.route("/api/v1/pos/unmapped", endpoint="anything.else")
    @require_wms_token
    def probe_unmapped():
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
            " WHERE source_system LIKE 'pos-scope-test-%'"
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


class TestPurePosTokenHappyPath:
    def test_pure_pos_token_can_hit_availability(self, probe_app):
        token_id = insert_token(
            plaintext="pure-pos-availability",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-pos-availability",
                headers={"X-WMS-Token": "pure-pos-availability"},
            )
            assert resp.status_code == 200
            assert resp.get_json()["token_id"] == token_id
        finally:
            delete_token(token_id)

    def test_pure_pos_token_can_hit_validate_cart(self, probe_app):
        token_id = insert_token(
            plaintext="pure-pos-validate-cart",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.post(
                "/probe-pos-validate-cart",
                headers={"X-WMS-Token": "pure-pos-validate-cart"},
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)

    def test_pure_pos_token_can_hit_checkout(self, probe_app):
        token_id = insert_token(
            plaintext="pure-pos-checkout",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.post(
                "/probe-pos-checkout",
                headers={"X-WMS-Token": "pure-pos-checkout"},
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)

    def test_pure_pos_token_can_hit_refund(self, probe_app):
        token_id = insert_token(
            plaintext="pure-pos-refund",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.post(
                "/probe-pos-refund",
                headers={"X-WMS-Token": "pure-pos-refund"},
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Cross-direction guards
# ----------------------------------------------------------------------


class TestCrossDirectionFromOutbound:
    def test_outbound_only_token_at_pos_availability_rejected(self, probe_app):
        """Token with event_types set hitting POS surface -> 403
        cross_direction_scope_violation. Outbound polling tokens have
        no business on the POS surface."""
        token_id = insert_token(
            plaintext="ob-only-at-pos",
            endpoints=["pos.dispatch"],
            event_types=["ship.confirmed"],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-pos-availability",
                headers={"X-WMS-Token": "ob-only-at-pos"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cross_direction_scope_violation"
        finally:
            delete_token(token_id)


class TestCrossDirectionFromInbound:
    def test_inbound_only_token_at_pos_checkout_rejected(self, probe_app):
        """Token with source_system + inbound_resources set hitting
        POS surface -> 403 cross_direction_scope_violation."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="ib-only-at-pos",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=["sales_orders"],
            source_system=ss,
        )
        try:
            resp = probe_app.post(
                "/probe-pos-checkout",
                headers={"X-WMS-Token": "ib-only-at-pos"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cross_direction_scope_violation"
        finally:
            delete_token(token_id)

    def test_token_with_only_source_system_at_pos_rejected(self, probe_app):
        """source_system without inbound_resources is unusual but the
        cross-direction check rejects ANY inbound marker on a POS
        request, not just both-set."""
        ss = _fresh_source_system()
        token_id = insert_token(
            plaintext="ss-only-at-pos",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=ss,
        )
        try:
            resp = probe_app.post(
                "/probe-pos-refund",
                headers={"X-WMS-Token": "ss-only-at-pos"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cross_direction_scope_violation"
        finally:
            delete_token(token_id)


class TestCrossDirectionFromPos:
    def test_pure_pos_token_at_outbound_rejected(self, probe_app):
        """A POS-only token (event_types empty) hitting outbound
        polling -> 403 endpoint_scope_violation. The POS token has no
        V150 slug in its endpoints array, so the existing V-200 slug
        enforcement catches this without needing a new cross-direction
        error_kind."""
        token_id = insert_token(
            plaintext="pos-only-at-outbound",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-outbound",
                headers={"X-WMS-Token": "pos-only-at-outbound"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "endpoint_scope_violation"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Slug-scope guard
# ----------------------------------------------------------------------


class TestEndpointSlugScope:
    def test_token_without_pos_dispatch_slug_rejected(self, probe_app):
        """Pure-direction token (no inbound / outbound markers) but
        endpoints list does NOT include pos.dispatch -> 403
        endpoint_scope_violation."""
        token_id = insert_token(
            plaintext="no-slug-at-pos",
            endpoints=[],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/probe-pos-availability",
                headers={"X-WMS-Token": "no-slug-at-pos"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "endpoint_scope_violation"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Fail-closed wiring-bug guard
# ----------------------------------------------------------------------


class TestPathPrefixWithoutFlaskEndpointClaim:
    def test_pos_path_prefix_without_endpoint_claim_rejected(self, probe_app):
        """A request whose path matches /api/v1/pos/ but whose Flask
        endpoint name is NOT in _V1100_POS_FLASK_ENDPOINTS fails closed
        with endpoint_scope_violation. Catches the wiring bug where a
        new POS route lands under @require_wms_token but its endpoint
        name was not added to the frozenset."""
        token_id = insert_token(
            plaintext="pos-token-at-unmapped",
            endpoints=["pos.dispatch"],
            event_types=[],
            inbound_resources=[],
            source_system=None,
        )
        try:
            resp = probe_app.get(
                "/api/v1/pos/unmapped",
                headers={"X-WMS-Token": "pos-token-at-unmapped"},
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "endpoint_scope_violation"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Auth failures still take precedence
# ----------------------------------------------------------------------


class TestAuthFailuresFirst:
    def test_missing_token_at_pos_returns_invalid_token(self, probe_app):
        resp = probe_app.get("/probe-pos-availability")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid_token"

    def test_unknown_token_at_pos_returns_invalid_token(self, probe_app):
        resp = probe_app.get(
            "/probe-pos-availability",
            headers={"X-WMS-Token": "never-issued"},
        )
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid_token"
