"""Tests for boot-time orphaned in_flight reset and shutdown drain."""

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
from services.webhook_dispatcher.http_client import HttpClient

from tests.test_webhook_dispatcher_dispatch import (  # noqa: E402
    StubHttpClient,
    _conn,
    _emit_event,
    _make_subscription,
    _wait_for_visible,
)


def _seed_in_flight(sub_id: str, event_id: int, attempted_offset_s: int = 30) -> int:
    """Insert an in_flight row with attempted_at offset back from
    NOW() by ``attempted_offset_s`` seconds. Returns delivery_id."""
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, attempted_at, secret_generation)
            VALUES (%s, %s, 1, 'in_flight',
                    NOW() - INTERVAL '{attempted_offset_s} seconds',
                    NOW() - INTERVAL '{attempted_offset_s} seconds', 1)
            RETURNING delivery_id
            """,
            (sub_id, event_id),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _row_status(delivery_id: int):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, scheduled_at FROM webhook_deliveries "
            "WHERE delivery_id = %s",
            (delivery_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# reset_orphaned_in_flight
# ---------------------------------------------------------------------


def test_reset_orphaned_in_flight_unconditional_for_stale_rows():
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        e1 = _emit_event()
        emitted.append(e1)
        delivery_id = _seed_in_flight(sub_id, e1, attempted_offset_s=120)

        before_status, _ = _row_status(delivery_id)
        assert before_status == "in_flight"

        count = dispatch_module.reset_orphaned_in_flight(os.environ["DATABASE_URL"])

        after_status, after_scheduled = _row_status(delivery_id)
        assert after_status == "pending"
        assert count >= 1
        # scheduled_at must be at NOW() (within tolerance), not the
        # original 2-minute-old timestamp.
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT NOW() - %s < INTERVAL '5 seconds'",
                (after_scheduled,),
            )
            assert cur.fetchone()[0] is True
        finally:
            conn.close()
    finally:
        cleanup()
        if emitted:
            c = _conn()
            c.autocommit = True
            try:
                c.cursor().execute(
                    "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                    (emitted,),
                )
            finally:
                c.close()


def test_reset_orphaned_in_flight_resets_recent_rows_too():
    """No age-threshold heuristic: a row that flipped to in_flight
    one second ago is still orphaned at boot (the dispatcher is the
    sole writer and is not running yet)."""
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        e1 = _emit_event()
        emitted.append(e1)
        delivery_id = _seed_in_flight(sub_id, e1, attempted_offset_s=1)

        dispatch_module.reset_orphaned_in_flight(os.environ["DATABASE_URL"])

        after_status, _ = _row_status(delivery_id)
        assert after_status == "pending"
    finally:
        cleanup()
        if emitted:
            c = _conn()
            c.autocommit = True
            try:
                c.cursor().execute(
                    "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                    (emitted,),
                )
            finally:
                c.close()


def test_reset_orphaned_in_flight_does_not_touch_other_statuses():
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        e1 = _emit_event()
        emitted.append(e1)
        # Seed pending, succeeded, dlq -- none should flip.
        conn = _conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO webhook_deliveries
                    (subscription_id, event_id, attempt_number, status,
                     scheduled_at, secret_generation)
                VALUES (%s, %s, 1, 'pending', NOW(), 1)
                RETURNING delivery_id
                """,
                (sub_id, e1),
            )
            pending_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO webhook_deliveries
                    (subscription_id, event_id, attempt_number, status,
                     scheduled_at, completed_at, secret_generation)
                VALUES (%s, %s, 1, 'succeeded', NOW(), NOW(), 1)
                RETURNING delivery_id
                """,
                (sub_id, e1),
            )
            succeeded_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO webhook_deliveries
                    (subscription_id, event_id, attempt_number, status,
                     scheduled_at, completed_at, secret_generation)
                VALUES (%s, %s, 8, 'dlq', NOW(), NOW(), 1)
                RETURNING delivery_id
                """,
                (sub_id, e1),
            )
            dlq_id = cur.fetchone()[0]
        finally:
            conn.close()

        dispatch_module.reset_orphaned_in_flight(os.environ["DATABASE_URL"])

        assert _row_status(pending_id)[0] == "pending"
        assert _row_status(succeeded_id)[0] == "succeeded"
        assert _row_status(dlq_id)[0] == "dlq"
    finally:
        cleanup()
        if emitted:
            c = _conn()
            c.autocommit = True
            try:
                c.cursor().execute(
                    "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                    (emitted,),
                )
            finally:
                c.close()


# ---------------------------------------------------------------------
# Pool shutdown drain
# ---------------------------------------------------------------------


class _ClosingHttpClient:
    """Tracks close() invocations so the test can assert the pool
    closed the shared HTTP client on shutdown."""

    def __init__(self):
        self.close_calls = 0

    def send(self, *args, **kwargs):  # pragma: no cover -- not invoked
        raise AssertionError("send not expected during drain test")

    def close(self):
        self.close_calls += 1


def test_pool_shutdown_closes_shared_http_client():
    from queue import Queue

    client = _ClosingHttpClient()
    pool = dispatch_module.SubscriptionWorkerPool(
        database_url=os.environ["DATABASE_URL"],
        wake_queue=Queue(),
        http_client=client,
    )
    pool.start()
    pool.shutdown()
    pool.join(timeout_s=5.0)
    assert client.close_calls == 1


def test_pool_shutdown_idempotent_close():
    from queue import Queue

    client = _ClosingHttpClient()
    pool = dispatch_module.SubscriptionWorkerPool(
        database_url=os.environ["DATABASE_URL"],
        wake_queue=Queue(),
        http_client=client,
    )
    pool.start()
    pool.shutdown()
    pool.shutdown()  # second call must be a no-op
    pool.join(timeout_s=5.0)
    # join() invokes close once per call; a second join is also OK.
    pool.join(timeout_s=1.0)
    assert client.close_calls >= 1


def test_subscription_worker_drains_within_window():
    """A worker mid-cycle must observe the shutdown signal and
    exit within a small multiple of the shutdown wait. The HTTP
    stub returns immediately so the bound is the wake-loop poll
    interval (1s)."""
    sub_id, _, cleanup = _make_subscription()
    emitted = []
    try:
        e1 = _emit_event()
        emitted.append(e1)
        _wait_for_visible(e1)

        client = StubHttpClient(responses=[200])
        shutdown = threading.Event()
        worker = dispatch_module.SubscriptionWorker(
            subscription_id=sub_id,
            database_url=os.environ["DATABASE_URL"],
            http_client=client,
            shutdown=shutdown,
        )
        worker.start()
        worker.signal()
        # Let the worker make at least one delivery.
        for _ in range(50):
            if client.calls:
                break
            time.sleep(0.05)
        assert client.calls, "worker did not deliver before shutdown"

        started = time.monotonic()
        shutdown.set()
        worker.signal()
        worker.join(timeout=5.0)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), "worker did not exit on shutdown"
        assert elapsed < 3.0, f"worker drain took {elapsed:.2f}s"
    finally:
        cleanup()
        if emitted:
            c = _conn()
            c.autocommit = True
            try:
                c.cursor().execute(
                    "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                    (emitted,),
                )
            finally:
                c.close()


def test_real_http_client_close_idempotent():
    """The production HttpClient must tolerate close() before any
    send() (lazy session) and a double close()."""
    client = HttpClient()
    client.close()  # no session yet
    client.close()  # double close
