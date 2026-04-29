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

Auto-pause invariants (plan §2.11, landed in D7):

  * After a non-terminal failure (D6 retry-slot INSERT), the
    pending count for the subscription is checked. If it has
    reached ``pending_ceiling``, the subscription flips to
    ``status='paused'`` with ``pause_reason='pending_ceiling'``
    in the same transaction as the failed-row write. A
    ``paused`` event is published on the
    ``webhook_subscription_events`` channel so peer workers
    evict their state.
  * After a terminal failure (D6 dlq flip), the DLQ count for
    the subscription is checked. If it has reached
    ``dlq_ceiling``, the same auto-pause path fires with
    ``pause_reason='dlq_ceiling'``.
  * Hard caps from ``DISPATCHER_MAX_PENDING_HARD_CAP`` /
    ``DISPATCHER_MAX_DLQ_HARD_CAP`` bound per-subscription
    overrides at admin-set time (A1 owns the upper-bound
    check; D7 lands the env-validator plumbing only).
  * Audit_log writes on each auto-pause are deferred to A1
    when webhook admin CRUD wires the audit_log hash chain.
    D7 logs CRITICAL on every auto-pause so the operator
    sees it in compose logs immediately.

Worker eviction (plan §2.9 + D5 follow-up #177):

  * A ``paused`` or ``deleted`` event on the pubsub channel
    triggers ``SubscriptionWorkerPool._run_fanout`` to call
    ``request_eviction()`` on the matching worker. The worker
    closes its psycopg2 connection (which surfaces as a clean
    exception in any in-flight SELECT), the run-loop exits,
    and the next ``_refresh_active_subscriptions`` cycle
    prunes the dead worker via the existing dead-worker
    pruning logic from D5.

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
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

from . import envelope as envelope_module
from . import http_client as http_client_module
from . import rate_limiter as rate_limiter_module
from . import retry as retry_module
from . import signing
from . import wake as wake_module

# Re-export for backwards-compatibility with tests/imports that
# referenced the D5 in-dispatch HttpClient placeholder. D8 moved
# the implementation to http_client.py.
HttpClient = http_client_module.HttpClient
HttpResponse = http_client_module.HttpResponse


LOGGER = logging.getLogger("webhook_dispatcher.dispatch")


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


# HttpClient + HttpResponse moved to http_client.py in D8.
# Re-exported above for backwards compatibility.


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


def _count_pending(cur, subscription_id: str) -> int:
    """Return the number of pending OR in_flight delivery rows
    for a subscription. Hits the
    webhook_deliveries_pending_count partial index. Used by the
    auto-pause check after a non-terminal failure (D7)."""
    cur.execute(
        """
        SELECT COUNT(*) FROM webhook_deliveries
         WHERE subscription_id = %s
           AND status IN ('pending', 'in_flight')
        """,
        (str(subscription_id),),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    if hasattr(row, "keys"):
        # RealDictRow -- COUNT(*) lands under the 'count' key.
        return int(next(iter(row.values())))
    return int(row[0])


def _count_dlq(cur, subscription_id: str) -> int:
    """Return the number of dlq delivery rows for a subscription.
    Hits the webhook_deliveries_dlq partial index. Used by the
    auto-pause check after a terminal DLQ flip (D7)."""
    cur.execute(
        """
        SELECT COUNT(*) FROM webhook_deliveries
         WHERE subscription_id = %s
           AND status = 'dlq'
        """,
        (str(subscription_id),),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    if hasattr(row, "keys"):
        return int(next(iter(row.values())))
    return int(row[0])


def _maybe_auto_pause(
    cur,
    subscription_id: str,
    pending_ceiling: int,
    dlq_ceiling: int,
    after_terminal_dlq: bool,
) -> Optional[str]:
    """Plan §2.11 ceiling auto-pause. Called after a failure
    write (non-terminal or terminal-DLQ) but BEFORE the commit
    that releases the row. Flips the subscription to
    ``status='paused'`` with the appropriate ``pause_reason``
    when the count crosses the threshold; returns the reason
    string used (caller publishes to the pubsub channel).

    Returns None when no auto-pause fired.

    The flip is conditional on ``status='active'`` so a
    subscription that is already paused (e.g., by an admin or a
    prior auto-pause from a peer dispatcher) does not get
    re-paused with a different reason. The conditional UPDATE
    + the count read happen in the same transaction as the
    failed-row write, which the caller commits together.
    """
    if after_terminal_dlq:
        count = _count_dlq(cur, subscription_id)
        if count < dlq_ceiling:
            return None
        reason = "dlq_ceiling"
    else:
        count = _count_pending(cur, subscription_id)
        if count < pending_ceiling:
            return None
        reason = "pending_ceiling"

    cur.execute(
        """
        UPDATE webhook_subscriptions
           SET status = 'paused',
               pause_reason = %s,
               updated_at = NOW()
         WHERE subscription_id = %s
           AND status = 'active'
        """,
        (reason, str(subscription_id)),
    )
    if cur.rowcount > 0:
        # CRITICAL log so the operator sees the auto-pause in
        # compose logs immediately. The audit_log hash-chain
        # row is deferred to A1 (admin webhook CRUD writes it).
        LOGGER.critical(
            "auto-paused subscription %s: pause_reason=%s, count=%d, "
            "ceiling=%d. Triage required: open the DLQ viewer or fix "
            "the consumer's failure mode, then resume the subscription.",
            subscription_id,
            reason,
            count,
            dlq_ceiling if after_terminal_dlq else pending_ceiling,
        )
        return reason
    return None


def _has_non_terminal_delivery(cur, subscription_id: str) -> bool:
    """True when the subscription has any non-terminal delivery
    row (pending or in_flight). Used by deliver_one to enforce
    head-of-line blocking in the presence of future-scheduled
    retry slots: a retry slot whose scheduled_at is past NOW()
    is matched by _select_next_pending; a retry slot whose
    scheduled_at is still in the future is NOT, but the event
    has not terminated and the dispatcher must back off rather
    than pick a newer event from integration_events.

    Hits the webhook_deliveries_pending_count partial index
    from migration 030.
    """
    cur.execute(
        """
        SELECT 1 FROM webhook_deliveries
         WHERE subscription_id = %s
           AND status IN ('pending', 'in_flight')
         LIMIT 1
        """,
        (str(subscription_id),),
    )
    return cur.fetchone() is not None


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
    """D5 placeholder retained for tests that pre-date D8.
    Production code path uses
    :func:`http_client.classify_exception` via HttpClient.send;
    deliver_one's catch path here only fires when a stub
    raises directly without producing an HttpResponse."""
    return http_client_module.classify_exception(exc)


def deliver_one(
    conn,
    subscription_id: str,
    http_client: HttpClient,
    redis_url: Optional[str] = None,
    acquire_rate_token: Optional[Callable[[int], bool]] = None,
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

    select_subscription_sql = """
        SELECT subscription_id, connector_id, delivery_url,
               subscription_filter, last_delivered_event_id, status,
               pending_ceiling, dlq_ceiling, rate_limit_per_second
          FROM webhook_subscriptions
         WHERE subscription_id = %s
        """
    cur.execute(select_subscription_sql, (str(subscription_id),))
    subscription = cur.fetchone()
    if subscription is None:
        return None
    if subscription["status"] != "active":
        return None

    if acquire_rate_token is not None:
        rate = int(subscription["rate_limit_per_second"])
        # Release the SELECT snapshot before sleeping for the
        # token. A subscription throttled at rate=1/s would
        # otherwise hold an idle transaction open for up to 1s
        # per cycle.
        conn.rollback()
        if not acquire_rate_token(rate):
            return None
        # Re-read the subscription. The status or rate could have
        # flipped during the wait (admin paused the subscription,
        # rate_limit_changed fired); the worker eviction path also
        # closes the connection mid-acquire on paused/deleted.
        cur.execute(select_subscription_sql, (str(subscription_id),))
        subscription = cur.fetchone()
        if subscription is None or subscription["status"] != "active":
            conn.rollback()
            return None

    pending = _select_next_pending(cur, subscription_id)
    if pending is None:
        # Plan §2.5 head-of-line blocking: if any non-terminal
        # delivery row exists for this subscription (a future-
        # scheduled retry slot from D6, or an in_flight row),
        # back off entirely. The fresh-select must NOT pick a
        # newer event past the cursor while a prior event is
        # still working through its retry schedule.
        if _has_non_terminal_delivery(cur, subscription_id):
            conn.rollback()
            return None
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

    # Plan §2.3 step 8: failure branch splits on attempt_number.
    # If the failed attempt is the 8th (terminal), flip the
    # current row directly to 'dlq' and advance the cursor;
    # otherwise mark the current row 'failed' and INSERT a fresh
    # retry-slot pending row scheduled at NOW() + retry_delay.
    current_attempt = pending["attempt_number"]

    if retry_module.is_terminal_attempt(current_attempt):
        cur.execute(
            """
            UPDATE webhook_deliveries
               SET status = 'dlq',
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
        # DLQ is terminal -> cursor advances per plan §1.6.
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
        # Plan §2.11: auto-pause if dlq_ceiling crossed. Same
        # transaction as the dlq write so peer dispatchers see
        # the flip atomically with the row landing.
        paused_reason = _maybe_auto_pause(
            cur,
            subscription_id,
            pending_ceiling=int(subscription["pending_ceiling"]),
            dlq_ceiling=int(subscription["dlq_ceiling"]),
            after_terminal_dlq=True,
        )
        conn.commit()
        if paused_reason is not None:
            wake_module.publish_subscription_event(redis_url, str(subscription_id), "paused")
        return DeliveryOutcome(
            delivery_id=pending["delivery_id"],
            event_id=event_row["event_id"],
            status="dlq",
            http_status=http_status,
            error_kind=error_kind,
            terminal=True,
        )

    # Non-terminal failure: flip the current row and schedule
    # a fresh retry slot. The retry slot's scheduled_at gates
    # _select_next_pending so the dispatcher does not pick it
    # up before the schedule says it should.
    next_attempt = current_attempt + 1
    delay_s = retry_module.retry_delay(next_attempt)

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
    cur.execute(
        f"""
        INSERT INTO webhook_deliveries
            (subscription_id, event_id, attempt_number, status,
             scheduled_at, secret_generation)
        VALUES (%s, %s, %s, 'pending', NOW() + INTERVAL '{delay_s} seconds', 1)
        """,
        (str(subscription_id), event_row["event_id"], next_attempt),
    )
    # Plan §2.11: auto-pause if pending_ceiling crossed. The
    # retry-slot row we just INSERTed counts toward pending,
    # which is intentional -- the ceiling exists to bound the
    # backlog, and an unhealthy consumer that failed N times
    # has N pending retry slots still alive.
    paused_reason = _maybe_auto_pause(
        cur,
        subscription_id,
        pending_ceiling=int(subscription["pending_ceiling"]),
        dlq_ceiling=int(subscription["dlq_ceiling"]),
        after_terminal_dlq=False,
    )
    conn.commit()
    if paused_reason is not None:
        wake_module.publish_subscription_event(redis_url, str(subscription_id), "paused")
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


def reset_orphaned_in_flight(database_url: str) -> int:
    """Flip every webhook_deliveries row stuck in ``in_flight`` back
    to ``pending`` with ``scheduled_at=NOW()``. Called on dispatcher
    boot. The dispatcher is the sole writer of these rows; an
    ``in_flight`` row at boot can only mean the prior process exited
    mid-POST, so the row is orphaned and must be retried. No
    age-threshold heuristic.

    Returns the number of rows reset so the caller can log it.
    """
    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE webhook_deliveries
               SET status = 'pending',
                   scheduled_at = NOW()
             WHERE status = 'in_flight'
            """
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


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
        redis_url: Optional[str] = None,
    ):
        super().__init__(
            daemon=True,
            name=f"webhook-dispatcher-sub-{subscription_id[:8]}",
        )
        self.subscription_id = subscription_id
        self.database_url = database_url
        self.http_client = http_client
        self.redis_url = redis_url
        self._shutdown = shutdown
        self._wake = threading.Event()
        self._evicted = threading.Event()
        self._conn = None  # populated in run() so request_eviction can close
        self._rate_bucket: Optional[rate_limiter_module.TokenBucket] = None

    def _acquire_rate_token(self, rate: int) -> bool:
        if self._rate_bucket is None:
            self._rate_bucket = rate_limiter_module.TokenBucket(rate)
        else:
            self._rate_bucket.set_rate(rate)
        # Bound the wait at 60s so a misconfigured rate combined
        # with a stuck consumer cannot leave a worker stranded
        # past a shutdown signal indefinitely; the shutdown event
        # also short-circuits the wait.
        return self._rate_bucket.acquire(timeout_s=60.0, shutdown=self._shutdown)

    def signal(self) -> None:
        self._wake.set()

    def request_eviction(self) -> None:
        """Plan §2.9 + D5 follow-up #177: cross-worker
        invalidation of paused / deleted subscriptions.

        Sets the per-worker eviction event AND closes the
        worker's psycopg2 connection so a thread mid-deliver_one
        exits its inner SELECT cleanly with a connection-closed
        error rather than completing the in-flight POST.
        Idempotent.
        """
        if self._evicted.is_set():
            return
        self._evicted.set()
        self._wake.set()  # break out of the wake.wait
        # Close the connection from the orchestrator thread so
        # the worker's inner I/O surfaces a clean exception.
        # psycopg2 connections are documented as not-thread-
        # safe for concurrent use, but close() from a different
        # thread is acceptable -- the worker thread's next
        # operation will raise InterfaceError, which the run()
        # loop catches and treats as a clean exit.
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        # Each worker owns its own connection so a slow POST on
        # one subscription does not block another subscription's
        # SQL queries through a shared connection.
        try:
            self._conn = psycopg2.connect(self.database_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "subscription %s worker failed to connect to DB: %s",
                self.subscription_id,
                exc,
            )
            return
        try:
            while not self._shutdown.is_set() and not self._evicted.is_set():
                self._wake.wait(timeout=1.0)
                if self._shutdown.is_set() or self._evicted.is_set():
                    return
                self._wake.clear()
                while not self._shutdown.is_set() and not self._evicted.is_set():
                    try:
                        outcome = deliver_one(
                            self._conn,
                            self.subscription_id,
                            self.http_client,
                            redis_url=self.redis_url,
                            acquire_rate_token=self._acquire_rate_token,
                        )
                    except AssertionError:
                        raise
                    except psycopg2.InterfaceError:
                        # Eviction closed the connection mid-
                        # query. Clean exit shape per plan §2.9.
                        return
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning(
                            "subscription %s deliver_one raised: %s",
                            self.subscription_id,
                            exc,
                        )
                        try:
                            self._conn.rollback()
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
            if self._evicted.is_set():
                LOGGER.info(
                    "subscription %s worker evicted (paused/deleted/"
                    "delivery_url_changed)",
                    self.subscription_id,
                )
            try:
                if self._conn is not None:
                    self._conn.close()
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
        redis_url: Optional[str] = None,
    ):
        self.database_url = database_url
        self.wake_queue = wake_queue
        self.http_client = http_client or HttpClient()
        self.refresh_interval_s = refresh_interval_s
        self.redis_url = redis_url
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
        survivors = 0
        for worker in workers:
            worker.join(timeout=timeout_s)
            if worker.is_alive():
                survivors += 1
                LOGGER.warning(
                    "worker %s did not exit within %.1fs",
                    worker.name,
                    timeout_s,
                )
        if survivors:
            LOGGER.warning(
                "shutdown drain timed out: %d worker(s) still in flight; "
                "process will exit and abandon them (threads are daemon=True). "
                "On the next boot the orphaned in_flight rows are reset to "
                "pending unconditionally.",
                survivors,
            )
        # Tear down the shared HTTP session once, after workers are
        # guaranteed not to be calling send() concurrently. close()
        # is idempotent so a double-shutdown path is safe.
        try:
            self.http_client.close()
        except Exception:  # noqa: BLE001
            pass

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
                    redis_url=self.redis_url,
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
                if worker is None:
                    continue
                # Plan §2.9 action table: paused and deleted
                # evict the worker; other events just signal it
                # (the next deliver_one re-reads the row to pick
                # up the new state).
                if event.subscription_event_kind in ("paused", "deleted"):
                    worker.request_eviction()
                else:
                    worker.signal()
                continue
            with self._lock:
                workers = list(self._workers.values())
            for worker in workers:
                worker.signal()
