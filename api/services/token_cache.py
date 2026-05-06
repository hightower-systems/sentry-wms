"""Per-entry TTL cache over wms_tokens for X-WMS-Token auth.

Used by ``@require_wms_token`` in middleware/auth_middleware.py to
avoid a DB round-trip on every polling or snapshot request. The cache
is per-worker (gunicorn workers do not share memory) so the hot auth
path pays zero extra hops on reads.

Revocation model:

v1.5.0 shipped "per-worker 60s TTL" as the only revocation path. A
token revoked in the admin panel stayed authenticated on every other
gunicorn worker until each worker's local entry expired (up to 60s).
V-205 (#146) flagged this as an unacceptable latency floor: during an
emergency rotation window the compromised token keeps working across
N-1 workers for the full 60s.

v1.5.1 adds Redis pubsub for targeted cross-worker invalidation.
admin_tokens.py calls ``invalidate(token_id)`` on every rotate /
revoke / delete; the call evicts the local entry AND publishes a
message on the ``wms_token_events`` channel. Every worker subscribes
to that channel at boot via a daemon thread that calls
``_invalidate_token_id_local`` on receipt. Revocation latency drops
from up to 60s to sub-second (pubsub delivery + one dict mutation
per worker). If Redis is unavailable the TTL remains the backstop.

Cache storage is a plain dict guarded by a threading.Lock. sync
gunicorn workers serialise HTTP handling so the lock is only
contended by the subscriber thread (and Celery in-process workers or
similar); in practice contention is negligible.
"""

import json
import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

from sqlalchemy import text

import models.database as _db

LOGGER = logging.getLogger(__name__)

# 60s per-entry TTL. Matches the framework doc's stated revocation
# window and lines up with the admin panel "token revoked, wait up to
# a minute" user-facing contract. v1.5.1 V-205 (#146) makes the
# typical case sub-second via pubsub; TTL stays as the backstop when
# Redis is unavailable or a message is dropped.
TTL_SECONDS = 60

# v1.5.1 V-205 (#146): pubsub channel for cross-worker invalidation.
# Every worker subscribes; admin rotate / revoke / delete publishes.
INVALIDATION_CHANNEL = "wms_token_events"

# v1.7.0 #274: Postgres NOTIFY channel for the wms_tokens revoked_at
# trigger. The trigger fires on NULL -> NOT NULL transitions of
# revoked_at regardless of whether the writer is the Flask admin path
# or a direct DB UPDATE. The LISTEN subscriber below converges on the
# same _invalidate_token_id_local effect as the Redis subscriber, so
# direct-DB revokes get sub-second cache eviction across workers.
PG_NOTIFY_REVOCATION_CHANNEL = "wms_token_revocations"


# {token_hash: (row_dict_or_none, fetched_at_epoch_seconds)}
_cache: Dict[str, Tuple[Optional[dict], float]] = {}
_lock = threading.Lock()

# v1.5.1 V-205 (#146): the Redis publisher is a thin handle used by
# invalidate(); the subscriber thread is daemonised so it does not
# block worker shutdown. Both are optional: a deployment without
# Redis still gets the 60s TTL-based revocation contract.
_redis_publisher = None
_subscriber_thread: Optional[threading.Thread] = None
_subscriber_started = threading.Event()

# v1.7.0 #274: Postgres LISTEN subscriber for the revoked_at trigger.
# Independent of the Redis subscriber so a deployment without Redis
# still propagates direct-DB revokes within one round-trip.
_pg_listen_thread: Optional[threading.Thread] = None
_pg_listen_started = threading.Event()
_pg_listen_stop = threading.Event()


def _fetch_by_hash(token_hash: str) -> Optional[dict]:
    """Read one wms_tokens row by token_hash and return it as a dict.

    Returns None when the hash is not in the table. Normalises scope
    array columns to plain Python lists so callers do not have to
    convert psycopg2 list-of-int objects at every usage site.
    """
    session = _db.SessionLocal()
    try:
        row = session.execute(
            text(
                """
                SELECT token_id, token_name, token_hash, warehouse_ids,
                       event_types, endpoints, connector_id, status,
                       source_system, inbound_resources, mapping_override,
                       created_at, rotated_at, expires_at, revoked_at,
                       last_used_at
                  FROM wms_tokens
                 WHERE token_hash = :h
                """
            ),
            {"h": token_hash},
        ).fetchone()
    finally:
        session.close()
    if row is None:
        return None
    return {
        "token_id": row.token_id,
        "token_name": row.token_name,
        "token_hash": row.token_hash,
        "warehouse_ids": list(row.warehouse_ids) if row.warehouse_ids else [],
        "event_types": list(row.event_types) if row.event_types else [],
        "endpoints": list(row.endpoints) if row.endpoints else [],
        "connector_id": row.connector_id,
        "status": row.status,
        # v1.7.0 Pipe B scope dimensions. source_system is NULL for
        # outbound-only tokens; inbound_resources defaults to '{}' so
        # outbound-only tokens still see an empty list (Decision-S).
        "source_system": row.source_system,
        "inbound_resources": list(row.inbound_resources) if row.inbound_resources else [],
        "mapping_override": row.mapping_override,
        "created_at": row.created_at,
        "rotated_at": row.rotated_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "last_used_at": row.last_used_at,
    }


def get_by_hash(token_hash: str) -> Optional[dict]:
    """Return the cached token row for ``token_hash``; refresh from DB on miss or stale."""
    now = time.monotonic()
    with _lock:
        entry = _cache.get(token_hash)
        if entry is not None:
            row, fetched_at = entry
            if now - fetched_at < TTL_SECONDS:
                return row
    # Miss or stale. Fetch without the lock held so the DB round-trip
    # does not block other threads.
    row = _fetch_by_hash(token_hash)
    with _lock:
        _cache[token_hash] = (row, time.monotonic())
    return row


def clear() -> None:
    """Drop the entire local cache. Test-only; production relies on
    ``invalidate`` (targeted + pubsub) and the TTL backstop."""
    with _lock:
        _cache.clear()


def _invalidate_token_id_local(token_id: int) -> None:
    """Evict every cached entry for the given token_id from THIS
    worker's dict. Called by the pubsub subscriber on receipt of an
    invalidation message and by ``invalidate`` on the publishing
    worker. The cache is keyed by token_hash, not token_id, so the
    eviction scans values for the matching token_id. Cost is O(n)
    but n is bounded by the number of distinct tokens a worker has
    ever authenticated (~ dozens in a realistic deployment).
    """
    with _lock:
        to_drop = [
            h for h, (row, _) in _cache.items()
            if row and row.get("token_id") == token_id
        ]
        for h in to_drop:
            del _cache[h]


def invalidate(token_id: int) -> None:
    """v1.5.1 V-205 (#146): evict this token across every worker.

    Evicts the entry from the calling worker's cache immediately,
    then publishes a message on ``wms_token_events`` so subscriber
    threads on every other worker evict the same token from their
    own dicts within one round-trip. Failure to publish is logged
    at warning level and swallowed: the per-worker TTL still catches
    the revocation within 60s.

    Called by admin_tokens.py from rotate / revoke / delete handlers
    instead of the v1.5.0 ``clear()`` (which only flushed the
    handling worker).
    """
    _invalidate_token_id_local(int(token_id))
    if _redis_publisher is None:
        return
    try:
        _redis_publisher.publish(
            INVALIDATION_CHANNEL,
            json.dumps({"token_id": int(token_id)}),
        )
    except Exception:  # noqa: BLE001 -- best effort; TTL is the backstop
        LOGGER.warning(
            "token_cache: pubsub publish failed for token_id=%s; "
            "relying on TTL backstop",
            token_id,
        )


def start_invalidation_subscriber(redis_url: Optional[str]) -> None:
    """Wire up the pubsub publisher + a daemon subscriber thread.

    Idempotent: safe to call more than once per process. Called from
    ``create_app`` after the rate-limiter's Redis URL has been
    resolved. ``redis_url`` of None (or missing ``redis`` module)
    disables pubsub and falls back to the TTL-only revocation path;
    the cache still works, just with the v1.5.0 latency floor.
    """
    global _redis_publisher, _subscriber_thread
    if _subscriber_started.is_set():
        return
    if not redis_url or not redis_url.startswith(("redis://", "rediss://")):
        LOGGER.info(
            "token_cache: no redis URL; invalidation pubsub disabled "
            "(TTL %ds is the only revocation path)",
            TTL_SECONDS,
        )
        _subscriber_started.set()
        return
    try:
        import redis  # noqa: WPS433 -- localised to avoid import cost in tests
    except ImportError:
        LOGGER.warning(
            "token_cache: redis package unavailable; invalidation "
            "pubsub disabled (TTL %ds is the only revocation path)",
            TTL_SECONDS,
        )
        _subscriber_started.set()
        return

    try:
        _redis_publisher = redis.Redis.from_url(redis_url)
        sub_client = redis.Redis.from_url(redis_url)
        pubsub = sub_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(INVALIDATION_CHANNEL)
    except Exception:  # noqa: BLE001
        LOGGER.warning(
            "token_cache: failed to open Redis pubsub connection; "
            "invalidation falls back to %ds TTL",
            TTL_SECONDS,
        )
        _redis_publisher = None
        _subscriber_started.set()
        return

    def _run():
        # The loop body handles malformed messages locally; the
        # outer try/except exists for connection-level failures
        # (Redis shutdown, network blip, test-process teardown
        # closing the socket) so the daemon thread exits cleanly
        # instead of raising an uncaught exception that pytest
        # surfaces as PytestUnhandledThreadExceptionWarning.
        # The parent process relies on the per-entry TTL as the
        # backstop whenever the subscriber is not running.
        try:
            for message in pubsub.listen():
                if not message or message.get("type") != "message":
                    continue
                try:
                    payload = message.get("data")
                    if isinstance(payload, bytes):
                        payload = payload.decode("utf-8")
                    data = json.loads(payload) if payload else {}
                    tid = data.get("token_id")
                    if tid is not None:
                        _invalidate_token_id_local(int(tid))
                except Exception:  # noqa: BLE001
                    LOGGER.warning(
                        "token_cache: malformed pubsub message ignored"
                    )
        except Exception as exc:  # noqa: BLE001
            # ConnectionError / ValueError (closed-file on the
            # socket buffer) / anything else. Log once at INFO
            # since this is the expected shape of a clean shutdown,
            # then return. Invalidation falls back to the TTL
            # until create_app re-invokes start_invalidation_subscriber
            # (e.g. on worker restart).
            LOGGER.info(
                "token_cache: invalidation subscriber exiting (%s: %s)",
                type(exc).__name__,
                exc,
            )

    _subscriber_thread = threading.Thread(
        target=_run,
        daemon=True,
        name="wms-token-cache-subscriber",
    )
    _subscriber_thread.start()
    _subscriber_started.set()
    LOGGER.info(
        "token_cache: invalidation subscriber started on channel %s",
        INVALIDATION_CHANNEL,
    )


def start_pg_listen_subscriber(database_url: Optional[str]) -> None:
    """v1.7.0 #274: Postgres LISTEN subscriber for direct-DB revokes.

    A daemon thread opens a dedicated psycopg2 connection (LISTEN
    requires its own session and AUTOCOMMIT isolation) and dispatches
    every NOTIFY on `wms_token_revocations` to
    `_invalidate_token_id_local`. The trigger published in mig 048
    fires AFTER UPDATE OF revoked_at so the LISTEN path catches every
    revoke regardless of whether the writer is the Flask admin handler
    or a direct DB UPDATE.

    Idempotent: safe to call more than once per process.
    `database_url` of None disables the LISTEN path; the per-entry TTL
    remains the only revocation backstop in that mode.
    """
    global _pg_listen_thread
    if _pg_listen_started.is_set():
        return
    if not database_url:
        LOGGER.info(
            "token_cache: no database URL; pg LISTEN disabled "
            "(direct-DB revokes fall back to %ds TTL)",
            TTL_SECONDS,
        )
        _pg_listen_started.set()
        return

    try:
        import psycopg2  # noqa: WPS433 -- localised to keep test boot light
        from psycopg2 import extensions as _ext  # noqa: WPS433
    except ImportError:
        LOGGER.warning(
            "token_cache: psycopg2 unavailable; pg LISTEN disabled "
            "(direct-DB revokes fall back to %ds TTL)",
            TTL_SECONDS,
        )
        _pg_listen_started.set()
        return

    try:
        conn = psycopg2.connect(database_url)
        conn.set_isolation_level(_ext.ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute(f"LISTEN {PG_NOTIFY_REVOCATION_CHANNEL}")
    except Exception:  # noqa: BLE001
        LOGGER.warning(
            "token_cache: failed to open pg LISTEN connection; "
            "direct-DB revokes fall back to %ds TTL",
            TTL_SECONDS,
        )
        _pg_listen_started.set()
        return

    def _run():
        # select-loop with a short timeout so the thread can exit when
        # `_pg_listen_stop` is set during test teardown without waiting
        # for a NOTIFY to wake it.
        import select
        try:
            while not _pg_listen_stop.is_set():
                rlist, _, _ = select.select([conn], [], [], 1.0)
                if not rlist:
                    continue
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    try:
                        token_id = int(notify.payload)
                    except (TypeError, ValueError):
                        LOGGER.warning(
                            "token_cache: malformed pg NOTIFY payload "
                            "ignored: %r",
                            notify.payload,
                        )
                        continue
                    _invalidate_token_id_local(token_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info(
                "token_cache: pg LISTEN subscriber exiting (%s: %s)",
                type(exc).__name__,
                exc,
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    _pg_listen_thread = threading.Thread(
        target=_run,
        daemon=True,
        name="wms-token-cache-pg-listen",
    )
    _pg_listen_thread.start()
    _pg_listen_started.set()
    LOGGER.info(
        "token_cache: pg LISTEN subscriber started on channel %s",
        PG_NOTIFY_REVOCATION_CHANNEL,
    )


def _testing_override_ttl(new_ttl_seconds: float) -> None:
    """Test-only: swap the module TTL to make TTL-boundary tests fast.

    The helper exists so tests do not need to wait 60 wall-clock seconds
    to exercise the stale-entry refresh path.
    """
    global TTL_SECONDS
    TTL_SECONDS = new_ttl_seconds


def _testing_reset_subscriber() -> None:
    """Test-only: reset the subscriber-started sentinel so a test can
    force a re-initialisation with a different Redis configuration
    (e.g., simulating "Redis unavailable" vs "Redis up")."""
    global _redis_publisher, _subscriber_thread, _pg_listen_thread
    _subscriber_started.clear()
    _redis_publisher = None
    _subscriber_thread = None
    _pg_listen_stop.set()
    _pg_listen_started.clear()
    _pg_listen_thread = None
    _pg_listen_stop.clear()
