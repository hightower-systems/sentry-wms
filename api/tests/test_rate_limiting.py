"""
V-041: rate limiting on sensitive admin endpoints and global default.

Uses Flask-Limiter's in-memory backend via CELERY_BROKER_URL being
absent in the test env. The module-level limiter is reset between
tests via the autouse fixture to keep counters isolated.
"""

import pytest

from services.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_limiter_storage():
    # Clear counters between tests so one test's quota consumption does
    # not leak into the next. Both the memory and Redis storage backends
    # expose a reset() method in flask-limiter 3.x.
    try:
        limiter._storage.reset()
    except Exception:
        pass
    yield


class TestPerRouteLimits:
    def test_connector_test_has_tighter_limit(self, client, auth_headers):
        # 10/minute budget. Hit it 11 times -> final call must be 429.
        # test_connection returns 404 on unknown connector; we only need
        # the request to reach the limiter gate, which runs before the
        # route body.
        last_status = None
        for _ in range(11):
            resp = client.post(
                "/api/admin/connectors/nonexistent_xyz/test",
                json={"warehouse_id": 1},
                headers=auth_headers,
            )
            last_status = resp.status_code
        assert last_status == 429, (
            f"11th call to /connectors/.../test should be rate limited; "
            f"got {last_status}"
        )

    def test_save_credentials_has_tighter_limit(self, client, auth_headers):
        last_status = None
        for _ in range(11):
            resp = client.post(
                "/api/admin/connectors/nonexistent_xyz/credentials",
                json={"warehouse_id": 1, "credentials": {"k": "v"}},
                headers=auth_headers,
            )
            last_status = resp.status_code
        assert last_status == 429

    def test_trigger_sync_has_limit(self, client, auth_headers):
        last_status = None
        for _ in range(21):
            resp = client.post(
                "/api/admin/connectors/nonexistent_xyz/sync/orders",
                json={"warehouse_id": 1},
                headers=auth_headers,
            )
            last_status = resp.status_code
        assert last_status == 429


class TestRateLimitKeyIsolation:
    def test_authenticated_user_isolated_from_ip_bucket(self, client, auth_headers):
        # Burn the per-user quota on /test.
        for _ in range(11):
            client.post(
                "/api/admin/connectors/nonexistent_xyz/test",
                json={"warehouse_id": 1},
                headers=auth_headers,
            )
        # A request without auth_headers uses a different key (the IP
        # bucket). It should NOT be rate limited by the user bucket -
        # it will be rejected for auth reasons (401), not 429.
        resp = client.post(
            "/api/admin/connectors/nonexistent_xyz/test",
            json={"warehouse_id": 1},
        )
        assert resp.status_code != 429


class TestLimiterConfig:
    def test_default_limits_registered(self):
        # Global default is 300/minute. Verified from the source constant so
        # the regression catches an accidental deletion/downgrade of the
        # default.
        from services.rate_limit import DEFAULT_LIMITS
        assert DEFAULT_LIMITS == ["300 per minute"]

    def test_limiter_has_health_without_limit(self, client):
        # /api/health is not an authenticated route and should not hit
        # a 429 at normal pace. Sanity check that the default limit is
        # loose enough for health polling.
        for _ in range(20):
            resp = client.get("/api/health")
            assert resp.status_code == 200
