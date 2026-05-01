"""Tests for the per-subscription token bucket rate limiter."""

import os
import sys
import threading
import time

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from services.webhook_dispatcher import dispatch as dispatch_module
from services.webhook_dispatcher.rate_limiter import TokenBucket

from tests.test_webhook_dispatcher_dispatch import (  # noqa: E402
    StubHttpClient,
    _conn,
    _emit_event,
    _make_subscription,
    _wait_for_visible,
)


# ---------------------------------------------------------------------
# TokenBucket math
# ---------------------------------------------------------------------


def test_token_bucket_initial_capacity_full():
    """A fresh bucket has rate-many tokens, so the first burst of
    rate acquires never blocks."""
    bucket = TokenBucket(10)
    started = time.monotonic()
    for _ in range(10):
        assert bucket.acquire(timeout_s=0.01) is True
    assert time.monotonic() - started < 0.05


def test_token_bucket_blocks_after_burst_then_refills():
    bucket = TokenBucket(10)
    for _ in range(10):
        assert bucket.acquire(timeout_s=0.01) is True
    started = time.monotonic()
    assert bucket.acquire(timeout_s=1.0) is True
    elapsed = time.monotonic() - started
    assert 0.05 <= elapsed <= 0.5, f"refill window unexpected: {elapsed:.3f}s"


def test_token_bucket_acquire_timeout_returns_false():
    bucket = TokenBucket(1)
    assert bucket.acquire(timeout_s=0.01) is True  # drain
    assert bucket.acquire(timeout_s=0.05) is False


def test_token_bucket_shutdown_short_circuits_wait():
    bucket = TokenBucket(1)
    assert bucket.acquire(timeout_s=0.01) is True
    shutdown = threading.Event()
    threading.Timer(0.05, shutdown.set).start()
    started = time.monotonic()
    result = bucket.acquire(timeout_s=10.0, shutdown=shutdown)
    elapsed = time.monotonic() - started
    assert result is False
    assert elapsed < 0.5


def test_token_bucket_set_rate_clamps_and_resets():
    bucket = TokenBucket(100)
    assert bucket.rate == 100
    # Acquire one then change rate to 5; tokens should clamp to 5.
    assert bucket.acquire(timeout_s=0.01) is True
    bucket.set_rate(5)
    assert bucket.rate == 5
    # Capacity is now 5, so 5 acquires must succeed without
    # blocking and a 6th acquire must block long enough that a
    # 0.01s timeout fails.
    for _ in range(5):
        assert bucket.acquire(timeout_s=0.01) is True
    assert bucket.acquire(timeout_s=0.01) is False


def test_token_bucket_set_rate_no_op_when_unchanged():
    bucket = TokenBucket(10)
    for _ in range(10):
        bucket.acquire(timeout_s=0.01)
    # Changing to the same rate must not refill the bucket.
    bucket.set_rate(10)
    assert bucket.acquire(timeout_s=0.01) is False


def test_token_bucket_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        TokenBucket(-1)
    bucket = TokenBucket(10)
    with pytest.raises(ValueError):
        bucket.set_rate(0)


# ---------------------------------------------------------------------
# Dispatch integration
# ---------------------------------------------------------------------


def _set_subscription_rate(subscription_id: str, rate: int) -> None:
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE webhook_subscriptions SET rate_limit_per_second = %s "
            "WHERE subscription_id = %s",
            (rate, subscription_id),
        )
    finally:
        conn.close()


def _delete_events(event_ids):
    """Remove emitted integration_events rows. Tests that emit
    bursts must clean up; subsequent tests in the run create
    subscriptions with cursor=0 that would otherwise observe
    these rows as fresh events."""
    if not event_ids:
        return
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM integration_events WHERE event_id = ANY(%s)",
            (list(event_ids),),
        )
    finally:
        conn.close()


def test_deliver_one_calls_acquire_with_subscription_rate():
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        _set_subscription_rate(sub_id, 7)
        event_id = _emit_event()
        emitted.append(event_id)
        _wait_for_visible(event_id)

        observed = []

        def fake_acquire(rate: int) -> bool:
            observed.append(rate)
            return True

        client = StubHttpClient(responses=[200])
        conn = _conn()
        try:
            outcome = dispatch_module.deliver_one(
                conn, sub_id, client, acquire_rate_token=fake_acquire,
            )
        finally:
            conn.close()
        assert outcome is not None and outcome.status == "succeeded"
        assert observed == [7]
    finally:
        cleanup()
        _delete_events(emitted)


def test_deliver_one_returns_none_when_acquire_returns_false():
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        event_id = _emit_event()
        emitted.append(event_id)
        _wait_for_visible(event_id)

        client = StubHttpClient(responses=[200])
        conn = _conn()
        try:
            outcome = dispatch_module.deliver_one(
                conn, sub_id, client, acquire_rate_token=lambda _r: False,
            )
        finally:
            conn.close()
        assert outcome is None
        assert client.calls == []

        # The row must remain pending (or not exist) -- no
        # in_flight flip should have happened.
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(
                "SELECT status FROM webhook_deliveries WHERE subscription_id = %s",
                (sub_id,),
            )
            rows = cur.fetchall()
        finally:
            c.close()
        assert rows == []
    finally:
        cleanup()
        _delete_events(emitted)


def test_deliver_one_rereads_status_after_acquire():
    """If the subscription flips to paused while the worker waits
    on a token, the post-acquire re-read short-circuits the cycle
    so an admin pause takes effect immediately rather than after
    one more delivery."""
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        event_id = _emit_event()
        emitted.append(event_id)
        _wait_for_visible(event_id)

        def acquire_then_pause(rate: int) -> bool:
            c = _conn()
            c.autocommit = True
            try:
                cur = c.cursor()
                cur.execute(
                    "UPDATE webhook_subscriptions SET status = 'paused' "
                    "WHERE subscription_id = %s",
                    (sub_id,),
                )
            finally:
                c.close()
            return True

        client = StubHttpClient(responses=[200])
        conn = _conn()
        try:
            outcome = dispatch_module.deliver_one(
                conn, sub_id, client, acquire_rate_token=acquire_then_pause,
            )
        finally:
            conn.close()
        assert outcome is None
        assert client.calls == []
    finally:
        cleanup()
        _delete_events(emitted)


def test_subscription_worker_rate_throttles_burst():
    """End-to-end: a subscription with rate=5 sees at most ~5
    POSTs in the first second of a burst of 20 events. The bucket
    starts full so the first 5 are immediate; subsequent calls
    are paced by the refill rate."""
    sub_id, _, cleanup = _make_subscription()
    events = []
    try:
        _set_subscription_rate(sub_id, 5)
        events = [_emit_event() for _ in range(20)]
        for eid in events:
            _wait_for_visible(eid)

        client = StubHttpClient(responses=[200] * 20)
        shutdown = threading.Event()
        worker = dispatch_module.SubscriptionWorker(
            subscription_id=sub_id,
            database_url=os.environ["DATABASE_URL"],
            http_client=client,
            shutdown=shutdown,
        )
        worker.start()
        worker.signal()
        # Sample at 1.05s: the initial burst (5) plus one refill
        # tick (~5/s -> at most ~5 more) bounds the count well
        # below the 20 emitted.
        time.sleep(1.05)
        count_at_1s = len(client.calls)
        shutdown.set()
        worker.signal()
        worker.join(timeout=5.0)

        assert count_at_1s >= 5, f"initial burst missing: {count_at_1s}"
        assert count_at_1s <= 12, (
            f"rate=5 should yield at most ~10 calls in the first "
            f"second; observed {count_at_1s}"
        )
    finally:
        cleanup()
        _delete_events(events)


def test_subscription_worker_observes_rate_limit_change():
    """Updating rate_limit_per_second mid-flight reconciles the
    bucket on the next dispatch cycle."""
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        _set_subscription_rate(sub_id, 100)
        rates_seen = []

        original = dispatch_module.SubscriptionWorker._acquire_rate_token

        def spy(self, rate):
            rates_seen.append(rate)
            return original(self, rate)

        dispatch_module.SubscriptionWorker._acquire_rate_token = spy
        try:
            event_a = _emit_event()
            emitted.append(event_a)
            _wait_for_visible(event_a)

            client = StubHttpClient(responses=[200] * 5)
            shutdown = threading.Event()
            worker = dispatch_module.SubscriptionWorker(
                subscription_id=sub_id,
                database_url=os.environ["DATABASE_URL"],
                http_client=client,
                shutdown=shutdown,
            )
            worker.start()
            worker.signal()
            # Wait for first delivery
            for _ in range(50):
                if client.calls:
                    break
                time.sleep(0.05)
            assert client.calls, "first delivery did not land"

            _set_subscription_rate(sub_id, 25)
            event_b = _emit_event()
            emitted.append(event_b)
            _wait_for_visible(event_b)
            worker.signal()
            for _ in range(50):
                if len(client.calls) >= 2:
                    break
                time.sleep(0.05)

            shutdown.set()
            worker.signal()
            worker.join(timeout=5.0)

            assert 100 in rates_seen
            assert 25 in rates_seen
        finally:
            dispatch_module.SubscriptionWorker._acquire_rate_token = original
    finally:
        cleanup()
        _delete_events(emitted)
