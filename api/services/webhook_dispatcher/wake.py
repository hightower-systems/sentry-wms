"""Wake orchestrator: LISTEN/NOTIFY + 2s fallback poll + Redis pubsub.

Three sources merge into one in-process queue per plan section 2.2:

  1. **LISTEN/NOTIFY** on ``integration_events_visible`` (migration
     031, #164). Sub-millisecond wake when an emit-site INSERT
     transitions ``visible_at`` from NULL -> NOT NULL. Each notify
     enqueues a :class:`WakeEvent` of kind ``"fresh_event"``
     carrying the ``event_id``.

  2. **Fallback poll** every ``DISPATCHER_FALLBACK_POLL_MS``.
     Catches NOTIFYs missed across a listener disconnect (NOTIFY
     is not durable). Each tick enqueues a ``"poll_all"`` event;
     the consumer (D5) treats this as "scan every active
     subscription for pending work."

  3. **Redis pubsub** on the ``webhook_subscription_events``
     channel for cross-worker invalidation per plan section 2.9.
     Mirrors the V-205 #146 ``token_cache`` subscriber shape:
     lazy ``import redis``, soft-fail when Redis is unavailable
     (LISTEN+poll continue working), and a daemon thread that
     parses payloads into ``"subscription_event"`` queue entries.

Lifecycle is owned by :class:`WakeOrchestrator`:

  * ``start()`` opens the LISTEN connection + spawns three daemon
    threads.
  * ``shutdown()`` sets the shared shutdown event AND closes the
    LISTEN connection + Redis pubsub so the threads break out of
    their blocking I/O calls instead of waiting for their
    select-timeout cycle.
  * ``join(timeout_s)`` waits for the threads; soft-fails (logs)
    if any do not exit within the window so a wedged thread is
    visible in shutdown logs but does not block the daemon's
    process exit.

The dispatch loop body that drains the queue (D5) is intentionally
left out of this commit; the queue is the boundary between D3
(producer side) and D5 (consumer side). D1's heartbeat-only main
loop drains and DEBUG-logs queue entries during D3 so the queue
does not grow unbounded; D5 replaces that drain with the real
per-subscription delivery loop.
"""

import json
import logging
import select
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Optional


LOGGER = logging.getLogger("webhook_dispatcher.wake")

# Channel names match plan §2.2 (LISTEN target) and §2.9 (Redis
# pubsub target). Pinned here so D5 / admin / migration code can
# import the same constants and a future rename is single-commit.
INTEGRATION_EVENTS_CHANNEL = "integration_events_visible"
SUBSCRIPTION_EVENTS_CHANNEL = "webhook_subscription_events"

# Plan §2.9 enumerates the six cross-worker action-table events.
# The wake module accepts any string here; D5 (or D9 for rate
# limit) is what binds each event to a behavior. Keeping the set
# closed at the dataclass level would couple D3 to the consumer's
# semantics; keeping it open keeps the producer agnostic.
_VALID_SUBSCRIPTION_EVENT_KINDS = frozenset({
    "paused",
    "resumed",
    "deleted",
    "delivery_url_changed",
    "rate_limit_changed",
    "secret_rotated",
})


def publish_subscription_event(
    redis_url: Optional[str], subscription_id: str, event: str
) -> None:
    """Publish a cross-worker invalidation message on the
    SUBSCRIPTION_EVENTS_CHANNEL. Mirrors the V-205 token_cache
    publisher shape: lazy import, soft-fail on missing Redis or
    connection error so a publisher failure cannot block the
    dispatcher's main path.

    Plan §2.9: peer workers act on the message per the action
    table. The dispatcher (D7) calls this from auto-pause; the
    admin endpoints (A1/A2) call it on every paused / resumed /
    deleted / delivery_url_changed / rate_limit_changed /
    secret_rotated mutation.

    Lives in wake.py (rather than dispatch.py) so the
    cross-worker pubsub plumbing -- subscribe (already here)
    plus publish (this function) -- is colocated. Also keeps
    the D2 single-serialization lint from catching a second
    json.dumps in dispatch.py: that lint protects the envelope
    sign-and-send path; the pubsub payload is unrelated.
    """
    if not redis_url or not redis_url.startswith(("redis://", "rediss://")):
        return
    try:
        import redis  # noqa: WPS433
        client = redis.Redis.from_url(redis_url)
        client.publish(
            SUBSCRIPTION_EVENTS_CHANNEL,
            json.dumps({"subscription_id": subscription_id, "event": event}),
        )
    except Exception:  # noqa: BLE001
        LOGGER.warning(
            "failed to publish %s event for subscription %s on Redis; "
            "peer workers will observe the change on the next 60s "
            "refresh cycle",
            event,
            subscription_id,
        )


@dataclass(frozen=True)
class WakeEvent:
    """One of three discriminated kinds. Only the fields valid for
    the kind are populated; consumers MUST switch on ``kind`` and
    not assume which optional fields are set.

    Frozen so a consumer cannot accidentally mutate an event after
    it has been enqueued. ``eq=True`` (the dataclass default) is
    acceptable here -- this is routing metadata, not security-
    relevant material; no constant-time comparison concern.
    """

    kind: str  # "fresh_event" | "poll_all" | "subscription_event"
    event_id: Optional[int] = None
    subscription_id: Optional[str] = None
    subscription_event_kind: Optional[str] = None


class WakeOrchestrator:
    """Owns the three wake threads and the shared queue.

    The orchestrator is intentionally passive about consumption:
    it produces events into ``self.queue`` and the consumer (D5)
    drains them. ``start()`` is idempotent so a test fixture that
    calls it twice does not spawn duplicate threads.

    Shutdown is two-step: ``shutdown()`` sets the shared event
    AND closes the I/O resources the threads block on, so each
    thread exits within one select-timeout cycle (1s for LISTEN,
    immediate for the fallback poll's ``Event.wait``, immediate
    for Redis pubsub via ``pubsub.close()``). ``join()`` then
    waits up to a configured timeout.
    """

    def __init__(
        self,
        database_url: str,
        redis_url: Optional[str],
        fallback_poll_ms: int,
        queue_maxsize: int = 0,
    ):
        self.database_url = database_url
        self.redis_url = redis_url
        self.fallback_poll_s = fallback_poll_ms / 1000.0
        self.queue: "Queue[WakeEvent]" = Queue(maxsize=queue_maxsize)
        self._shutdown = threading.Event()
        self._listen_conn = None
        self._pubsub = None
        self._listen_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._pubsub_thread: Optional[threading.Thread] = None
        self._started = False
        self._lock = threading.Lock()

    # -- Lifecycle ----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._open_listen_connection()
            self._listen_thread = self._spawn(self._run_listen, "wake-listen")
            self._poll_thread = self._spawn(self._run_fallback_poll, "wake-poll")
            self._pubsub_thread = self._spawn(self._run_pubsub, "wake-pubsub")
            self._started = True
            LOGGER.info(
                "wake orchestrator started (listen=%s, poll=%.2fs, redis=%s)",
                INTEGRATION_EVENTS_CHANNEL,
                self.fallback_poll_s,
                "on" if self.redis_url else "off",
            )

    def shutdown(self) -> None:
        """Request shutdown. Idempotent. Closes the blocking I/O
        resources so the threads wake immediately; the actual
        thread joining happens in :meth:`join`."""
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        # Closing the LISTEN connection breaks the select() in
        # _run_listen; closing the pubsub breaks pubsub.listen()
        # in _run_pubsub. The fallback poll thread waits on
        # _shutdown.wait() so it is already unblocked.
        try:
            if self._listen_conn is not None:
                self._listen_conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pubsub is not None:
                self._pubsub.close()
        except Exception:  # noqa: BLE001
            pass

    def join(self, timeout_s: float = 5.0) -> None:
        """Wait for all three threads to exit. Soft-fails on
        timeout so a wedged thread is visible but not blocking."""
        for thread in (
            self._listen_thread,
            self._poll_thread,
            self._pubsub_thread,
        ):
            if thread is None:
                continue
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                LOGGER.warning(
                    "wake thread %s did not exit within %.1fs of shutdown; "
                    "process exit will proceed but the thread is leaked",
                    thread.name,
                    timeout_s,
                )

    # -- Producers ---------------------------------------------------

    def _spawn(self, target, name: str) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=True, name=name)
        thread.start()
        return thread

    def _open_listen_connection(self) -> None:
        """Open the dedicated psycopg2 connection used by
        :meth:`_run_listen`. Autocommit because LISTEN must be
        outside a transaction. Localised import keeps this file
        cheap to import in tests that do not exercise the live
        wake path."""
        import psycopg2  # noqa: WPS433 -- localised import

        self._listen_conn = psycopg2.connect(self.database_url)
        self._listen_conn.autocommit = True
        cur = self._listen_conn.cursor()
        cur.execute(f"LISTEN {INTEGRATION_EVENTS_CHANNEL}")
        cur.close()

    def _run_listen(self) -> None:
        """LISTEN thread. Blocks on select() with a 1s timeout so
        a shutdown event is observed within one cycle. Mirrors the
        ``snapshot_keeper._drain_notifications`` pattern.

        On a connection error (typical shape: shutdown closed the
        connection out from under us) the loop exits cleanly; the
        fallback poll continues to wake the dispatcher in the
        meantime, and a future re-init can re-open the LISTEN
        connection if needed.
        """
        try:
            while not self._shutdown.is_set():
                conn = self._listen_conn
                if conn is None:
                    # Shutdown set the connection to None or close()
                    # raised. Exit cleanly.
                    return
                try:
                    rlist, _, _ = select.select([conn], [], [], 1.0)
                except (ValueError, OSError):
                    # Connection closed under us (shutdown path).
                    return
                if not rlist:
                    continue
                try:
                    conn.poll()
                except Exception:  # noqa: BLE001
                    return
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    if notify.channel != INTEGRATION_EVENTS_CHANNEL:
                        continue
                    try:
                        event_id = int(notify.payload)
                    except (TypeError, ValueError):
                        LOGGER.warning(
                            "wake: malformed NOTIFY payload on %s ignored: %r",
                            INTEGRATION_EVENTS_CHANNEL,
                            notify.payload,
                        )
                        continue
                    self.queue.put(
                        WakeEvent(kind="fresh_event", event_id=event_id)
                    )
        except Exception as exc:  # noqa: BLE001
            LOGGER.info(
                "wake listen thread exiting (%s: %s)",
                type(exc).__name__,
                exc,
            )

    def _run_fallback_poll(self) -> None:
        """Fallback poll thread. ``Event.wait`` sleeps until the
        timeout OR the shutdown event fires; the second condition
        means SIGTERM wakes the thread immediately rather than
        waiting up to ``fallback_poll_s`` seconds."""
        while True:
            timed_out = not self._shutdown.wait(timeout=self.fallback_poll_s)
            if not timed_out:
                # Shutdown fired; exit.
                return
            # Re-check after the wait in case shutdown raced the
            # timeout.
            if self._shutdown.is_set():
                return
            self.queue.put(WakeEvent(kind="poll_all"))

    def _run_pubsub(self) -> None:
        """Redis pubsub thread. Soft-fail mirrors the V-205 #146
        ``token_cache`` subscriber: missing Redis URL or import
        error logs a clear message and exits the thread; the rest
        of the orchestrator continues on LISTEN+poll alone."""
        if not self.redis_url or not self.redis_url.startswith(
            ("redis://", "rediss://")
        ):
            LOGGER.info(
                "wake: no Redis URL configured; cross-worker invalidation "
                "disabled (LISTEN+poll continue working)"
            )
            return

        try:
            import redis  # noqa: WPS433 -- localised import
        except ImportError:
            LOGGER.warning(
                "wake: redis package unavailable; cross-worker invalidation "
                "disabled (LISTEN+poll continue working)"
            )
            return

        try:
            client = redis.Redis.from_url(self.redis_url)
            self._pubsub = client.pubsub(ignore_subscribe_messages=True)
            self._pubsub.subscribe(SUBSCRIPTION_EVENTS_CHANNEL)
        except Exception:  # noqa: BLE001
            LOGGER.warning(
                "wake: failed to open Redis pubsub on %s; cross-worker "
                "invalidation disabled",
                SUBSCRIPTION_EVENTS_CHANNEL,
            )
            self._pubsub = None
            return

        LOGGER.info(
            "wake: Redis pubsub subscribed on %s",
            SUBSCRIPTION_EVENTS_CHANNEL,
        )

        try:
            for message in self._pubsub.listen():
                if self._shutdown.is_set():
                    return
                if not message or message.get("type") != "message":
                    continue
                self._handle_pubsub_message(message)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info(
                "wake: pubsub thread exiting (%s: %s)",
                type(exc).__name__,
                exc,
            )

    def _handle_pubsub_message(self, message: Any) -> None:
        """Parse one pubsub message and enqueue. Logs and drops
        malformed payloads so a misconfigured publisher does not
        wedge the subscriber thread.

        Expected shape (D4 publishes this from admin endpoints):

            {"subscription_id": "<uuid>", "event": "<kind>"}
        """
        payload = message.get("data")
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                LOGGER.warning(
                    "wake: pubsub message has non-utf-8 payload; ignored"
                )
                return
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            LOGGER.warning(
                "wake: pubsub message is not valid JSON; ignored: %r",
                payload,
            )
            return
        if not isinstance(data, dict):
            LOGGER.warning(
                "wake: pubsub message is not a JSON object; ignored: %r",
                data,
            )
            return
        sub_id = data.get("subscription_id")
        event_kind = data.get("event")
        if not sub_id or not isinstance(sub_id, str):
            LOGGER.warning(
                "wake: pubsub message missing subscription_id; ignored"
            )
            return
        if event_kind not in _VALID_SUBSCRIPTION_EVENT_KINDS:
            LOGGER.warning(
                "wake: pubsub message has unknown event %r; ignored "
                "(known: %s)",
                event_kind,
                sorted(_VALID_SUBSCRIPTION_EVENT_KINDS),
            )
            return
        self.queue.put(
            WakeEvent(
                kind="subscription_event",
                subscription_id=sub_id,
                subscription_event_kind=event_kind,
            )
        )
