"""Tests for rate limiter, circuit breaker, and BaseConnector.make_request.

Covers:
- Exponential backoff calculation
- Jitter produces different delays
- Circuit breaker state transitions (closed -> open -> half-open -> closed/re-open)
- Rate limit header parsing
- Proactive slowdown threshold
- Custom header name overrides
- make_request retries on 429/503
- make_request triggers circuit breaker after repeated failures
"""

import os
import sys
import time as _time
from unittest.mock import Mock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from connectors.base import BaseConnector
from connectors.example import ExampleConnector
from connectors.rate_limiter import (
    CircuitBreakerState,
    CircuitOpenError,
    RateLimitState,
    exponential_backoff,
)


# ---------------------------------------------------------------------------
# Exponential backoff
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    def test_attempt_0(self):
        """attempt=0 should produce delay in [1s, 2s]."""
        delay = exponential_backoff(0)
        assert 1.0 <= delay <= 2.0

    def test_attempt_1(self):
        """attempt=1 should produce delay in [2s, 3s]."""
        delay = exponential_backoff(1)
        assert 2.0 <= delay <= 3.0

    def test_attempt_2(self):
        """attempt=2 should produce delay in [4s, 5s]."""
        delay = exponential_backoff(2)
        assert 4.0 <= delay <= 5.0

    def test_jitter_produces_different_delays(self):
        """Two calls with the same attempt should produce different delays (jitter)."""
        delays = {exponential_backoff(1) for _ in range(10)}
        # With 10 samples from a continuous uniform distribution,
        # all should be unique with very high probability
        assert len(delays) > 5

    def test_custom_base_delay(self):
        """base_delay scales the result."""
        delay = exponential_backoff(0, base_delay=0.1, max_jitter=0.0)
        assert delay == pytest.approx(0.1, abs=0.01)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_closed_initially(self):
        cb = CircuitBreakerState()
        assert not cb.is_open
        cb.check()  # should not raise

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreakerState(threshold=3, cooldown_seconds=60)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open
        with pytest.raises(CircuitOpenError):
            cb.check()

    def test_success_before_threshold_resets_count(self):
        cb = CircuitBreakerState(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failures == 0
        assert not cb.is_open

    def test_does_not_open_below_threshold(self):
        cb = CircuitBreakerState(threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert not cb.is_open
        cb.check()  # should not raise

    def test_half_open_after_cooldown(self):
        """After cooldown expires, check() allows one call through."""
        cb = CircuitBreakerState(threshold=1, cooldown_seconds=1)
        cb.record_failure()
        assert cb.is_open
        # Simulate cooldown expiry by backdating opened_at
        cb.opened_at = _time.monotonic() - 2
        cb.check()  # should not raise - half-open

    def test_success_in_half_open_closes_circuit(self):
        cb = CircuitBreakerState(threshold=1, cooldown_seconds=1)
        cb.record_failure()
        cb.opened_at = _time.monotonic() - 2  # force half-open
        cb.check()
        cb.record_success()
        assert not cb.is_open
        assert cb.failures == 0

    def test_failure_in_half_open_reopens_circuit(self):
        cb = CircuitBreakerState(threshold=1, cooldown_seconds=1)
        cb.record_failure()
        original_opened_at = cb.opened_at
        cb.opened_at = _time.monotonic() - 2  # force half-open
        cb.check()  # half-open, allowed through
        cb.record_failure()  # half-open call failed
        assert cb.is_open
        assert cb.opened_at > original_opened_at  # cooldown reset

    def test_cooldown_not_expired_still_raises(self):
        cb = CircuitBreakerState(threshold=1, cooldown_seconds=300)
        cb.record_failure()
        with pytest.raises(CircuitOpenError):
            cb.check()


# ---------------------------------------------------------------------------
# Rate limit headers
# ---------------------------------------------------------------------------


class TestRateLimitState:
    def _mock_response(self, headers):
        resp = Mock()
        resp.headers = headers
        return resp

    def test_parses_standard_headers(self):
        state = RateLimitState()
        resp = self._mock_response({
            "X-RateLimit-Remaining": "42",
            "X-RateLimit-Limit": "100",
            "Retry-After": "30",
        })
        state.update_from_response(resp, "X-RateLimit-Remaining", "X-RateLimit-Limit", "Retry-After")
        assert state.remaining == 42
        assert state.limit == 100
        assert state.retry_after == 30.0

    def test_missing_headers_leave_state_untouched(self):
        state = RateLimitState(remaining=50, limit=100)
        resp = self._mock_response({})
        state.update_from_response(resp, "X-RateLimit-Remaining", "X-RateLimit-Limit", "Retry-After")
        # Unchanged when headers absent
        assert state.remaining == 50
        assert state.limit == 100

    def test_invalid_header_ignored(self):
        state = RateLimitState()
        resp = self._mock_response({"X-RateLimit-Remaining": "not-a-number"})
        state.update_from_response(resp, "X-RateLimit-Remaining", "X-RateLimit-Limit", "Retry-After")
        assert state.remaining is None

    def test_no_slowdown_when_plenty_remaining(self):
        state = RateLimitState(remaining=80, limit=100)
        assert state.compute_slowdown(0.1) == 0.0

    def test_slowdown_kicks_in_below_threshold(self):
        state = RateLimitState(remaining=5, limit=100)  # 5% remaining
        delay = state.compute_slowdown(0.1)
        assert delay > 0

    def test_slowdown_at_zero_remaining_maxes_out(self):
        state = RateLimitState(remaining=0, limit=100)
        delay = state.compute_slowdown(0.1)
        assert delay > 0.5  # substantial delay

    def test_no_slowdown_without_headers(self):
        state = RateLimitState()  # no headers yet
        assert state.compute_slowdown(0.1) == 0.0


# ---------------------------------------------------------------------------
# Custom header overrides in subclass
# ---------------------------------------------------------------------------


class TestCustomHeaderOverrides:
    def test_subclass_can_override_headers(self):
        """Connectors can declare different header names as class attributes."""

        class ShopifyLikeConnector(ExampleConnector):
            rate_limit_remaining_header = "X-Shopify-Shop-Api-Call-Limit"
            rate_limit_limit_header = "X-Shopify-Limit"
            retry_after_header = "Retry-After-Shop"

        conn = ShopifyLikeConnector({})
        assert conn.rate_limit_remaining_header == "X-Shopify-Shop-Api-Call-Limit"
        assert conn.rate_limit_limit_header == "X-Shopify-Limit"
        assert conn.retry_after_header == "Retry-After-Shop"

    def test_default_headers_on_base(self):
        assert BaseConnector.rate_limit_remaining_header == "X-RateLimit-Remaining"
        assert BaseConnector.rate_limit_limit_header == "X-RateLimit-Limit"
        assert BaseConnector.retry_after_header == "Retry-After"


# ---------------------------------------------------------------------------
# make_request integration
# ---------------------------------------------------------------------------


def _mock_response(status_code, headers=None, text=""):
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    return resp


class TestMakeRequest:
    @pytest.fixture(autouse=True)
    def _patch_sleep(self):
        """Patch time.sleep in the base module so tests don't actually wait."""
        with patch("connectors.base.time.sleep") as sleep_mock:
            self.sleep_mock = sleep_mock
            yield

    @pytest.fixture(autouse=True)
    def _bypass_url_guard(self):
        """Bypass the SSRF URL guard (V-009) for these tests.

        The URL guard performs DNS resolution to check for private IPs,
        which fails offline for the fabricated api.example.com hostname
        used throughout these tests. The guard itself is tested in
        tests/test_url_guard.py; here we skip it so these tests remain
        focused on retry/backoff/circuit-breaker behavior.
        """
        with patch("connectors.base.assert_url_allowed", lambda url: None):
            yield

    def test_success_returns_response(self):
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(200, {"X-RateLimit-Remaining": "99", "X-RateLimit-Limit": "100"})
            response = conn.make_request("GET", "https://api.example.com/ping")
        assert response.status_code == 200
        assert req.call_count == 1
        assert conn._rate_limit.remaining == 99

    def test_retries_on_429(self):
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.side_effect = [
                _mock_response(429),
                _mock_response(429),
                _mock_response(200),
            ]
            response = conn.make_request("GET", "https://api.example.com/data")
        assert response.status_code == 200
        assert req.call_count == 3

    def test_retries_on_503(self):
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.side_effect = [_mock_response(503), _mock_response(200)]
            response = conn.make_request("GET", "https://api.example.com/data")
        assert response.status_code == 200
        assert req.call_count == 2

    def test_gives_up_after_max_retries(self):
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(429)
            response = conn.make_request("GET", "https://api.example.com/data")
        # Returns the last 429 response after exhausting retries
        assert response.status_code == 429
        assert req.call_count == 3

    def test_no_retry_on_4xx_other_than_429(self):
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(404)
            response = conn.make_request("GET", "https://api.example.com/missing")
        assert response.status_code == 404
        assert req.call_count == 1  # no retry

    def test_retry_on_network_error(self):
        import requests as _req
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.side_effect = [
                _req.ConnectionError("boom"),
                _mock_response(200),
            ]
            response = conn.make_request("GET", "https://api.example.com/data")
        assert response.status_code == 200
        assert req.call_count == 2

    def test_circuit_opens_after_repeated_failures(self):
        """After 5 consecutive 500s across calls, the breaker opens."""
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(500)
            for _ in range(5):
                conn.make_request("GET", "https://api.example.com/data")

        # 6th call should fail fast
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(200)
            with pytest.raises(CircuitOpenError):
                conn.make_request("GET", "https://api.example.com/data")
            assert req.call_count == 0  # never actually called

    def test_respects_retry_after_header(self):
        """When server sends Retry-After, we sleep for that duration."""
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.side_effect = [
                _mock_response(429, {"Retry-After": "5"}),
                _mock_response(200),
            ]
            conn.make_request("GET", "https://api.example.com/data")
        # Check that sleep was called with 5 (from Retry-After)
        sleep_durations = [call.args[0] for call in self.sleep_mock.call_args_list]
        assert 5.0 in sleep_durations or 5 in sleep_durations

    def test_success_closes_circuit(self):
        """A successful call after failures resets the failure counter."""
        conn = ExampleConnector({})
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(500)
            for _ in range(3):
                conn.make_request("GET", "https://api.example.com/data")
            assert conn._circuit_breaker.failures == 3

            req.return_value = _mock_response(200)
            conn.make_request("GET", "https://api.example.com/data")
            assert conn._circuit_breaker.failures == 0
            assert not conn._circuit_breaker.is_open

    def test_rate_limit_slowdown_applied(self):
        """When remaining is low, a proactive delay is added before the next call."""
        conn = ExampleConnector({})
        # Set up rate limit state showing low remaining
        conn._rate_limit.remaining = 5
        conn._rate_limit.limit = 100  # 5% remaining, below 10% threshold

        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(200)
            conn.make_request("GET", "https://api.example.com/data")

        # time.sleep should have been called with a positive delay
        sleep_durations = [call.args[0] for call in self.sleep_mock.call_args_list]
        assert any(d > 0 for d in sleep_durations)

    def test_subclass_uses_custom_headers(self):
        """make_request reads rate limit from subclass-defined header names."""

        class CustomConnector(ExampleConnector):
            rate_limit_remaining_header = "My-Remaining"
            rate_limit_limit_header = "My-Limit"

        conn = CustomConnector({})
        with patch("connectors.base.requests.request") as req:
            req.return_value = _mock_response(200, {"My-Remaining": "50", "My-Limit": "100"})
            conn.make_request("GET", "https://api.example.com/data")

        assert conn._rate_limit.remaining == 50
        assert conn._rate_limit.limit == 100


class TestConnectorInitsRateLimiter:
    def test_each_instance_has_own_state(self):
        """Rate limit and circuit breaker state is per-instance, not shared."""
        a = ExampleConnector({})
        b = ExampleConnector({})
        a._circuit_breaker.record_failure()
        assert a._circuit_breaker.failures == 1
        assert b._circuit_breaker.failures == 0

    def test_threshold_inherited_from_class(self):
        """Default threshold comes from class attribute."""
        conn = ExampleConnector({})
        assert conn._circuit_breaker.threshold == 5
        assert conn._circuit_breaker.cooldown_seconds == 300
