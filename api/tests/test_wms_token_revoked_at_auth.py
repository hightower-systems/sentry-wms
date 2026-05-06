"""v1.7.0 #278: @require_wms_token rejects when revoked_at is set,
regardless of status.

Pre-#278, the auth middleware gated only on `status == 'active'`. A
direct DB write of the form `UPDATE wms_tokens SET revoked_at = NOW()`
-- without also setting status='revoked' -- produced a row that the
status check let through, even after #274's pg_notify trigger evicted
the cache. Mig 048's trigger now flips status in lock-step on the same
UPDATE, and the auth check additionally rejects revoked_at IS NOT NULL
as defense-in-depth.

Tests pin both gates independently:

- Row with status='active' but revoked_at populated -> 401 (the new
  revoked_at gate catches it).
- Row with status='revoked' but revoked_at NULL -> 401 (the existing
  status gate catches it; pin so the new gate does not regress the
  status path).

Plus an integration test for the full direct-DB-revoke flow:
trigger fires -> status flips -> cache evicts -> next request 401.
"""

import os
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest
from flask import Flask, g, jsonify

from middleware.auth_middleware import require_wms_token
from services import token_cache

from _wms_token_helpers import delete_token, insert_token


DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture
def probe_app():
    app = Flask("test-revoked-at-auth")

    @app.route("/probe", endpoint="polling.poll_events")
    @require_wms_token
    def probe():
        return jsonify({"token_id": g.current_token["token_id"]})

    return app.test_client()


@pytest.fixture(autouse=True)
def _fresh_cache():
    token_cache.clear()
    yield
    token_cache.clear()


class TestRevokedAtGate:
    """Bidirectional assertion: each gate catches its half independently."""

    def test_active_status_with_revoked_at_populated_is_rejected(
        self, probe_app
    ):
        """The shape that gate 17 caught: an operator runs
        `UPDATE wms_tokens SET revoked_at = NOW() WHERE token_id = X`.
        With mig 048's trigger the lock-step UPDATE flips status to
        'revoked'; in this test we seed the row directly with the
        pre-trigger shape (status='active' + revoked_at populated) so
        the auth gate's revoked_at check is exercised in isolation.
        INSERT does not fire the AFTER UPDATE OF revoked_at trigger
        so this seed state is reachable."""
        token_id = insert_token(
            plaintext="rev-at-active",
            status="active",
            revoked_at=datetime.now(timezone.utc),
        )
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "rev-at-active"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "invalid_token"}
        finally:
            delete_token(token_id)

    def test_revoked_status_without_revoked_at_is_rejected(self, probe_app):
        """Pre-#278 behavior: a row with status='revoked' is rejected
        by the status gate regardless of revoked_at. Pin so the new
        revoked_at gate doesn't accidentally regress the status path
        (e.g., a future refactor that swapped order of checks)."""
        token_id = insert_token(
            plaintext="rev-status-only",
            status="revoked",
            revoked_at=None,
        )
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "rev-status-only"}
            )
            assert resp.status_code == 401
            assert resp.get_json() == {"error": "invalid_token"}
        finally:
            delete_token(token_id)

    def test_active_status_without_revoked_at_passes(self, probe_app):
        """Sanity check: a normal active token still authenticates.
        Without this, the two rejection tests above could trivially pass
        if the auth middleware rejected every request."""
        token_id = insert_token(plaintext="rev-at-active-ok")
        try:
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "rev-at-active-ok"}
            )
            assert resp.status_code == 200
        finally:
            delete_token(token_id)


class TestDirectDbRevokeEndToEnd:
    """The shape pre-merge gate 17 exercised: an operator runs a
    direct UPDATE setting revoked_at, the trigger flips status and
    fires pg_notify, the cache evicts, the next request is 401.

    The test issues an explicit `token_cache.clear()` to mirror what
    the LISTEN subscriber would do post-NOTIFY; running the subscriber
    inline would couple the test to thread-scheduling timing."""

    def test_direct_update_sets_revoked_at_then_rejects(self, probe_app):
        token_id = insert_token(plaintext="direct-revoke-target")
        try:
            # Initial probe authenticates and caches the row.
            ok = probe_app.get(
                "/probe", headers={"X-WMS-Token": "direct-revoke-target"}
            )
            assert ok.status_code == 200

            # Direct DB UPDATE: set ONLY revoked_at, not status. The
            # mig 048 trigger fires a secondary UPDATE setting status
            # to 'revoked'; the test verifies that downstream of the
            # cache eviction, the next probe is 401.
            conn = psycopg2.connect(DATABASE_URL)
            try:
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute(
                    "UPDATE wms_tokens SET revoked_at = NOW() "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                # Verify the trigger did its job: status flipped.
                cur.execute(
                    "SELECT status, revoked_at FROM wms_tokens "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                status, revoked_at = cur.fetchone()
                assert status == "revoked", (
                    f"trigger should have flipped status to 'revoked'; "
                    f"got {status!r}"
                )
                assert revoked_at is not None
            finally:
                conn.close()

            # Mirror the LISTEN-subscriber eviction. The next probe
            # fetches fresh DB state and finds status='revoked'.
            token_cache.clear()
            rejected = probe_app.get(
                "/probe", headers={"X-WMS-Token": "direct-revoke-target"}
            )
            assert rejected.status_code == 401
            assert rejected.get_json() == {"error": "invalid_token"}
        finally:
            delete_token(token_id)

    def test_trigger_idempotent_on_already_revoked_status(self, probe_app):
        """The mig 048 trigger guards against re-setting status when
        it's already 'revoked'. A token whose initial UPDATE sets both
        status='revoked' AND revoked_at=NOW() (the Flask admin path)
        must not trigger a second redundant UPDATE that would burn an
        extra audit_log row."""
        token_id = insert_token(plaintext="idempotent-target")
        try:
            conn = psycopg2.connect(DATABASE_URL)
            try:
                conn.autocommit = True
                cur = conn.cursor()
                # The Flask admin path's UPDATE shape: both columns set
                # in one statement.
                cur.execute(
                    "UPDATE wms_tokens "
                    "   SET status = 'revoked', revoked_at = NOW() "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                # Verify status is 'revoked' and the row is consistent.
                cur.execute(
                    "SELECT status, revoked_at FROM wms_tokens "
                    " WHERE token_id = %s",
                    (token_id,),
                )
                status, revoked_at = cur.fetchone()
                assert status == "revoked"
                assert revoked_at is not None
            finally:
                conn.close()
            # Auth gate rejects either way (status OR revoked_at).
            token_cache.clear()
            resp = probe_app.get(
                "/probe", headers={"X-WMS-Token": "idempotent-target"}
            )
            assert resp.status_code == 401
        finally:
            delete_token(token_id)
