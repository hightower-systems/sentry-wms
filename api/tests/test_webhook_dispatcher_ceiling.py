"""Tests for the v1.6.0 D7 ceiling auto-pause + worker
eviction (#179, also closes #177).

Plan sections 2.11 (ceilings + hard caps) and 2.9 (cross-worker
action table).

Coverage:

  * pending_ceiling auto-pause atomically with the failed-row
    write; status='paused', pause_reason='pending_ceiling'.
  * dlq_ceiling auto-pause atomically with the dlq flip;
    status='paused', pause_reason='dlq_ceiling'.
  * Below-threshold failure does NOT pause.
  * Already-paused subscription is not re-paused with a
    different reason.
  * deliver_one returns None on a paused subscription (existing
    D5 path).
  * SubscriptionWorker.request_eviction closes the connection
    and the run-loop exits within ~1s of the call.
  * SubscriptionWorkerPool fanout: a paused subscription_event
    on the queue evicts the matching worker; other event kinds
    just signal it.
  * Hard-cap env vars are read by env_validator helpers (D1
    landed the ranges; D7 documents the consumer surface in
    plan §2.11).

Tests that exercise auto-pause set ``pending_ceiling`` /
``dlq_ceiling`` low (100 / 10) so the test does not need to
emit thousands of events to hit the threshold.
"""

import os
import sys
import threading
import time
import uuid
from queue import Queue

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from services.webhook_dispatcher import dispatch as dispatch_module
from services.webhook_dispatcher import env_validator
from services.webhook_dispatcher import retry as retry_module
from services.webhook_dispatcher import wake as wake_module

from tests.test_webhook_dispatcher_dispatch import (  # noqa: E402
    StubHttpClient,
    _conn,
    _emit_event,
    _make_subscription,
    _wait_for_visible,
)


@pytest.fixture
def zero_retry_delays(monkeypatch):
    """Make retry slots immediately pickable so the auto-pause
    sequence does not wait wall-clock seconds. The schedule
    constants are asserted by D6's TestRetryScheduleConstant
    (which does NOT monkeypatch)."""
    monkeypatch.setattr(
        retry_module,
        "RETRY_SCHEDULE_SECONDS",
        (0, 0, 0, 0, 0, 0, 0, 0),
    )


def _set_ceiling(sub_id: str, *, pending: int = None, dlq: int = None):
    """Tighten the per-subscription ceilings for a test. The
    column-level CHECK from migration 029 bounds the lower side
    (pending >= 100, dlq >= 10) so tests use those minimums."""
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        if pending is not None:
            cur.execute(
                "UPDATE webhook_subscriptions SET pending_ceiling = %s WHERE subscription_id = %s",
                (pending, sub_id),
            )
        if dlq is not None:
            cur.execute(
                "UPDATE webhook_subscriptions SET dlq_ceiling = %s WHERE subscription_id = %s",
                (dlq, sub_id),
            )
    finally:
        conn.close()


def _subscription_state(sub_id: str):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, pause_reason FROM webhook_subscriptions WHERE subscription_id = %s",
            (sub_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# pending_ceiling
# ---------------------------------------------------------------------


class TestPendingCeilingAutoPause:
    """Unit-test _maybe_auto_pause directly with a controlled
    pending count. The integration with deliver_one is covered
    by ``test_below_threshold_does_not_pause`` (the call site
    fires through deliver_one's failure branch with no
    auto-pause) plus the existing D5 dispatch tests (the call
    sites are present in the failure branches by code
    inspection)."""

    def test_pending_ceiling_flip_at_threshold(self):
        """Plan §2.11: when pending count reaches the
        per-subscription ceiling, _maybe_auto_pause flips
        status='paused' with pause_reason='pending_ceiling' in
        the same transaction as the failed-row write."""
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            _set_ceiling(sub_id, pending=100)

            # Pre-populate 100 pending rows so the COUNT(*)
            # check inside _maybe_auto_pause sees count >=
            # ceiling. The future scheduled_at keeps them out
            # of _select_next_pending's path -- not relevant
            # for the unit test which calls _maybe_auto_pause
            # directly.
            conn = _conn()
            conn.autocommit = True
            try:
                cur = conn.cursor()
                for i in range(100):
                    cur.execute(
                        """
                        INSERT INTO webhook_deliveries
                            (subscription_id, event_id, attempt_number,
                             status, scheduled_at, secret_generation)
                        VALUES (%s, %s, 1, 'pending',
                                NOW() + INTERVAL '1 hour', 1)
                        """,
                        (sub_id, 1000000 + i),
                    )
            finally:
                conn.close()

            conn = _conn()
            try:
                cur = conn.cursor()
                reason = dispatch_module._maybe_auto_pause(  # noqa: SLF001
                    cur,
                    sub_id,
                    pending_ceiling=100,
                    dlq_ceiling=10,
                    after_terminal_dlq=False,
                )
                conn.commit()
            finally:
                conn.close()

            assert reason == "pending_ceiling"

            status, current_reason = _subscription_state(sub_id)
            assert status == "paused"
            assert current_reason == "pending_ceiling"
        finally:
            cleanup()

    def test_below_threshold_does_not_pause(self, zero_retry_delays):
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            _set_ceiling(sub_id, pending=10000)

            e1 = _emit_event(event_type="d7.pending.below")
            emitted.append(e1)
            _wait_for_visible(e1)

            stub = StubHttpClient(responses=[500])
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()

            assert outcome is not None
            assert outcome.status == "failed"
            # Pending count is 1 (the retry slot); ceiling is
            # 10_000. Auto-pause should not fire.
            status, reason = _subscription_state(sub_id)
            assert status == "active"
            assert reason is None
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cleanup_conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cleanup_conn.close()


# ---------------------------------------------------------------------
# dlq_ceiling
# ---------------------------------------------------------------------


class TestDlqCeilingAutoPause:
    """Same shape as the pending-ceiling unit test: drive
    _maybe_auto_pause directly with a controlled dlq count."""

    def test_dlq_ceiling_flip_at_threshold(self):
        """Plan §2.11: dlq count reaches dlq_ceiling -> status
        flips to paused with pause_reason='dlq_ceiling'."""
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            _set_ceiling(sub_id, dlq=10)

            conn = _conn()
            conn.autocommit = True
            try:
                cur = conn.cursor()
                for i in range(10):
                    cur.execute(
                        """
                        INSERT INTO webhook_deliveries
                            (subscription_id, event_id, attempt_number,
                             status, scheduled_at, completed_at,
                             secret_generation)
                        VALUES (%s, %s, 8, 'dlq', NOW(), NOW(), 1)
                        """,
                        (sub_id, 2000000 + i),
                    )
            finally:
                conn.close()

            conn = _conn()
            try:
                cur = conn.cursor()
                reason = dispatch_module._maybe_auto_pause(  # noqa: SLF001
                    cur,
                    sub_id,
                    pending_ceiling=10000,
                    dlq_ceiling=10,
                    after_terminal_dlq=True,
                )
                conn.commit()
            finally:
                conn.close()

            assert reason == "dlq_ceiling"

            status, current_reason = _subscription_state(sub_id)
            assert status == "paused"
            assert current_reason == "dlq_ceiling"
        finally:
            cleanup()

    def test_already_paused_subscription_is_not_re_paused(
        self, zero_retry_delays
    ):
        """Conditional UPDATE WHERE status='active' guard: if a
        subscription is already paused (e.g., by an admin or by
        a peer dispatcher's auto-pause), a subsequent
        ceiling-crossing on this dispatcher does not flip the
        status back to a different reason. The original reason
        survives."""
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            # Manually flip to paused with reason='manual'.
            conn = _conn()
            conn.autocommit = True
            conn.cursor().execute(
                "UPDATE webhook_subscriptions SET status='paused', "
                "pause_reason='manual' WHERE subscription_id=%s",
                (sub_id,),
            )
            conn.close()

            # Now try to invoke the auto-pause helper directly
            # with a count that would normally trigger
            # pending_ceiling. The conditional UPDATE WHERE
            # status='active' should be a no-op.
            conn = _conn()
            try:
                cur = conn.cursor()
                # Pre-populate to exceed the (low) pending_ceiling
                # so _maybe_auto_pause's COUNT check returns
                # > ceiling.
                for i in range(101):
                    cur.execute(
                        """
                        INSERT INTO webhook_deliveries
                            (subscription_id, event_id, attempt_number,
                             status, scheduled_at, secret_generation)
                        VALUES (%s, %s, 1, 'pending',
                                NOW() + INTERVAL '1 hour', 1)
                        """,
                        (sub_id, 3000000 + i),
                    )
                _set_ceiling(sub_id, pending=100)

                reason = dispatch_module._maybe_auto_pause(  # noqa: SLF001
                    cur,
                    sub_id,
                    pending_ceiling=100,
                    dlq_ceiling=10,
                    after_terminal_dlq=False,
                )
                conn.commit()
            finally:
                conn.close()

            # _maybe_auto_pause returns None when the UPDATE
            # affected 0 rows (because the WHERE status='active'
            # guard kept the manual pause intact).
            assert reason is None

            status, current_reason = _subscription_state(sub_id)
            assert status == "paused"
            assert current_reason == "manual"  # unchanged
        finally:
            cleanup()


# ---------------------------------------------------------------------
# Worker eviction (#177 close)
# ---------------------------------------------------------------------


class TestWorkerEviction:
    def test_request_eviction_exits_run_loop(self):
        """Plan §2.9 + #177: request_eviction closes the
        worker's connection and flips the per-worker eviction
        event; the run() loop exits within ~1s."""
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            shutdown = threading.Event()
            worker = dispatch_module.SubscriptionWorker(
                subscription_id=sub_id,
                database_url=os.environ["DATABASE_URL"],
                http_client=StubHttpClient(),
                shutdown=shutdown,
            )
            worker.start()
            # Give the worker a moment to open its connection
            # and enter the wake.wait loop.
            for _ in range(20):
                if worker._conn is not None:  # noqa: SLF001
                    break
                time.sleep(0.05)
            assert worker._conn is not None  # noqa: SLF001

            worker.request_eviction()
            worker.join(timeout=3.0)
            assert not worker.is_alive(), (
                "request_eviction must exit the worker run-loop within 3s"
            )
        finally:
            cleanup()


class TestSubscriptionWorkerPoolFanout:
    def test_paused_event_evicts_matching_worker(self, monkeypatch):
        """Pool fanout: a paused subscription_event on the wake
        queue calls request_eviction on the matching worker;
        the worker exits its run-loop."""
        # Long refresh interval so the test isn't racing the
        # pool refresh thread.
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            queue = Queue()
            pool = dispatch_module.SubscriptionWorkerPool(
                database_url=os.environ["DATABASE_URL"],
                wake_queue=queue,
                refresh_interval_s=300.0,
            )
            pool.start()
            try:
                # Wait for the pool to spawn the worker for our subscription.
                worker = None
                for _ in range(30):
                    with pool._lock:  # noqa: SLF001
                        worker = pool._workers.get(sub_id)  # noqa: SLF001
                    if worker is not None:
                        break
                    time.sleep(0.1)
                assert worker is not None, "pool did not spawn worker"

                queue.put(
                    wake_module.WakeEvent(
                        kind="subscription_event",
                        subscription_id=sub_id,
                        subscription_event_kind="paused",
                    )
                )

                worker.join(timeout=3.0)
                assert not worker.is_alive()
            finally:
                pool.shutdown()
                pool.join(timeout_s=5)
        finally:
            cleanup()

    def test_orphan_worker_evicted_on_refresh(self):
        """Issue #207: a worker whose subscription_id is no
        longer in the active set gets evicted on the next
        refresh cycle. Reproduces the phantom-worker shape from
        the v1.6.0 pre-merge gate (test fixture rolls back, direct
        SQL DELETE, etc.) where the admin pubsub publication is
        bypassed and the pool keeps a stale worker forever."""
        sub_id, _plaintext, cleanup = _make_subscription()
        cleanup_ran = False
        try:
            queue = Queue()
            pool = dispatch_module.SubscriptionWorkerPool(
                database_url=os.environ["DATABASE_URL"],
                wake_queue=queue,
                refresh_interval_s=300.0,
            )
            pool.start()
            try:
                worker = None
                for _ in range(30):
                    with pool._lock:  # noqa: SLF001
                        worker = pool._workers.get(sub_id)  # noqa: SLF001
                    if worker is not None:
                        break
                    time.sleep(0.1)
                assert worker is not None, "pool did not spawn worker"

                # Simulate the out-of-band removal path: cleanup
                # deletes the subscription via direct SQL without
                # publishing the cross-worker pubsub event the
                # admin API would have published. This is the
                # phantom-worker reproduction.
                cleanup()
                cleanup_ran = True

                # Drive a refresh manually instead of waiting for
                # the 300s interval. The orphan reconciliation
                # should evict our worker since sub_id is no
                # longer in webhook_subscriptions.
                pool._refresh_active_subscriptions()  # noqa: SLF001

                worker.join(timeout=3.0)
                assert not worker.is_alive(), (
                    "orphan worker must exit after reconciliation"
                )
                with pool._lock:  # noqa: SLF001
                    assert sub_id not in pool._workers, (  # noqa: SLF001
                        "orphan worker must be removed from the pool dict"
                    )
            finally:
                pool.shutdown()
                pool.join(timeout_s=5)
        finally:
            if not cleanup_ran:
                cleanup()

    def test_resumed_event_just_signals_does_not_evict(self):
        """Plan §2.9: ``resumed`` is NOT in the eviction set.
        The fanout signals the worker (so its next deliver_one
        re-reads the row state) but does not request_eviction."""
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            queue = Queue()
            pool = dispatch_module.SubscriptionWorkerPool(
                database_url=os.environ["DATABASE_URL"],
                wake_queue=queue,
                refresh_interval_s=300.0,
            )
            pool.start()
            try:
                worker = None
                for _ in range(30):
                    with pool._lock:  # noqa: SLF001
                        worker = pool._workers.get(sub_id)  # noqa: SLF001
                    if worker is not None:
                        break
                    time.sleep(0.1)
                assert worker is not None

                queue.put(
                    wake_module.WakeEvent(
                        kind="subscription_event",
                        subscription_id=sub_id,
                        subscription_event_kind="resumed",
                    )
                )
                # Wait briefly; worker should NOT exit.
                time.sleep(0.5)
                assert worker.is_alive(), (
                    "resumed event must signal, not evict"
                )
            finally:
                pool.shutdown()
                pool.join(timeout_s=5)
        finally:
            cleanup()


# ---------------------------------------------------------------------
# Hard-cap env vars (D1 plumbing -> D7 documented)
# ---------------------------------------------------------------------


class TestHardCapEnvVars:
    def test_pending_hard_cap_default(self):
        assert env_validator.int_var("DISPATCHER_MAX_PENDING_HARD_CAP") == 50_000

    def test_dlq_hard_cap_default(self):
        assert env_validator.int_var("DISPATCHER_MAX_DLQ_HARD_CAP") == 5_000

    def test_hard_cap_re_reads_on_every_call(self, monkeypatch):
        """V-217 #156 inheritance: hard caps are not frozen at
        import. A1's admin endpoint will read these on every
        admin POST/PATCH via int_var."""
        monkeypatch.setenv("DISPATCHER_MAX_PENDING_HARD_CAP", "12345")
        assert env_validator.int_var("DISPATCHER_MAX_PENDING_HARD_CAP") == 12345
        monkeypatch.setenv("DISPATCHER_MAX_PENDING_HARD_CAP", "67890")
        assert env_validator.int_var("DISPATCHER_MAX_PENDING_HARD_CAP") == 67890
