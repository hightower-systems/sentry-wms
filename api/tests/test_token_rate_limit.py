"""X-WMS-Token rate-limit bucket isolation (v1.5.0 #130).

Two layers:

1. Key-function unit tests prove the ``_rate_limit_key`` preference
   chain: ``token:<id>`` > ``user:<id>`` > ``ip:<addr>``.

2. End-to-end tests against the real ``/api/v1/snapshot/inventory``
   endpoint (2 req/min per token) prove bucket isolation: two tokens
   have independent budgets, and the snapshot route and the polling
   route do not share a bucket for the same token (flask-limiter
   counts per ``(key, route)`` tuple).

The cookie-auth vs token-auth isolation is proven at the key-function
level; cookie-auth users cannot actually reach ``/api/v1/*`` (all
routes are gated by ``@require_wms_token``), so a cross-scheme
same-route integration test is structurally impossible.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from flask import Flask, g

from _wms_token_helpers import insert_token
from services import token_cache
from services.rate_limit import _rate_limit_key, limiter


@pytest.fixture(autouse=True)
def _reset_limiter_and_cache():
    # Reset in-process state only. We deliberately do NOT TRUNCATE
    # wms_tokens between tests: the fixture's outer transaction holds
    # row locks on rows the handler just INSERTed, and a separate
    # autocommit DELETE would block forever waiting for the fixture
    # teardown. Cross-test uniqueness is supplied by uuid4 in each
    # test's plaintext.
    try:
        limiter._storage.reset()
    except Exception:
        pass
    token_cache.clear()
    yield
    try:
        limiter._storage.reset()
    except Exception:
        pass
    token_cache.clear()


@pytest.fixture()
def key_fn_app():
    """Cheap Flask app for unit-testing ``_rate_limit_key`` with a
    proper request context. The app is not wired to any routes; we
    only need ``test_request_context`` to populate ``request.remote_addr``
    and ``g`` the way the real limiter does.
    """
    return Flask("test-key-fn")


class TestRateLimitKeyPreference:
    def test_prefers_current_token_token_id(self, key_fn_app):
        with key_fn_app.test_request_context("/probe"):
            g.current_token = {"token_id": 42}
            g.current_user = {"user_id": 99, "kind": "wms_token"}
            assert _rate_limit_key() == "token:42"

    def test_falls_back_to_user_id_when_no_token(self, key_fn_app):
        with key_fn_app.test_request_context("/probe"):
            g.current_user = {"user_id": 7}
            assert _rate_limit_key() == "user:7"

    def test_falls_back_to_ip_when_nothing_set(self, key_fn_app):
        with key_fn_app.test_request_context(
            "/probe", environ_base={"REMOTE_ADDR": "10.0.0.5"}
        ):
            assert _rate_limit_key() == "ip:10.0.0.5"

    def test_token_key_distinct_from_user_key(self, key_fn_app):
        """Prove bucket isolation at the key-function level: a
        connector token with id=5 and a cookie user with id=5 produce
        different strings, so flask-limiter treats them as independent
        buckets."""
        with key_fn_app.test_request_context("/probe"):
            g.current_token = {"token_id": 5}
            token_key = _rate_limit_key()
        with key_fn_app.test_request_context("/probe"):
            g.current_user = {"user_id": 5}
            user_key = _rate_limit_key()
        assert token_key == "token:5"
        assert user_key == "user:5"
        assert token_key != user_key


class TestSnapshotBucketIsolationBetweenTokens:
    def test_two_tokens_have_independent_snapshot_buckets(
        self, client, seed_data
    ):
        """Snapshot endpoint is 2/min per token. Hit it 3 times with
        token A -> the third call gets 429 (rate-limited). Token B's
        next call is NOT 429 because it has its own bucket.

        We don't need the keeper actually promoting the scan; the
        rate-limit gate runs BEFORE the handler body, so a 503
        ``snapshot_keeper_unavailable`` on the first two calls is
        equally fine.
        """
        plaintext_a = f"bucket-a-{uuid.uuid4()}"
        plaintext_b = f"bucket-b-{uuid.uuid4()}"
        insert_token(plaintext=plaintext_a, warehouse_ids=[1])
        insert_token(plaintext=plaintext_b, warehouse_ids=[1])

        headers_a = {"X-WMS-Token": plaintext_a}
        headers_b = {"X-WMS-Token": plaintext_b}

        statuses = [
            client.get(
                "/api/v1/snapshot/inventory?warehouse_id=1", headers=headers_a
            ).status_code
            for _ in range(3)
        ]
        # Third call on token A trips the 2/min limit.
        assert statuses[-1] == 429, (
            f"snapshot endpoint is 2/min per token; third call on same "
            f"token must be 429. Got sequence {statuses}"
        )

        # Token B's first call lands in its own bucket and is not
        # rate-limited.
        resp_b = client.get(
            "/api/v1/snapshot/inventory?warehouse_id=1", headers=headers_b
        )
        assert resp_b.status_code != 429, (
            "token B's bucket must be independent of token A's"
        )


class TestSnapshotAndPollingBucketsDoNotShare:
    def test_snapshot_saturation_does_not_rate_limit_polling(
        self, client, seed_data
    ):
        """flask-limiter buckets on (key, route). Saturating the
        snapshot endpoint for one token does NOT consume that token's
        polling budget, even with the same ``token:<id>`` key."""
        plaintext = f"cross-route-{uuid.uuid4()}"
        insert_token(
            plaintext=plaintext,
            warehouse_ids=[1],
            event_types=["receipt.completed"],
        )
        headers = {"X-WMS-Token": plaintext}

        # Saturate snapshot (3 calls, last is 429).
        for _ in range(3):
            client.get(
                "/api/v1/snapshot/inventory?warehouse_id=1", headers=headers
            )

        # Polling still has 120/min available; a single poll is not
        # rate-limited.
        poll_resp = client.get(
            "/api/v1/events?after=0&warehouse_id=1", headers=headers
        )
        assert poll_resp.status_code != 429, (
            "polling bucket is separate from snapshot bucket for the "
            "same token; saturating snapshot must not limit polling"
        )
