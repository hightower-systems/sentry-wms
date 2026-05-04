"""Tests for the v1.6.0 D6 retry schedule + DLQ transition (#178).

Plan sections 2.4 (retry schedule), 2.3 step 8 (failed branch),
1.6 (cursor advance on dlq).

Coverage:

  * RETRY_SCHEDULE_SECONDS matches the plan vector exactly.
  * retry_delay rejects out-of-range attempts and returns the
    documented values for 2..8.
  * is_terminal_attempt(8) is True; is_terminal_attempt(7) is
    False.
  * deliver_one with 500 for 3 attempts then 200 produces 4
    delivery rows attempt_number 1-4 with the schedule deltas.
  * deliver_one with 500 forever produces 8 delivery rows
    ending in dlq; cursor advances on the dlq write; no 9th
    row.
  * Retry slot's scheduled_at lands at NOW() + retry_delay
    within tolerance.

Tests that emit DB-driven retry sequences monkeypatch
RETRY_SCHEDULE_SECONDS to all-zero so the loop does not wait
wall-clock seconds for the slot to mature.
"""

import os
import sys
import time
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from services.webhook_dispatcher import dispatch as dispatch_module
from services.webhook_dispatcher import retry as retry_module

# Reuse helpers from the dispatch test suite.
from tests.test_webhook_dispatcher_dispatch import (  # noqa: E402
    StubHttpClient,
    _conn,
    _emit_event,
    _make_subscription,
    _wait_for_visible,
)


# ----------------------------------------------------------------------
# retry.py constants and pure functions
# ----------------------------------------------------------------------


class TestRetryScheduleConstant:
    def test_matches_plan_vector(self):
        """Plan §2.4 hard-codes
        ``[1s, 4s, 15s, 60s, 5m, 30m, 2h, 12h]``. A refactor
        that perturbed the schedule shifts the cumulative
        retry window away from ~15h, which the consumer
        integration guide will document."""
        assert retry_module.RETRY_SCHEDULE_SECONDS == (
            1, 4, 15, 60, 5 * 60, 30 * 60, 2 * 3600, 12 * 3600,
        )

    def test_max_attempts_is_eight(self):
        assert retry_module.MAX_ATTEMPTS == 8

    def test_cumulative_window_under_16_hours(self):
        """Plan §2.4 documents ~15h cumulative. Lock the bound
        loosely so a small re-tuning under 1h does not break
        the test, but a runaway addition (e.g., a 24h slot)
        does."""
        cumulative = sum(retry_module.RETRY_SCHEDULE_SECONDS)
        assert cumulative < 16 * 3600


class TestRetryDelay:
    @pytest.mark.parametrize(
        "next_attempt,base",
        [
            (2, 4),
            (3, 15),
            (4, 60),
            (5, 5 * 60),
            (6, 30 * 60),
            (7, 2 * 3600),
            (8, 12 * 3600),
        ],
    )
    def test_each_attempt_returns_value_in_jitter_band(self, next_attempt, base):
        """Delay before attempt N is RETRY_SCHEDULE_SECONDS[N-1]
        multiplied by a per-call jitter factor in [0.9, 1.1]
        (#234). The 12h slot at index 7 stays reachable via
        retry_delay(8); without it the cumulative retry window
        collapses from ~15h to ~2.6h, which would silently shrink
        consumers' incident-response budget."""
        # Truncation via int() means the lower bound for short
        # slots can come in one second under 0.9*base; widen the
        # bounds by 1s on each side so the assertion is stable.
        lo = int(0.9 * base) - 1
        hi = int(1.1 * base) + 1
        for _ in range(20):  # multiple draws to exercise the RNG path
            v = retry_module.retry_delay(next_attempt)
            assert lo <= v <= hi, (
                f"retry_delay({next_attempt}) = {v} outside "
                f"jitter band [{lo}, {hi}] for base {base}"
            )

    def test_attempt_one_raises(self):
        """Attempt 1 fires at NOW() with no retry delay; the
        helper rejects 1 explicitly so a caller cannot
        accidentally compute a delay for the initial attempt."""
        with pytest.raises(ValueError):
            retry_module.retry_delay(1)

    def test_attempt_nine_raises(self):
        """The 8th attempt is terminal; there is no 9th."""
        with pytest.raises(ValueError):
            retry_module.retry_delay(9)

    def test_zero_or_negative_raises(self):
        for n in (0, -1, -8):
            with pytest.raises(ValueError):
                retry_module.retry_delay(n)


class TestRetryJitter:
    """#234: each retry slot carries +/-10% jitter so multiple
    subscriptions whose first delivery to the same consumer URL
    fails at the same minute do not retry at the same minute
    every retry slot. The cumulative window stays inside the
    documented ~15h budget; the worst case (1.1 ** 7 * sum)
    is still under ~17h."""

    def test_jitter_produces_spread_across_calls(self):
        """1000 calls to retry_delay(2) must NOT all return the
        same value -- if they do, the jitter path is dead and
        synchronized retry storms still happen."""
        values = {retry_module.retry_delay(2) for _ in range(1000)}
        # base=4 with int truncation collapses to {3, 4} after
        # rounding; the count is small but must be > 1.
        assert len(values) > 1, (
            "retry_delay must produce more than one value across "
            "1000 calls; jitter path appears not to be wired"
        )

    def test_jitter_band_holds_for_long_slots(self):
        """A 5-minute slot lets us see a wider integer spread
        because int truncation no longer collapses the band into
        one or two bins. Confirms the bounds are honored across
        a representative sample."""
        base = 5 * 60
        lo = int(0.9 * base)
        hi = int(1.1 * base)
        for _ in range(200):
            v = retry_module.retry_delay(5)
            assert lo - 1 <= v <= hi + 1

    def test_cumulative_worst_case_under_seventeen_hours(self):
        """Even at the upper jitter bound applied to every slot,
        the cumulative retry window stays inside the relaxed
        bound the consumer-side budgeting docs reference."""
        worst = 1.1 * sum(retry_module.RETRY_SCHEDULE_SECONDS)
        assert worst < 17 * 3600

    def test_zero_base_returns_zero(self, monkeypatch):
        """Test fixtures monkey-patch RETRY_SCHEDULE_SECONDS to
        ``(0,) * 8`` to short-circuit retry waits in integration
        tests. The jitter floor must respect that contract:
        base=0 stays 0, not max(1, ...) which would slow the
        whole loop test suite by 8s per chained-retry test."""
        monkeypatch.setattr(retry_module, "RETRY_SCHEDULE_SECONDS", (0,) * 8)
        for n in range(2, retry_module.MAX_ATTEMPTS + 1):
            assert retry_module.retry_delay(n) == 0


class TestIsTerminalAttempt:
    def test_eight_is_terminal(self):
        assert retry_module.is_terminal_attempt(8) is True

    def test_seven_is_not_terminal(self):
        assert retry_module.is_terminal_attempt(7) is False

    def test_above_eight_is_terminal(self):
        """A defensive bound: if a refactor accidentally bumped
        attempt_number past MAX_ATTEMPTS, the dispatcher should
        treat it as terminal rather than overflow into a 9th
        attempt the schema CHECK would reject."""
        assert retry_module.is_terminal_attempt(9) is True


# ----------------------------------------------------------------------
# deliver_one retry behaviour against a real DB
# ----------------------------------------------------------------------


def _drain(sub_id: str, stub: StubHttpClient, max_iters: int = 16):
    """Run deliver_one until it returns None or the cap fires.
    Returns the list of outcomes the dispatcher produced."""
    outcomes = []
    conn = _conn()
    try:
        for _ in range(max_iters):
            outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            if outcome is None:
                break
            outcomes.append(outcome)
    finally:
        conn.close()
    return outcomes


def _delivery_rows(sub_id: str):
    """Return all webhook_deliveries rows for a subscription
    ordered by delivery_id."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT delivery_id, event_id, attempt_number, status,
                   scheduled_at, http_status, error_kind
              FROM webhook_deliveries
             WHERE subscription_id = %s
             ORDER BY delivery_id ASC
            """,
            (sub_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def _cursor_value(sub_id: str) -> int:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_delivered_event_id FROM webhook_subscriptions WHERE subscription_id = %s",
            (sub_id,),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def zero_retry_delays(monkeypatch):
    """Patch the schedule so retry slots are pickable
    immediately. Tests that exercise the full retry sequence
    rely on this; the schedule constants themselves are
    asserted by TestRetryScheduleConstant (which does NOT
    monkeypatch)."""
    monkeypatch.setattr(
        retry_module,
        "RETRY_SCHEDULE_SECONDS",
        (0, 0, 0, 0, 0, 0, 0, 0),
    )


class TestRetryThenSucceed:
    def test_500_three_times_then_200_produces_four_rows(self, zero_retry_delays):
        """Plan §2.3 step 8: each non-terminal failure inserts
        a fresh retry slot at attempt_number+1. After three
        failures and one success, there are exactly four rows
        with attempt_number 1, 2, 3, 4. The first three are in
        ``failed`` and the fourth is in ``succeeded``."""
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d6.retry.then.success")
            emitted.append(e1)
            _wait_for_visible(e1)

            stub = StubHttpClient(responses=[500, 500, 500, 200])
            outcomes = _drain(sub_id, stub)

            assert len(outcomes) == 4
            # Outcome objects don't carry attempt_number; verify
            # via DB rows below.

            rows = _delivery_rows(sub_id)
            assert len(rows) == 4
            assert [r[2] for r in rows] == [1, 2, 3, 4]
            assert [r[3] for r in rows] == ["failed", "failed", "failed", "succeeded"]
            assert _cursor_value(sub_id) == e1
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cleanup_conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cleanup_conn.close()


class TestEightFailuresEndInDLQ:
    def test_500_forever_produces_eight_rows_terminating_in_dlq(self, zero_retry_delays):
        """Plan §2.3 step 8 + plan §1.6: the 8th attempt's
        failure flips the existing row to ``dlq`` (no new row)
        AND advances the cursor. There are exactly 8 rows; the
        first 7 are ``failed``, the 8th is ``dlq``. The cursor
        moves to the event_id."""
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d6.eight.failures")
            emitted.append(e1)
            _wait_for_visible(e1)

            # Always-500 stub: any number of calls returns 500.
            class AlwaysFailing:
                def send(self, *a, **kw):
                    return dispatch_module.HttpResponse(
                        status_code=500, error_kind=None, error_detail=None
                    )

            outcomes = _drain(sub_id, AlwaysFailing(), max_iters=20)

            assert len(outcomes) == 8
            rows = _delivery_rows(sub_id)
            assert len(rows) == 8
            assert [r[2] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8]
            assert [r[3] for r in rows] == [
                "failed", "failed", "failed", "failed",
                "failed", "failed", "failed", "dlq",
            ]
            assert outcomes[-1].status == "dlq"
            assert outcomes[-1].terminal is True
            assert _cursor_value(sub_id) == e1, (
                "plan §1.6: dlq is a terminal state, cursor advances"
            )

            # No 9th row exists (attempt_number bound is 8 per
            # migration 030's CHECK constraint and MAX_ATTEMPTS).
            assert all(r[2] <= 8 for r in rows)
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cleanup_conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cleanup_conn.close()


class TestRetrySlotScheduledAtCorrectInterval:
    def test_retry_slot_scheduled_at_matches_schedule(self):
        """No monkeypatch here -- exercise the real schedule
        and confirm the scheduled_at on the inserted retry slot
        lands inside the jittered band around the second-slot
        4-second base. #234 jitters retry slots by +/-10%, so
        the dispatcher's retry_delay call inside deliver_one
        produces a value that is independent of any retry_delay
        the test calls -- the test must tolerate the full
        [0.9*base, 1.1*base] band rather than pin one value."""
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d6.retry.scheduled_at")
            emitted.append(e1)
            _wait_for_visible(e1)

            base_delay_s = retry_module.RETRY_SCHEDULE_SECONDS[1]
            assert base_delay_s == 4
            min_jittered = 0.9 * base_delay_s
            max_jittered = 1.1 * base_delay_s

            stub = StubHttpClient(responses=[500])
            conn = _conn()
            t_before = time.time()
            try:
                dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            t_after = time.time()

            rows = _delivery_rows(sub_id)
            assert len(rows) == 2  # the failed attempt 1 + the retry slot for attempt 2
            retry_slot = rows[1]
            assert retry_slot[2] == 2  # attempt_number = 2
            assert retry_slot[3] == "pending"
            # Tolerate the jitter band + DB clock drift on either side.
            scheduled_at_ts = retry_slot[4].timestamp()
            min_expected = t_before + min_jittered - 1.0
            max_expected = t_after + max_jittered + 1.0
            assert min_expected <= scheduled_at_ts <= max_expected, (
                f"retry slot scheduled_at={scheduled_at_ts} not within "
                f"jittered band [{min_expected}, {max_expected}]"
            )
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cleanup_conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cleanup_conn.close()


class TestRetrySlotBlocksFreshEvents:
    def test_future_scheduled_retry_slot_blocks_new_event_pickup(self):
        """Plan §2.5 head-of-line blocking under D6 semantics:
        a retry slot whose scheduled_at is still in the future
        prevents deliver_one from picking up a newer event past
        the cursor. The deliver_one back-off is via
        _has_non_terminal_delivery."""
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d6.hol.first")
            e2 = _emit_event(event_type="d6.hol.second")
            emitted.extend([e1, e2])
            _wait_for_visible(e2)

            stub = StubHttpClient(responses=[500])  # only one call expected
            conn = _conn()
            try:
                first = dispatch_module.deliver_one(conn, sub_id, stub)
                assert first is not None
                assert first.event_id == e1
                assert first.status == "failed"

                # Retry slot for e1 attempt=2 was inserted with
                # scheduled_at = NOW() + retry_delay(2) = NOW() +
                # 4s (real schedule). The next deliver_one call
                # sees no pending row under the time gate AND a
                # non-terminal row exists for the subscription
                # -> back off.
                second = dispatch_module.deliver_one(conn, sub_id, stub)
                assert second is None, (
                    "deliver_one must back off (no pending past time gate, "
                    "non-terminal row exists) rather than pick e2"
                )
            finally:
                conn.close()
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cleanup_conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cleanup_conn.close()
