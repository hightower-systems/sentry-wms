"""Per-subscription delivery loop (plan §2.3 + §1.6).

D5 fills in the dispatch surface: read pending rows from
``webhook_deliveries``, sign via :mod:`signing`, POST to the
consumer URL, update state. The single-serialization invariant
(plan §3.1) gets its runtime assertion here at the HTTP-client
boundary -- the bytes passed to the HTTP client MUST equal
``signed.body`` produced by :func:`signing.sign_request`. A
refactor that introduces a transformation between sign and send
surfaces as ``AssertionError``.

Cursor semantics (plan §1.6): ``webhook_subscriptions
.last_delivered_event_id`` advances strictly on terminal state
(``succeeded`` or ``dlq``), never on in-progress (``pending`` /
``in_flight``) or non-terminal failures (``failed``). D5 covers
``succeeded``; D6 covers the ``dlq`` flip.

Head-of-line blocking (plan §2.5): a stuck or retrying event
blocks newer events on the same subscription. The cursor is the
mechanism: D5 selects the next event ONLY after the previous
event terminated. The test suite locks this invariant; an
operational consequence (auto-pause at ceilings) lands in D7.

Consumer dedupe contract (plan §3.1): the dedupe key is
``event_id`` from the envelope BODY, not the
``X-Sentry-Delivery-Id`` header. The header is a composite
``f"{event_id}:{timestamp}"`` scoped to a specific delivery
attempt; a consumer that dedupes on the header would mistake
two retries of the same event for two distinct events. The
runbook (R3) and the consumer integration guide
(``docs/api/webhooks.md``) state this explicitly.

D5 explicitly does NOT include:
  * Retry schedule (D6) -- D5 marks failures as ``failed`` and
    leaves the next-attempt scheduling to D6.
  * Ceiling auto-pause (D7) -- D5 ignores ``status='paused'``
    subscriptions but does not flip them.
  * HTTP client policy (D8) -- D5 uses a placeholder
    ``requests.post`` invocation with ``verify=True`` and
    ``timeout=10`` baked in; D8 replaces with a full Session
    factory + connection pool + error-kind classification.
  * Rate limiter (D9), graceful shutdown drain (D10), SSRF
    guard (D11), strict-typed filter (D12), cross-worker
    publisher (D4).
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Dict, Mapping, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

from . import envelope as envelope_module
from . import signing


LOGGER = logging.getLogger("webhook_dispatcher.dispatch")


# Default HTTP timeout for the placeholder client. D8 will pull
# this from DISPATCHER_HTTP_TIMEOUT_MS via env_validator.int_var
# and replace the placeholder with a real Session factory.
_DEFAULT_HTTP_TIMEOUT_S = 10.0

# Visible-at gate matches plan §1.6: events less than 2s old are
# considered in-flight (transaction may not have committed across
# all sessions yet). The 2-second buffer tolerates the gap
# between the deferred trigger firing and the COMMIT becoming
# visible to a separate session.
_VISIBLE_AT_BUFFER_S = 2

# Subscription-list refresh cadence: plan §2.1 specifies "one
# worker thread per active subscription, refreshed every 60s".
_SUBSCRIPTION_REFRESH_S = 60.0


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of a single deliver_one cycle. ``terminal`` is True
    when the cycle ended in ``succeeded`` (or, in D6, ``dlq``),
    so the orchestrator knows whether to advance the per-
    subscription wake state."""

    delivery_id: int
    event_id: int
    status: str  # "succeeded" | "failed"
    http_status: Optional[int]
    error_kind: Optional[str]
    terminal: bool


# ---------------------------------------------------------------------
# HTTP client (placeholder; D8 replaces)
# ---------------------------------------------------------------------


class HttpClient:
    """Placeholder synchronous HTTP client. D8 replaces this with
    a full ``requests.Session`` factory + connection pool +
    error-kind classification + ``allow_redirects=False``.

    The ``send`` method takes a pre-built body bytes object
    explicitly (rather than the envelope dict) because the
    runtime ``request_body == signed_body`` assertion lives at
    THIS boundary -- the caller serializes + signs once, hands
    the bytes here, and the assertion fires inside :meth:`send`
    if the bytes were transformed in flight.
    """

    def __init__(self, timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S):
        self.timeout_s = timeout_s

    def send(
        self,
        url: str,
        body: bytes,
        signature: str,
        timestamp: int,
        secret_generation: int,
        event_type: str,
        event_id: int,
        signed_body_for_assertion: bytes,
    ) -> "HttpResponse":
        """Send ``body`` to ``url`` with the v1.6.0 signature
        headers. Returns :class:`HttpResponse` with status_code
        and (truncated) error detail; raises HTTP-related
        exceptions for the caller to classify.

        Plan §3.1 single-serialization runtime assertion: the
        bytes the HTTP layer is about to send MUST equal the
        bytes that were signed. ``signed_body_for_assertion``
        is the value produced by signing.sign_request().body;
        if it ever differs from ``body`` here, a refactor has
        introduced a transformation between sign and send and
        the assertion catches it before the HTTP layer goes
        live with mismatched bytes.
        """
        assert body is signed_body_for_assertion or body == signed_body_for_assertion, (
            "single-serialization invariant violated: the bytes about to be "
            "POSTed do not match the bytes that were signed. A refactor "
            "introduced a transformation between sign and send."
        )

        # Localised import keeps this module cheap to load when
        # tests stub the HTTP client out via dependency injection.
        import requests  # noqa: WPS433

        headers = {
            "Content-Type": "application/json",
            "X-Sentry-Signature": signature,
            "X-Sentry-Signature-Generation": str(secret_generation),
            "X-Sentry-Delivery-Id": f"{event_id}:{timestamp}",
            "X-Sentry-Event-Type": event_type,
            "X-Sentry-Timestamp": str(timestamp),
        }

        # verify=True is the v1.6.0 invariant; the CI lint added
        # in D1 enforces no disabled-TLS-verification keyword
        # argument anywhere under api/services/webhook_dispatcher/.
        response = requests.post(
            url,
            data=body,
            headers=headers,
            timeout=self.timeout_s,
            verify=True,
            allow_redirects=False,
        )
        return HttpResponse(
            status_code=response.status_code,
            error_kind=None,
            error_detail=None,
        )


@dataclass(frozen=True)
class HttpResponse:
    status_code: Optional[int]
    error_kind: Optional[str]
    error_detail: Optional[str]


# ---------------------------------------------------------------------
# deliver_one (the core cycle)
# ---------------------------------------------------------------------


def _row_to_event_envelope(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Adapt an integration_events row dict to the envelope
    shape :func:`envelope.build_envelope` expects. Mostly a
    pass-through; the column names already line up except for
    timestamp serialization (Postgres returns datetime; the
    envelope wants an ISO-8601 string)."""
    raw_ts = row["event_timestamp"]
    if hasattr(raw_ts, "isoformat"):
        # Postgres TIMESTAMPTZ -> aware datetime. Use UTC ISO with
        # 'Z' suffix to match the polling-API envelope shape.
        ts_str = raw_ts.isoformat()
    else:
        ts_str = str(raw_ts)
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "event_version": row["event_version"],
        "event_timestamp": ts_str,
        "aggregate_type": row["aggregate_type"],
        "aggregate_external_id": row["aggregate_external_id"],
        "warehouse_id": row["warehouse_id"],
        "source_txn_id": row["source_txn_id"],
        "payload": row["payload"],
    }


def _build_filter_clauses(sub_filter: Mapping[str, Any]) -> tuple[str, list]:
    """Translate ``webhook_subscriptions.subscription_filter``
    into a SQL fragment + parameter list. v1.6.0 supports two
    keys per plan §2.12 (D12 will tighten to a strict-typed
    Pydantic model):

      * event_types: list of strings -> event_type IN (...)
      * warehouse_ids: list of ints -> warehouse_id IN (...)

    Empty filter (the default) returns ('', []) so the SELECT
    matches every event past the cursor.
    """
    clauses: list[str] = []
    params: list = []
    event_types = sub_filter.get("event_types") if isinstance(sub_filter, dict) else None
    warehouse_ids = sub_filter.get("warehouse_ids") if isinstance(sub_filter, dict) else None
    if event_types:
        clauses.append("event_type = ANY(%s)")
        params.append(list(event_types))
    if warehouse_ids:
        clauses.append("warehouse_id = ANY(%s)")
        params.append(list(warehouse_ids))
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _select_next_pending(cur, subscription_id: str) -> Optional[Mapping[str, Any]]:
    """Find the oldest pending delivery for this subscription.
    Hits the ``webhook_deliveries_dispatch`` partial index from
    migration 030."""
    cur.execute(
        """
        SELECT delivery_id, event_id, attempt_number, secret_generation,
               scheduled_at
          FROM webhook_deliveries
         WHERE subscription_id = %s
           AND status = 'pending'
           AND scheduled_at <= NOW()
         ORDER BY scheduled_at ASC, delivery_id ASC
         LIMIT 1
        """,
        (str(subscription_id),),
    )
    return cur.fetchone()


def _select_next_fresh_event(
    cur, subscription: Mapping[str, Any]
) -> Optional[Mapping[str, Any]]:
    """Find the next integration_events row past the subscription
    cursor that matches the subscription filter and has cleared
    the visible-at gate."""
    sub_filter = subscription.get("subscription_filter") or {}
    if isinstance(sub_filter, str):
        try:
            sub_filter = json.loads(sub_filter)
        except (TypeError, ValueError):
            sub_filter = {}
    filter_clause, filter_params = _build_filter_clauses(sub_filter)

    cur.execute(
        f"""
        SELECT event_id, event_type, event_version, event_timestamp,
               aggregate_type, aggregate_id, aggregate_external_id,
               warehouse_id, source_txn_id, payload
          FROM integration_events
         WHERE event_id > %s
           AND visible_at IS NOT NULL
           AND visible_at <= NOW() - INTERVAL '{_VISIBLE_AT_BUFFER_S} seconds'
           {filter_clause}
         ORDER BY event_id ASC
         LIMIT 1
        """,
        [subscription["last_delivered_event_id"]] + filter_params,
    )
    return cur.fetchone()


def _classify_request_exception(exc: Exception) -> tuple[str, str]:
    """Map a Python exception from the placeholder HTTP client
    to (error_kind, error_detail). D8 expands the mapping to
    cover the full requests/urllib3 exception hierarchy; D5
    handles the basic shapes that the test suite exercises."""
    detail = (str(exc) or type(exc).__name__)[:512]
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout", detail
    if "ssl" in name or "tls" in name:
        return "tls", detail
    if "connect" in name:
        return "connection", detail
    return "unknown", detail


def deliver_one(
    conn,
    subscription_id: str,
    http_client: HttpClient,
) -> Optional[DeliveryOutcome]:
    """One delivery cycle for a subscription. Returns None when
    no pending or fresh event exists for this subscription
    (caller backs off); returns a :class:`DeliveryOutcome`
    otherwise.

    The connection's autocommit / transaction state is the
    caller's responsibility. This function performs explicit
    COMMITs at each state-change boundary so a crash mid-loop
    never leaves a row stuck in an undocumented state.
    """
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT subscription_id, connector_id, delivery_url,
               subscription_filter, last_delivered_event_id, status
          FROM webhook_subscriptions
         WHERE subscription_id = %s
        """,
        (str(subscription_id),),
    )
    subscription = cur.fetchone()
    if subscription is None:
        return None
    if subscription["status"] != "active":
        return None

    pending = _select_next_pending(cur, subscription_id)
    if pending is None:
        fresh = _select_next_fresh_event(cur, subscription)
        if fresh is None:
            conn.rollback()
            return None
        cur.execute(
            """
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, secret_generation)
            VALUES (%s, %s, 1, 'pending', NOW(), 1)
            RETURNING delivery_id, event_id, attempt_number, secret_generation,
                      scheduled_at
            """,
            (str(subscription_id), fresh["event_id"]),
        )
        pending = cur.fetchone()

    cur.execute(
        """
        UPDATE webhook_deliveries
           SET status = 'in_flight',
               attempted_at = NOW()
         WHERE delivery_id = %s
        """,
        (pending["delivery_id"],),
    )
    conn.commit()

    # Reload the event row in case the pending row was a retry slot
    # (event_id may differ from the one returned by _select_next_fresh_event).
    cur.execute(
        """
        SELECT event_id, event_type, event_version, event_timestamp,
               aggregate_type, aggregate_id, aggregate_external_id,
               warehouse_id, source_txn_id, payload
          FROM integration_events
         WHERE event_id = %s
        """,
        (pending["event_id"],),
    )
    event_row = cur.fetchone()
    if event_row is None:
        # The event vanished from integration_events after we
        # committed the in_flight flip. Logical-integrity gap.
        # Mark the delivery as failed with error_kind='unknown';
        # the cursor stays put so an operator can investigate.
        cur.execute(
            """
            UPDATE webhook_deliveries
               SET status = 'failed',
                   completed_at = NOW(),
                   error_kind = 'unknown',
                   error_detail = 'integration_events row missing at dispatch time'
             WHERE delivery_id = %s
            """,
            (pending["delivery_id"],),
        )
        conn.commit()
        return DeliveryOutcome(
            delivery_id=pending["delivery_id"],
            event_id=pending["event_id"],
            status="failed",
            http_status=None,
            error_kind="unknown",
            terminal=False,
        )

    secret = signing.load_secret_for_signing(cur, subscription_id)
    signed = signing.sign_request(
        envelope_module.build_envelope(_row_to_event_envelope(event_row)),
        secret,
    )

    started = time.monotonic()
    http_status: Optional[int] = None
    error_kind: Optional[str] = None
    error_detail: Optional[str] = None

    try:
        response = http_client.send(
            url=subscription["delivery_url"],
            body=signed.body,
            signature=signed.signature,
            timestamp=signed.timestamp,
            secret_generation=signed.secret_generation,
            event_type=event_row["event_type"],
            event_id=event_row["event_id"],
            signed_body_for_assertion=signed.body,
        )
        http_status = response.status_code
        error_kind = response.error_kind
        error_detail = response.error_detail
    except AssertionError:
        # Single-serialization invariant fired. Re-raise so the
        # test suite surfaces it loudly; production code paths
        # never hit this because the body is constructed exactly
        # once via sign_request and never transformed.
        raise
    except Exception as exc:  # noqa: BLE001
        error_kind, error_detail = _classify_request_exception(exc)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    is_2xx = http_status is not None and 200 <= http_status < 300

    if is_2xx:
        cur.execute(
            """
            UPDATE webhook_deliveries
               SET status = 'succeeded',
                   completed_at = NOW(),
                   http_status = %s,
                   response_time_ms = %s
             WHERE delivery_id = %s
            """,
            (http_status, elapsed_ms, pending["delivery_id"]),
        )
        cur.execute(
            """
            UPDATE webhook_subscriptions
               SET last_delivered_event_id = %s,
                   updated_at = NOW()
             WHERE subscription_id = %s
               AND last_delivered_event_id < %s
            """,
            (event_row["event_id"], str(subscription_id), event_row["event_id"]),
        )
        conn.commit()
        return DeliveryOutcome(
            delivery_id=pending["delivery_id"],
            event_id=event_row["event_id"],
            status="succeeded",
            http_status=http_status,
            error_kind=None,
            terminal=True,
        )

    if error_kind is None:
        # Non-2xx HTTP status -> classify by status range. D8
        # will refine this with the full requests/urllib3 enum.
        if http_status is not None and 400 <= http_status < 500:
            error_kind = "4xx"
        elif http_status is not None and 500 <= http_status < 600:
            error_kind = "5xx"
        else:
            error_kind = "unknown"
        if error_detail is None:
            error_detail = f"HTTP {http_status}" if http_status is not None else "no response"

    cur.execute(
        """
        UPDATE webhook_deliveries
           SET status = 'failed',
               completed_at = NOW(),
               http_status = %s,
               response_time_ms = %s,
               error_kind = %s,
               error_detail = %s
         WHERE delivery_id = %s
        """,
        (
            http_status,
            elapsed_ms,
            error_kind,
            (error_detail or "")[:512],
            pending["delivery_id"],
        ),
    )
    conn.commit()
    return DeliveryOutcome(
        delivery_id=pending["delivery_id"],
        event_id=event_row["event_id"],
        status="failed",
        http_status=http_status,
        error_kind=error_kind,
        terminal=False,
    )


# ---------------------------------------------------------------------
# Subscription worker pool
# ---------------------------------------------------------------------


class SubscriptionWorker(threading.Thread):
    """One worker per active subscription. Drains its
    per-subscription wake signal and calls :func:`deliver_one`
    until the subscription has no more pending or fresh events
    (deliver_one returns None)."""

    def __init__(
        self,
        subscription_id: str,
        database_url: str,
        http_client: HttpClient,
        shutdown: threading.Event,
    ):
        super().__init__(
            daemon=True,
            name=f"webhook-dispatcher-sub-{subscription_id[:8]}",
        )
        self.subscription_id = subscription_id
        self.database_url = database_url
        self.http_client = http_client
        self._shutdown = shutdown
        self._wake = threading.Event()

    def signal(self) -> None:
        self._wake.set()

    def run(self) -> None:
        # Each worker owns its own connection so a slow POST on
        # one subscription does not block another subscription's
        # SQL queries through a shared connection.
        try:
            conn = psycopg2.connect(self.database_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "subscription %s worker failed to connect to DB: %s",
                self.subscription_id,
                exc,
            )
            return
        try:
            while not self._shutdown.is_set():
                self._wake.wait(timeout=1.0)
                if self._shutdown.is_set():
                    return
                self._wake.clear()
                while not self._shutdown.is_set():
                    try:
                        outcome = deliver_one(
                            conn, self.subscription_id, self.http_client
                        )
                    except AssertionError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning(
                            "subscription %s deliver_one raised: %s",
                            self.subscription_id,
                            exc,
                        )
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    if outcome is None:
                        break
                    LOGGER.info(
                        "delivered subscription=%s event=%s status=%s "
                        "http=%s err=%s",
                        self.subscription_id,
                        outcome.event_id,
                        outcome.status,
                        outcome.http_status,
                        outcome.error_kind,
                    )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class SubscriptionWorkerPool:
    """Owns the per-subscription workers and the subscription-list
    refresh thread. Fans out wake events from the D3 queue to the
    matching per-subscription worker.

    Lifecycle is symmetric with :class:`wake.WakeOrchestrator`:
    ``start()`` spawns the refresh thread and the initial worker
    set; ``shutdown()`` flips the shared event and signals every
    worker; ``join()`` waits.
    """

    def __init__(
        self,
        database_url: str,
        wake_queue: "Queue",
        http_client: Optional[HttpClient] = None,
        refresh_interval_s: float = _SUBSCRIPTION_REFRESH_S,
    ):
        self.database_url = database_url
        self.wake_queue = wake_queue
        self.http_client = http_client or HttpClient()
        self.refresh_interval_s = refresh_interval_s
        self._workers: Dict[str, SubscriptionWorker] = {}
        self._shutdown = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None
        self._fanout_thread: Optional[threading.Thread] = None
        # RLock because start() takes the lock and then calls
        # _refresh_active_subscriptions() which also takes it.
        # A non-reentrant Lock would deadlock on the second
        # acquisition.
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._refresh_thread is not None:
                return
            self._refresh_active_subscriptions()
            self._refresh_thread = threading.Thread(
                target=self._run_refresh,
                daemon=True,
                name="webhook-dispatcher-refresh",
            )
            self._refresh_thread.start()
            self._fanout_thread = threading.Thread(
                target=self._run_fanout,
                daemon=True,
                name="webhook-dispatcher-fanout",
            )
            self._fanout_thread.start()
            LOGGER.info(
                "subscription worker pool started (active=%d)",
                len(self._workers),
            )

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        with self._lock:
            for worker in self._workers.values():
                worker.signal()  # break wake.wait

    def join(self, timeout_s: float = 30.0) -> None:
        for thread in (self._refresh_thread, self._fanout_thread):
            if thread is not None:
                thread.join(timeout=timeout_s)
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.join(timeout=timeout_s)
            if worker.is_alive():
                LOGGER.warning(
                    "worker %s did not exit within %.1fs",
                    worker.name,
                    timeout_s,
                )

    def _refresh_active_subscriptions(self) -> None:
        """Pull the list of active subscriptions; spawn workers
        for any that are new; mark workers for absent
        subscriptions to exit (D7 will tear them down explicitly
        on paused; D5 simply lets a missing-from-active worker
        stop signaling)."""
        try:
            conn = psycopg2.connect(self.database_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("subscription refresh failed to connect: %s", exc)
            return
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT subscription_id::text FROM webhook_subscriptions "
                "WHERE status = 'active'"
            )
            active = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

        with self._lock:
            # Prune dead workers before computing the diff. A
            # worker can die if its psycopg2.connect() raised at
            # spawn time (transient DB blip) -- the run() method
            # logs and returns, leaving the SubscriptionWorker
            # object in self._workers with is_alive() == False.
            # Without pruning, the next refresh cycle sees the
            # subscription_id is "already known" and never
            # respawns; the subscription stays permanently
            # silent until the dispatcher restarts. Re-spawn on
            # the next refresh by removing dead entries first.
            dead = [
                sub_id
                for sub_id, worker in self._workers.items()
                if not worker.is_alive()
            ]
            for sub_id in dead:
                LOGGER.warning(
                    "subscription %s worker is no longer alive; "
                    "pruning so it can respawn this cycle",
                    sub_id,
                )
                del self._workers[sub_id]

            for sub_id in active - set(self._workers.keys()):
                worker = SubscriptionWorker(
                    subscription_id=sub_id,
                    database_url=self.database_url,
                    http_client=self.http_client,
                    shutdown=self._shutdown,
                )
                worker.start()
                worker.signal()  # wake immediately to drain anything pending
                self._workers[sub_id] = worker
                LOGGER.info("spawned worker for subscription %s", sub_id)

    def _run_refresh(self) -> None:
        while not self._shutdown.wait(timeout=self.refresh_interval_s):
            self._refresh_active_subscriptions()

    def _run_fanout(self) -> None:
        """Pull events off the D3 queue and signal the right
        worker(s). For ``poll_all`` and ``fresh_event`` we signal
        every worker (the per-subscription cursor query filters
        events that don't apply); for ``subscription_event`` we
        signal only the named subscription's worker."""
        while not self._shutdown.is_set():
            try:
                event = self.wake_queue.get(timeout=0.5)
            except Empty:
                continue
            if event.kind == "subscription_event":
                with self._lock:
                    worker = self._workers.get(event.subscription_id)
                if worker is not None:
                    worker.signal()
                continue
            with self._lock:
                workers = list(self._workers.values())
            for worker in workers:
                worker.signal()
