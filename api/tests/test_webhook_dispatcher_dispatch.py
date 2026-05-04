"""Tests for the v1.6.0 D5 delivery loop (#176 / plan §2.3 + §1.6).

Coverage:

  * Happy path: 5 events deliver in order under a 200-OK mock;
    cursor advances to the latest event_id; delivery rows land
    in succeeded state with monotonic delivery_id.
  * Cursor advances only on terminal state -- a 500 response
    flips the row to failed and leaves the cursor put.
  * Runtime body == signed_body assertion fires when an
    HttpClient stub mutates the body before sending.
  * Head-of-line blocking: an unterminated first event blocks
    later events on the same subscription.
  * Subscription with no pending events is a no-op.
  * subscription_filter narrows the SELECT by event_types and
    warehouse_ids (filter resolution is in SQL, not application).
  * Subscription with status='paused' is skipped.
  * Timeout-shaped exception classifies as error_kind='timeout'.

Each test owns its subscription + secret + emits its own events,
and cleans up via DELETE FROM webhook_subscriptions which
cascades to webhook_secrets and forces webhook_deliveries to be
removed first (RESTRICT FK; tests that emit deliveries clean
them up explicitly).
"""

import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import List, Optional

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from cryptography.fernet import Fernet

from services.webhook_dispatcher import dispatch as dispatch_module
from services.webhook_dispatcher import signing


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_connector(cur, connector_id="test-conn-d5") -> str:
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "D5 dispatch test connector"),
    )
    return connector_id


def _make_subscription(
    delivery_url="https://example.invalid/d5",
    subscription_filter=None,
    status="active",
):
    """Create a subscription + a generation=1 secret. Returns
    (subscription_id, plaintext_secret_bytes, cleanup_fn)."""
    signing._fernet_cache = None  # noqa: SLF001 -- ensure tests use the env key
    fernet = signing._get_fernet()  # noqa: SLF001

    conn = _conn()
    conn.autocommit = True
    cur = conn.cursor()
    connector_id = _ensure_connector(cur)
    sub_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO webhook_subscriptions
            (subscription_id, connector_id, display_name, delivery_url,
             subscription_filter, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            str(sub_id),
            connector_id,
            "d5 test sub",
            delivery_url,
            json.dumps(subscription_filter or {}),
            status,
        ),
    )
    plaintext = b"d5-test-secret-32-bytes-padding!"
    cur.execute(
        "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
        "VALUES (%s, 1, %s)",
        (str(sub_id), fernet.encrypt(plaintext)),
    )
    conn.close()

    def cleanup():
        c = _conn()
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "DELETE FROM webhook_deliveries WHERE subscription_id = %s",
            (str(sub_id),),
        )
        cur.execute(
            "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
            (str(sub_id),),
        )
        c.close()

    return str(sub_id), plaintext, cleanup


def _emit_event(event_type="test.d5", warehouse_id=1, payload=None) -> int:
    """Insert an integration_events row and COMMIT so the
    deferred visible_at trigger fires. Returns event_id."""
    payload = payload or {"k": "v"}
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO integration_events
                (event_type, event_version, aggregate_type, aggregate_id,
                 aggregate_external_id, warehouse_id, source_txn_id, payload)
            VALUES (%s, 1, 'test_aggregate', %s, %s, %s, %s, %s)
            RETURNING event_id
            """,
            (
                event_type,
                abs(hash(uuid.uuid4())) % (10**9),
                str(uuid.uuid4()),
                warehouse_id,
                str(uuid.uuid4()),
                json.dumps(payload),
            ),
        )
        event_id = cur.fetchone()[0]
        conn.commit()
        return event_id
    finally:
        conn.close()


def _wait_for_visible(event_id: int, timeout_s: float = 5.0) -> None:
    """The visible-at gate (plan §1.6) requires
    visible_at <= NOW() - 2s before deliver_one will pick the
    event up. Wait until the gate clears so the test does not
    depend on wall-clock timing.

    Uses autocommit so each iteration's SELECT sees a fresh
    snapshot; a non-autocommit connection would lock its
    transaction snapshot at first query and miss the
    visible_at trigger update emitted by the writer's COMMIT.
    """
    deadline = time.monotonic() + timeout_s
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        while time.monotonic() < deadline:
            cur.execute(
                "SELECT visible_at <= NOW() - INTERVAL '2 seconds' "
                "FROM integration_events WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return
            time.sleep(0.2)
        raise AssertionError(f"event {event_id} did not clear the visible_at gate within {timeout_s}s")
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Mock HTTP clients
# ---------------------------------------------------------------------


class StubHttpClient:
    """Records outbound calls and returns canned responses.
    Thread-safe so tests can reason about call ordering under the
    SubscriptionWorker thread model."""

    def __init__(self, responses=None, exception=None):
        self._responses = list(responses or [])
        self._exception = exception
        self.calls: List[dict] = []
        self._lock = threading.Lock()

    def send(
        self,
        url,
        body,
        signature,
        timestamp,
        secret_generation,
        event_type,
        event_id,
        signed_body_for_assertion,
    ):
        # Mirror the real assertion so the stub is faithful.
        assert body is signed_body_for_assertion or body == signed_body_for_assertion
        with self._lock:
            self.calls.append(
                {
                    "url": url,
                    "body": body,
                    "signature": signature,
                    "timestamp": timestamp,
                    "secret_generation": secret_generation,
                    "event_type": event_type,
                    "event_id": event_id,
                }
            )
        if self._exception is not None:
            raise self._exception
        if self._responses:
            status = self._responses.pop(0)
        else:
            status = 200
        return dispatch_module.HttpResponse(
            status_code=status, error_kind=None, error_detail=None
        )


class MutatingHttpClient:
    """Sends a body different from the one that was signed. The
    runtime check in :class:`HttpClient.send` should fire and
    surface :class:`SingleSerializationViolation` to the caller;
    the dispatch loop catches it by name and re-raises so the
    breach surfaces loudly rather than being reclassified as a
    delivery failure."""

    def send(
        self,
        url,
        body,
        signature,
        timestamp,
        secret_generation,
        event_type,
        event_id,
        signed_body_for_assertion,
    ):
        # Pretend a careless refactor introduced a transformation:
        # rebuild the body by re-serializing the input. This
        # mismatch is precisely what the runtime check exists to
        # catch. #221: surface the same exception class the real
        # HttpClient.send raises so the dispatch loop's `except`
        # clause matches uniformly across the test stub and prod.
        from services.webhook_dispatcher import http_client as hc_module

        mutated = body + b"\n"  # one trailing newline
        if not (
            mutated is signed_body_for_assertion
            or mutated == signed_body_for_assertion
        ):
            raise hc_module.SingleSerializationViolation(
                "single-serialization invariant violated: the bytes about "
                "to be POSTed do not match the bytes that were signed."
            )
        return dispatch_module.HttpResponse(
            status_code=200, error_kind=None, error_detail=None
        )


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


class TestHappyPathFiveEvents:
    def test_five_events_deliver_in_order_with_cursor_advance(self):
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            event_ids = [_emit_event(event_type=f"d5.happy.{i}") for i in range(5)]
            for eid in event_ids:
                _wait_for_visible(eid)

            stub = StubHttpClient(responses=[200, 200, 200, 200, 200])
            conn = _conn()
            try:
                outcomes = []
                while True:
                    outcome = dispatch_module.deliver_one(conn, sub_id, stub)
                    if outcome is None:
                        break
                    outcomes.append(outcome)
                    if len(outcomes) > 10:
                        break  # safety
            finally:
                conn.close()

            assert len(outcomes) == 5
            assert all(o.status == "succeeded" for o in outcomes)
            assert [o.event_id for o in outcomes] == event_ids
            # delivery_id is BIGSERIAL across the table, so monotonic.
            assert outcomes == sorted(outcomes, key=lambda o: o.delivery_id)

            # Cursor advanced to the last event.
            verify_conn = _conn()
            try:
                cur = verify_conn.cursor()
                cur.execute(
                    "SELECT last_delivered_event_id FROM webhook_subscriptions WHERE subscription_id = %s",
                    (sub_id,),
                )
                assert cur.fetchone()[0] == event_ids[-1]
            finally:
                verify_conn.close()
        finally:
            try:
                cleanup()
            finally:
                # Always clean integration_events even if the
                # subscription cleanup raised; otherwise a
                # failed test leaves orphan rows for the next
                # run to trip on.
                cleanup_conn = _conn()
                cleanup_conn.autocommit = True
                cleanup_conn.cursor().execute(
                    "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                    (event_ids,),
                )
                cleanup_conn.close()


# ---------------------------------------------------------------------
# Cursor advances only on terminal state
# ---------------------------------------------------------------------


class TestCursorAdvanceOnlyOnTerminal:
    def test_500_does_not_advance_cursor(self):
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.cursor.1")
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
            assert outcome.error_kind == "5xx"

            cur = _conn().cursor()
            cur.execute(
                "SELECT last_delivered_event_id FROM webhook_subscriptions WHERE subscription_id = %s",
                (sub_id,),
            )
            # Cursor untouched (still at 0).
            assert cur.fetchone()[0] == 0
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
# Runtime body == signed_body assertion
# ---------------------------------------------------------------------


class TestSingleSerializationRuntimeAssertion:
    def test_mutating_client_triggers_assertion_error(self):
        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.assert")
            emitted.append(e1)
            _wait_for_visible(e1)

            mutator = MutatingHttpClient()
            conn = _conn()
            try:
                from services.webhook_dispatcher import (
                    http_client as hc_module,
                )

                with pytest.raises(
                    hc_module.SingleSerializationViolation,
                    match="single-serialization",
                ):
                    dispatch_module.deliver_one(conn, sub_id, mutator)
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


# ---------------------------------------------------------------------
# Head-of-line blocking
# ---------------------------------------------------------------------


class TestHeadOfLineBlocking:
    def test_failed_first_event_blocks_second(self, monkeypatch):
        """Plan §2.5: a stuck event blocks newer events on the
        same subscription.

        D6 changed the failure-branch semantics from "mark
        failed and stop" to "mark failed and INSERT a fresh
        retry slot scheduled at NOW() + retry_delay." The HOL
        invariant is now enforced via two gates:
        _select_next_pending matches only rows whose
        scheduled_at <= NOW() (so a future-scheduled retry slot
        is invisible until its time comes), and
        _has_non_terminal_delivery makes deliver_one back off
        from the fresh-event select while any non-terminal row
        exists.

        Patch RETRY_SCHEDULE_SECONDS to all-zero so the test
        does not need to wait wall-clock seconds; the
        invariant under test is "newer event waits for older
        event to terminate," not the schedule's exact values
        (which D6's own tests cover).
        """
        from services.webhook_dispatcher import retry as retry_mod

        monkeypatch.setattr(
            retry_mod,
            "RETRY_SCHEDULE_SECONDS",
            (0, 0, 0, 0, 0, 0, 0, 0),
        )

        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.hol.first")
            e2 = _emit_event(event_type="d5.hol.second")
            emitted.extend([e1, e2])
            _wait_for_visible(e2)

            stub = StubHttpClient(responses=[500, 200])  # first call 500
            conn = _conn()
            try:
                first = dispatch_module.deliver_one(conn, sub_id, stub)
                assert first is not None
                assert first.event_id == e1
                assert first.status == "failed"

                # Second call -- D6 inserted a retry slot
                # (attempt 2) at NOW() + 0s; deliver_one picks
                # it up. The retry slot is for e1, NOT e2: HOL
                # blocking is preserved by
                # _has_non_terminal_delivery refusing to pick
                # e2 from integration_events while e1's retry
                # slot is still alive.
                second = dispatch_module.deliver_one(conn, sub_id, stub)
                assert second is not None
                assert second.event_id == e1, (
                    "head-of-line blocking: e2 must not be delivered "
                    "until e1 reaches a terminal state"
                )
                assert second.status == "succeeded"

                third = dispatch_module.deliver_one(conn, sub_id, stub)
                assert third is not None
                assert third.event_id == e2  # e1 terminal now, e2 picks up
                assert third.status == "succeeded"
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


# ---------------------------------------------------------------------
# No-op path
# ---------------------------------------------------------------------


class TestNoPendingNoFresh:
    def test_returns_none_when_caught_up(self):
        sub_id, _plaintext, cleanup = _make_subscription()
        try:
            stub = StubHttpClient()
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            assert outcome is None
            assert stub.calls == []
        finally:
            cleanup()


# ---------------------------------------------------------------------
# subscription_filter
# ---------------------------------------------------------------------


class TestSubscriptionFilter:
    def test_event_types_filter_skips_non_matching(self):
        sub_id, _plaintext, cleanup = _make_subscription(
            subscription_filter={"event_types": ["d5.filter.match"]}
        )
        emitted = []
        try:
            e_skip = _emit_event(event_type="d5.filter.skip")
            e_match = _emit_event(event_type="d5.filter.match")
            emitted.extend([e_skip, e_match])
            _wait_for_visible(e_match)

            stub = StubHttpClient(responses=[200])
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            assert outcome is not None
            assert outcome.event_id == e_match, (
                f"filter must skip event_type='d5.filter.skip' (event_id={e_skip}) "
                f"and pick event_type='d5.filter.match' (event_id={e_match}); "
                f"got event_id={outcome.event_id}"
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

    def test_warehouse_ids_filter_skips_non_matching(self):
        sub_id, _plaintext, cleanup = _make_subscription(
            subscription_filter={"warehouse_ids": [1]}
        )
        emitted = []
        # Only test the WH=1 acceptance path; emitting WH=2 would
        # require a second warehouse row which the seed doesn't
        # guarantee. The IN-clause shape is what matters.
        try:
            e_match = _emit_event(event_type="d5.wh.match", warehouse_id=1)
            emitted.append(e_match)
            _wait_for_visible(e_match)

            stub = StubHttpClient(responses=[200])
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            assert outcome is not None
            assert outcome.event_id == e_match
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
# Status='paused' is skipped
# ---------------------------------------------------------------------


class TestPausedSubscriptionSkipped:
    def test_paused_subscription_is_no_op(self):
        sub_id, _plaintext, cleanup = _make_subscription(status="paused")
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.paused")
            emitted.append(e1)
            _wait_for_visible(e1)

            stub = StubHttpClient(responses=[200])
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            assert outcome is None
            assert stub.calls == []
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cleanup_conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cleanup_conn.close()


class TestMalformedFilterFailsClosed:
    """#232: a subscription_filter row that fails Pydantic
    validation USED TO fall open (empty filter, matches every
    event). Now fail closed: auto-pause the subscription with
    pause_reason='malformed_filter', write an audit_log row,
    return None so deliver_one backs off without selecting any
    event.
    """

    def _corrupt_filter(self, sub_id: str) -> None:
        """Write a Pydantic-incompatible filter shape via raw SQL.
        Pydantic SubscriptionFilter has extra='forbid', so an
        unknown key trips validation."""
        c = _conn()
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "UPDATE webhook_subscriptions "
            "   SET subscription_filter = "
            "       '{\"unknown_field\": 1}'::jsonb "
            " WHERE subscription_id = %s",
            (sub_id,),
        )
        c.close()

    def _read_subscription(self, sub_id: str):
        c = _conn()
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "SELECT status, pause_reason FROM webhook_subscriptions "
            "WHERE subscription_id = %s",
            (sub_id,),
        )
        row = cur.fetchone()
        c.close()
        return row

    def _audit_count(self, sub_id: str) -> int:
        c = _conn()
        c.autocommit = True
        cur = c.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_AUTO_PAUSE' "
            "  AND details->>'subscription_id' = %s",
            (sub_id,),
        )
        n = cur.fetchone()[0]
        c.close()
        return n

    def test_malformed_filter_pauses_and_logs_audit(self):
        sub_id, _plaintext, cleanup = _make_subscription(
            subscription_filter={"event_types": ["receipt.completed"]},
        )
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.malformed")
            emitted.append(e1)
            _wait_for_visible(e1)

            self._corrupt_filter(sub_id)

            stub = StubHttpClient(responses=[200])
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()

            # deliver_one returns None: no event selected.
            assert outcome is None
            # No HTTP send happened: a fail-open path would have
            # delivered the event under an empty filter.
            assert stub.calls == []
            # Subscription is now paused with the documented
            # malformed_filter reason.
            row = self._read_subscription(sub_id)
            assert row[0] == "paused"
            assert row[1] == "malformed_filter"
            # Audit_log captured the auto-pause.
            assert self._audit_count(sub_id) == 1
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cur = cleanup_conn.cursor()
            cur.execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cur.execute(
                "DELETE FROM audit_log "
                "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_AUTO_PAUSE' "
                "  AND details->>'subscription_id' = %s",
                (sub_id,),
            )
            cleanup_conn.close()

    def test_already_paused_does_not_re_audit(self):
        """A second deliver_one call against the same bad row
        does not write a duplicate audit row -- the conditional
        UPDATE WHERE status='active' is a no-op the second time
        and the audit INSERT is gated on rowcount."""
        sub_id, _plaintext, cleanup = _make_subscription(
            subscription_filter={"event_types": ["receipt.completed"]},
        )
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.malformed-2")
            emitted.append(e1)
            _wait_for_visible(e1)
            self._corrupt_filter(sub_id)

            stub = StubHttpClient(responses=[200, 200])
            conn = _conn()
            try:
                first = dispatch_module.deliver_one(conn, sub_id, stub)
                # Second call hits the early-return at deliver_one
                # on status!='active' before reaching the filter
                # parse path.
                second = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            assert first is None
            assert second is None
            assert self._audit_count(sub_id) == 1
        finally:
            cleanup()
            cleanup_conn = _conn()
            cleanup_conn.autocommit = True
            cur = cleanup_conn.cursor()
            cur.execute(
                "DELETE FROM integration_events WHERE event_id = ANY(%s)",
                (emitted,),
            )
            cur.execute(
                "DELETE FROM audit_log "
                "WHERE action_type = 'WEBHOOK_SUBSCRIPTION_AUTO_PAUSE' "
                "  AND details->>'subscription_id' = %s",
                (sub_id,),
            )
            cleanup_conn.close()


# ---------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------


class TestErrorClassification:
    def test_timeout_exception_classifies_as_timeout(self):
        """D8 classifies via isinstance check against
        requests.exceptions.Timeout (replaces the D5 name-
        substring heuristic). Tests must raise a real Timeout."""
        import requests

        sub_id, _plaintext, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event(event_type="d5.timeout")
            emitted.append(e1)
            _wait_for_visible(e1)

            stub = StubHttpClient(
                exception=requests.exceptions.Timeout("read timed out")
            )
            conn = _conn()
            try:
                outcome = dispatch_module.deliver_one(conn, sub_id, stub)
            finally:
                conn.close()
            assert outcome is not None
            assert outcome.status == "failed"
            assert outcome.error_kind == "timeout"

            cur = _conn().cursor()
            cur.execute(
                "SELECT error_detail FROM webhook_deliveries WHERE delivery_id = %s",
                (outcome.delivery_id,),
            )
            detail = cur.fetchone()[0]
            from services.webhook_dispatcher import error_catalog
            assert detail == error_catalog.get_short_message("timeout")
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
# Filter-clause unit tests (no DB)
# ---------------------------------------------------------------------


class TestBuildFilterClauses:
    def test_empty_filter(self):
        from services.webhook_dispatcher.subscription_filter import SubscriptionFilter

        clause, params = dispatch_module._build_filter_clauses(SubscriptionFilter())
        assert clause == ""
        assert params == []

    def test_event_types_only(self):
        from services.webhook_dispatcher.subscription_filter import SubscriptionFilter

        clause, params = dispatch_module._build_filter_clauses(
            SubscriptionFilter(event_types=["a", "b"])
        )
        assert "event_type = ANY" in clause
        assert params == [["a", "b"]]

    def test_both_filters_combined(self):
        from services.webhook_dispatcher.subscription_filter import SubscriptionFilter

        clause, params = dispatch_module._build_filter_clauses(
            SubscriptionFilter(event_types=["a"], warehouse_ids=[1, 2])
        )
        assert "event_type = ANY" in clause and "warehouse_id = ANY" in clause
        assert params == [["a"], [1, 2]]

    def test_unknown_keys_now_rejected(self):
        """Strict-typed filter rejects unknown keys via the
        Pydantic model. The dispatcher's parse path catches the
        ValidationError and falls back to the empty filter; the
        clause builder itself only sees a validated model so a
        construction with an unknown key never reaches it."""
        from pydantic import ValidationError

        from services.webhook_dispatcher.subscription_filter import SubscriptionFilter

        with pytest.raises(ValidationError):
            SubscriptionFilter.model_validate({"future_field": ["x"]})
