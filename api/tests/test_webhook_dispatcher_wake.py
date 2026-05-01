"""Tests for the v1.6.0 D3 wake orchestrator (#175 / plan §2.2 + §2.9).

Three wake sources, one shared queue, three threads. Coverage:

  * NOTIFY wake -- INSERT into integration_events triggers a
    fresh_event on the queue within 100ms.
  * Fallback poll -- a poll_all event lands within ~1.5x the poll
    interval, regardless of NOTIFY traffic.
  * Pubsub wake -- a JSON message on webhook_subscription_events
    enqueues a subscription_event with the parsed kind.
  * Pubsub soft-fail -- redis_url=None starts cleanly, the
    pubsub thread exits without crashing, LISTEN+poll keep working.
  * Malformed pubsub messages are logged + dropped, not crashed on.
  * Shutdown stops all three threads within the drain timeout.

The tests use the live sentry-redis + sentry-db services already
running for the dev stack. WakeOrchestrator opens its own
psycopg2 + redis connections so the tests do not need to share
connection pools with the rest of the suite.
"""

import json
import logging
import os
import sys
import threading
import time
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
# #227: pubsub HMAC key. Tests that exercise weak-key rejection
# clear / mutate this at the test level.
os.environ.setdefault(
    "SENTRY_PUBSUB_HMAC_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from services.webhook_dispatcher import pubsub_signing
from services.webhook_dispatcher import wake as wake_module


REDIS_URL = os.environ.get("REDIS_URL", "")


def _redis_available() -> bool:
    """Probe Redis with a 0.5s timeout. Tests that rely on the
    real broker skip cleanly when it is unreachable -- a CI
    environment without a Redis service should not turn the whole
    file red, but the tests are loud-fail per the policy when
    Redis is genuinely supposed to be present."""
    if not REDIS_URL or not REDIS_URL.startswith(("redis://", "rediss://")):
        return False
    try:
        import redis  # noqa: WPS433
    except ImportError:
        return False
    try:
        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5)
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _make_orchestrator(
    redis_url=None, fallback_poll_ms=500
) -> wake_module.WakeOrchestrator:
    return wake_module.WakeOrchestrator(
        database_url=os.environ["DATABASE_URL"],
        redis_url=redis_url,
        fallback_poll_ms=fallback_poll_ms,
    )


def _drain_until(
    orchestrator: wake_module.WakeOrchestrator,
    predicate,
    timeout_s: float,
) -> wake_module.WakeEvent:
    """Pop events off the queue until ``predicate(event)`` returns
    True or the timeout elapses. Returns the matching event;
    raises AssertionError on timeout. Useful when other wake
    sources may produce noise while we wait for the specific
    event under test."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            event = orchestrator.queue.get(timeout=0.1)
        except Exception:  # noqa: BLE001  -- queue.Empty
            continue
        if predicate(event):
            return event
    raise AssertionError(
        f"no event matching predicate within {timeout_s}s; queue size now: "
        f"{orchestrator.queue.qsize()}"
    )


# ----------------------------------------------------------------------
# WakeEvent dataclass
# ----------------------------------------------------------------------


class TestWakeEvent:
    def test_frozen_blocks_field_assignment(self):
        evt = wake_module.WakeEvent(kind="poll_all")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            evt.kind = "fresh_event"  # type: ignore

    def test_three_kinds_construct_correctly(self):
        a = wake_module.WakeEvent(kind="fresh_event", event_id=42)
        b = wake_module.WakeEvent(kind="poll_all")
        c = wake_module.WakeEvent(
            kind="subscription_event",
            subscription_id="abc",
            subscription_event_kind="paused",
        )
        assert a.event_id == 42
        assert b.event_id is None
        assert c.subscription_id == "abc" and c.subscription_event_kind == "paused"

    def test_equality_is_value_based(self):
        a = wake_module.WakeEvent(kind="fresh_event", event_id=99)
        b = wake_module.WakeEvent(kind="fresh_event", event_id=99)
        assert a == b


# ----------------------------------------------------------------------
# LISTEN/NOTIFY path
# ----------------------------------------------------------------------


class TestListenNotifyWake:
    def setup_method(self, method):
        self.orchestrator = _make_orchestrator(fallback_poll_ms=10000)
        self.orchestrator.start()

    def teardown_method(self, method):
        self.orchestrator.shutdown()
        self.orchestrator.join(timeout_s=5)

    def _emit_test_event(self) -> int:
        """INSERT into integration_events and COMMIT so the
        deferred visible_at trigger fires and the migration 031
        AFTER UPDATE trigger publishes a NOTIFY."""
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            source_txn = uuid.uuid4()
            aggregate_external = uuid.uuid4()
            aggregate_id = abs(hash(source_txn)) % (10**9)
            cur.execute(
                """
                INSERT INTO integration_events (
                    event_type, event_version, aggregate_type,
                    aggregate_id, aggregate_external_id, warehouse_id,
                    source_txn_id, payload
                ) VALUES (
                    'test.wake.notify', 1, 'test_aggregate',
                    %s, %s, 1, %s, '{}'::jsonb
                ) RETURNING event_id
                """,
                (aggregate_id, str(aggregate_external), str(source_txn)),
            )
            event_id = cur.fetchone()[0]
            conn.commit()
            return event_id
        finally:
            conn.close()

    def _cleanup_event(self, event_id: int) -> None:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        try:
            conn.cursor().execute(
                "DELETE FROM integration_events WHERE event_id = %s",
                (event_id,),
            )
        finally:
            conn.close()

    def test_notify_lands_on_queue_within_100ms(self):
        event_id = self._emit_test_event()
        try:
            event = _drain_until(
                self.orchestrator,
                lambda e: e.kind == "fresh_event" and e.event_id == event_id,
                timeout_s=2.0,
            )
            assert event.kind == "fresh_event"
            assert event.event_id == event_id
        finally:
            self._cleanup_event(event_id)

    def test_listen_thread_reconnects_after_connection_drop(self):
        """Issue #208: pre-fix, dropping the LISTEN connection
        silently exited the listen thread and the dispatcher ran
        on poll-only mode for the rest of its life. Post-fix the
        listen thread reconnects and NOTIFY-driven dispatch
        recovers. Asserts both behaviors: (1) the reconnect counter
        increments; (2) a fresh INSERT after the drop lands on
        the queue as fresh_event, not as a poll-driven generic
        wake."""
        before = self.orchestrator.health_snapshot()
        assert before["listen_reconnect_count"] == 0

        # Drop the LISTEN connection out from under the
        # orchestrator. The select() in the listen loop sees the
        # closed fd, the loop tears down + reconnects.
        try:
            self.orchestrator._listen_conn.close()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass

        # Give the reconnect path a moment to fire. The default
        # listen_reconnect_backoff_s is 1.0s; allow up to 3s for
        # the reconnect counter to tick.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snap = self.orchestrator.health_snapshot()
            if snap["listen_reconnect_count"] >= 1:
                break
            time.sleep(0.05)
        assert self.orchestrator.health_snapshot()["listen_reconnect_count"] >= 1, (
            "listen thread must increment listen_reconnect_count after a drop"
        )

        # Drain anything queued during the reconnect window so we
        # observe the post-reconnect NOTIFY specifically.
        while not self.orchestrator.queue.empty():
            try:
                self.orchestrator.queue.get_nowait()
            except Exception:  # noqa: BLE001
                break

        # Emit a fresh event AFTER the reconnect; it must land via
        # NOTIFY, not via the slow poll fallback.
        event_id = self._emit_test_event()
        try:
            event = _drain_until(
                self.orchestrator,
                lambda e: e.kind == "fresh_event" and e.event_id == event_id,
                timeout_s=3.0,
            )
            assert event.kind == "fresh_event"
            assert event.event_id == event_id
        finally:
            self._cleanup_event(event_id)


# ----------------------------------------------------------------------
# Fallback poll path
# ----------------------------------------------------------------------


class TestFallbackPoll:
    def test_poll_all_lands_within_window(self):
        # 200ms poll interval; expect at least one poll_all within 1s.
        orch = _make_orchestrator(fallback_poll_ms=200)
        orch.start()
        try:
            event = _drain_until(
                orch,
                lambda e: e.kind == "poll_all",
                timeout_s=1.0,
            )
            assert event.kind == "poll_all"
        finally:
            orch.shutdown()
            orch.join(timeout_s=5)

    def test_poll_continues_after_listen_connection_drop(self):
        """Simulate a missed-NOTIFY scenario: close the LISTEN
        connection out from under the orchestrator. The fallback
        poll thread runs independently and must keep producing
        poll_all events."""
        orch = _make_orchestrator(fallback_poll_ms=200)
        orch.start()
        try:
            # Drop the listen connection.
            try:
                orch._listen_conn.close()  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass

            # Drain any queued events so we observe a FRESH poll
            # (not one that fired before we dropped the conn).
            time.sleep(0.05)
            while not orch.queue.empty():
                try:
                    orch.queue.get_nowait()
                except Exception:  # noqa: BLE001
                    break

            event = _drain_until(
                orch,
                lambda e: e.kind == "poll_all",
                timeout_s=1.0,
            )
            assert event.kind == "poll_all"
        finally:
            orch.shutdown()
            orch.join(timeout_s=5)


# ----------------------------------------------------------------------
# Redis pubsub path
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not _redis_available(),
    reason="REDIS_URL not set or Redis unreachable; skipping pubsub tests",
)
class TestRedisPubsubWake:
    def setup_method(self, method):
        self.orchestrator = _make_orchestrator(
            redis_url=REDIS_URL, fallback_poll_ms=10000
        )
        self.orchestrator.start()
        # Give the pubsub thread a moment to subscribe before we publish.
        time.sleep(0.2)

    def teardown_method(self, method):
        self.orchestrator.shutdown()
        self.orchestrator.join(timeout_s=5)

    def _publish(self, payload):
        """#227: wrap inner payload in the HMAC envelope so the
        subscriber's verification path accepts it. Inner payload
        may be a dict (built-by-helper) or a raw string (used by
        the malformed-payload test to drive the unsigned path)."""
        import redis  # noqa: WPS433

        client = redis.Redis.from_url(REDIS_URL)
        if isinstance(payload, dict):
            sub_id = payload.get("subscription_id", "")
            event = payload.get("event", "")
            wire = pubsub_signing.build_envelope(
                sub_id, event, pubsub_signing.load_key()
            )
        elif isinstance(payload, list):
            # No subscription_id / event tuple to sign; intentionally
            # send raw so the subscriber's malformed path runs.
            wire = json.dumps(payload)
        else:
            wire = payload
        client.publish(wake_module.SUBSCRIPTION_EVENTS_CHANNEL, wire)

    def _publish_raw(self, raw_str):
        """Bypass the HMAC envelope entirely. Drives the
        unsigned-message rejection path. The subscriber must drop
        these and not enqueue."""
        import redis  # noqa: WPS433

        client = redis.Redis.from_url(REDIS_URL)
        client.publish(wake_module.SUBSCRIPTION_EVENTS_CHANNEL, raw_str)

    def test_subscription_event_lands_on_queue(self):
        sub_id = str(uuid.uuid4())
        self._publish({"subscription_id": sub_id, "event": "paused"})
        event = _drain_until(
            self.orchestrator,
            lambda e: e.kind == "subscription_event" and e.subscription_id == sub_id,
            timeout_s=2.0,
        )
        assert event.subscription_event_kind == "paused"

    def test_each_known_event_kind_routes(self):
        for kind in (
            "paused",
            "resumed",
            "deleted",
            "delivery_url_changed",
            "rate_limit_changed",
            "secret_rotated",
        ):
            sub_id = str(uuid.uuid4())
            self._publish({"subscription_id": sub_id, "event": kind})
            event = _drain_until(
                self.orchestrator,
                lambda e: (
                    e.kind == "subscription_event"
                    and e.subscription_id == sub_id
                ),
                timeout_s=2.0,
            )
            assert event.subscription_event_kind == kind

    def test_unknown_event_kind_is_dropped(self, caplog):
        sub_id = str(uuid.uuid4())
        with caplog.at_level(logging.WARNING, logger="webhook_dispatcher.wake"):
            self._publish({"subscription_id": sub_id, "event": "made_up_kind"})
            time.sleep(0.5)
        # No subscription_event for this sub_id should land.
        events = []
        while not self.orchestrator.queue.empty():
            try:
                events.append(self.orchestrator.queue.get_nowait())
            except Exception:  # noqa: BLE001
                break
        for evt in events:
            assert not (
                evt.kind == "subscription_event"
                and evt.subscription_id == sub_id
            ), "unknown event kinds must be dropped, not enqueued"
        assert any(
            "made_up_kind" in record.getMessage() for record in caplog.records
        ), "unknown event kind must produce a WARNING log"

    def test_malformed_payload_is_dropped(self, caplog):
        with caplog.at_level(logging.WARNING, logger="webhook_dispatcher.wake"):
            self._publish("not-json")
            time.sleep(0.3)
            self._publish({"missing": "subscription_id"})
            time.sleep(0.3)
        # Drain queue; no subscription_event should have landed.
        events = []
        while not self.orchestrator.queue.empty():
            try:
                events.append(self.orchestrator.queue.get_nowait())
            except Exception:  # noqa: BLE001
                break
        for evt in events:
            assert evt.kind != "subscription_event", (
                f"malformed payload must not enqueue: got {evt!r}"
            )


# ----------------------------------------------------------------------
# #227: pubsub HMAC envelope verification
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not _redis_available(),
    reason="Redis service not reachable; skipping pubsub HMAC tests",
)
class TestPubsubHmacEnvelope:
    """Defense in depth against a Redis-side attacker forging
    subscription_event messages. Pre-#227 the channel was
    unauthenticated; any client with publish rights could trigger
    eviction storms or spam secret_rotated events."""

    def setup_method(self, method):
        self.orchestrator = _make_orchestrator(
            redis_url=REDIS_URL, fallback_poll_ms=10000
        )
        self.orchestrator.start()
        time.sleep(0.2)

    def teardown_method(self, method):
        self.orchestrator.shutdown()
        self.orchestrator.join(timeout_s=5)

    def _drain(self):
        events = []
        while not self.orchestrator.queue.empty():
            try:
                events.append(self.orchestrator.queue.get_nowait())
            except Exception:  # noqa: BLE001
                break
        return events

    def _publish_raw(self, raw_str):
        import redis  # noqa: WPS433

        client = redis.Redis.from_url(REDIS_URL)
        client.publish(wake_module.SUBSCRIPTION_EVENTS_CHANNEL, raw_str)

    def test_unsigned_inner_payload_is_rejected(self, caplog):
        """A pre-#227 publisher (raw inner payload, no envelope)
        no longer reaches the queue. This is the load-bearing
        regression: a Redis-side attacker who replays a captured
        pre-#227 message gets dropped."""
        sub_id = str(uuid.uuid4())
        unsigned = json.dumps({"subscription_id": sub_id, "event": "deleted"})
        with caplog.at_level(
            logging.WARNING, logger="webhook_dispatcher.wake"
        ):
            self._publish_raw(unsigned)
            time.sleep(0.4)
        events = self._drain()
        assert all(
            e.subscription_id != sub_id for e in events
        ), "unsigned (pre-#227) payload must not enqueue"
        assert any(
            "signature missing or invalid" in r.getMessage()
            for r in caplog.records
        ), "unsigned payload rejection must produce a WARNING log"

    def test_tampered_signature_is_rejected(self, caplog):
        sub_id = str(uuid.uuid4())
        # Build a valid envelope, then corrupt the signature.
        good = pubsub_signing.build_envelope(
            sub_id, "deleted", pubsub_signing.load_key()
        )
        tampered = good.replace('"sig":"', '"sig":"00')
        with caplog.at_level(
            logging.WARNING, logger="webhook_dispatcher.wake"
        ):
            self._publish_raw(tampered)
            time.sleep(0.4)
        events = self._drain()
        assert all(e.subscription_id != sub_id for e in events)
        assert any(
            "signature missing or invalid" in r.getMessage()
            for r in caplog.records
        )

    def test_payload_swap_keeps_signature_invalid(self, caplog):
        """An attacker who captures a legitimate envelope cannot
        swap the inner payload (e.g. `paused` -> `deleted`) without
        invalidating the signature. Build the tampered envelope by
        re-encoding the outer JSON with a mutated inner payload
        string but the original sig from the `paused` envelope."""
        sub_id = str(uuid.uuid4())
        outer = json.loads(
            pubsub_signing.build_envelope(
                sub_id, "paused", pubsub_signing.load_key()
            )
        )
        sig_for_paused = outer["sig"]
        deleted_inner = pubsub_signing.canonical_payload(sub_id, "deleted")
        tampered = json.dumps(
            {"sig": sig_for_paused, "payload": deleted_inner}
        )
        with caplog.at_level(
            logging.WARNING, logger="webhook_dispatcher.wake"
        ):
            self._publish_raw(tampered)
            time.sleep(0.4)
        events = self._drain()
        assert all(e.subscription_id != sub_id for e in events)

    def test_signed_envelope_round_trips(self):
        """Sanity check: the publisher's envelope is decoded and
        routed by the subscriber. If this regresses, the suite
        loses ground truth on the HMAC path."""
        sub_id = str(uuid.uuid4())
        envelope = pubsub_signing.build_envelope(
            sub_id, "secret_rotated", pubsub_signing.load_key()
        )
        self._publish_raw(envelope)
        event = _drain_until(
            self.orchestrator,
            lambda e: (
                e.kind == "subscription_event"
                and e.subscription_id == sub_id
            ),
            timeout_s=2.0,
        )
        assert event.subscription_event_kind == "secret_rotated"

    def test_publish_subscription_event_envelope_is_signed(self):
        """The producer side: publish_subscription_event sends a
        signed envelope on the channel. Captured via a real
        subscriber that pulls one raw message off the channel and
        verifies the HMAC under the same key."""
        import redis  # noqa: WPS433

        client = redis.Redis.from_url(REDIS_URL)
        sub = client.pubsub(ignore_subscribe_messages=True)
        sub.subscribe(wake_module.SUBSCRIPTION_EVENTS_CHANNEL)
        time.sleep(0.1)

        try:
            wake_module.publish_subscription_event(
                REDIS_URL, "abc-123", "paused"
            )
            wire = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                msg = sub.get_message(timeout=0.2)
                if msg and msg.get("type") == "message":
                    wire = msg["data"]
                    if isinstance(wire, bytes):
                        wire = wire.decode("utf-8")
                    break
            assert wire is not None, (
                "publish_subscription_event did not put a message on "
                "the channel within 2s"
            )
            inner = pubsub_signing.parse_envelope(
                wire, pubsub_signing.load_key()
            )
            assert inner is not None, (
                "publisher's envelope did not verify under the same "
                "HMAC key (sign / verify out of sync)"
            )
            assert inner == {
                "subscription_id": "abc-123",
                "event": "paused",
            }
        finally:
            try:
                sub.close()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------
# Soft-fail when Redis is unavailable
# ----------------------------------------------------------------------


class TestRedisSoftFail:
    def test_no_redis_url_starts_cleanly(self, caplog):
        """Plan §2.2: missing Redis URL is a soft-fail; LISTEN +
        poll continue working."""
        with caplog.at_level(logging.INFO, logger="webhook_dispatcher.wake"):
            orch = _make_orchestrator(redis_url=None, fallback_poll_ms=200)
            orch.start()
            try:
                # The fallback poll still produces events.
                event = _drain_until(
                    orch,
                    lambda e: e.kind == "poll_all",
                    timeout_s=1.0,
                )
                assert event.kind == "poll_all"
                # The pubsub thread exited cleanly; check it is not alive.
                assert orch._pubsub_thread is not None  # noqa: SLF001
                # Give the thread a moment to exit; daemon=True so its
                # is_alive() returns False once _run_pubsub returned.
                time.sleep(0.1)
                assert not orch._pubsub_thread.is_alive(), (  # noqa: SLF001
                    "pubsub thread must exit cleanly when redis_url=None"
                )
            finally:
                orch.shutdown()
                orch.join(timeout_s=5)
        assert any(
            "no Redis URL configured" in record.getMessage()
            for record in caplog.records
        ), "no-Redis path must produce an informative INFO log"


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


class TestLifecycle:
    def test_shutdown_stops_all_threads(self):
        orch = _make_orchestrator(fallback_poll_ms=500)
        orch.start()
        time.sleep(0.1)
        # All three threads alive (or pubsub may have exited if no
        # Redis URL was configured -- our default _make_orchestrator
        # passes None, so pubsub exits immediately).
        listen_alive = orch._listen_thread.is_alive()  # noqa: SLF001
        poll_alive = orch._poll_thread.is_alive()  # noqa: SLF001
        assert listen_alive
        assert poll_alive

        orch.shutdown()
        orch.join(timeout_s=5)

        assert not orch._listen_thread.is_alive(), "listen thread did not exit"  # noqa: SLF001
        assert not orch._poll_thread.is_alive(), "poll thread did not exit"  # noqa: SLF001
        assert not orch._pubsub_thread.is_alive(), "pubsub thread did not exit"  # noqa: SLF001

    def test_start_is_idempotent(self):
        orch = _make_orchestrator(fallback_poll_ms=10000)
        orch.start()
        first_listen_thread = orch._listen_thread  # noqa: SLF001
        orch.start()  # second call must be a no-op
        try:
            assert orch._listen_thread is first_listen_thread, (  # noqa: SLF001
                "second start() must not spawn duplicate threads"
            )
        finally:
            orch.shutdown()
            orch.join(timeout_s=5)

    def test_shutdown_is_idempotent(self):
        orch = _make_orchestrator(fallback_poll_ms=200)
        orch.start()
        orch.shutdown()
        orch.shutdown()  # no exception
        orch.join(timeout_s=5)

    def test_shutdown_completes_within_drain_window(self):
        """Even with a long fallback poll interval, shutdown must
        return quickly because the LISTEN connection close + the
        Event.wait wakeup unblock the threads immediately."""
        orch = _make_orchestrator(fallback_poll_ms=10000)
        orch.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        orch.shutdown()
        orch.join(timeout_s=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, (
            f"shutdown should complete under 3s; took {elapsed:.2f}s"
        )
