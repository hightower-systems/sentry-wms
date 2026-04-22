"""Per-entry TTL cache over wms_tokens for X-WMS-Token auth.

Used by ``@require_wms_token`` in middleware/auth_middleware.py to
avoid a DB round-trip on every polling or snapshot request. The cache
is per-worker (gunicorn workers do not share memory in v1.5.0) and
the TTL is the framework-doc stated revocation window: a token
revoked in the admin panel is rejected within 60 seconds on every
worker.

Trade-off: a strictly shared cache (Redis) would give near-instant
revocation but add a network hop to every auth check. The per-worker
TTL trades up-to-60s revocation latency for zero extra hops on the
hot path. Snapshot page latency and polling p95 live on this hop.

Cache storage is a plain dict guarded by a threading.Lock. sync
gunicorn workers serialise HTTP handling so the lock is only
contended by background threads (Celery in-process workers or
similar); in practice contention is negligible.
"""

import os
import threading
import time
from typing import Dict, Optional, Tuple

from sqlalchemy import text

import models.database as _db

# 60s per-entry TTL. Matches the framework doc's stated revocation
# window and lines up with the admin panel "token revoked, wait up to
# a minute" user-facing contract.
TTL_SECONDS = 60


# {token_hash: (row_dict_or_none, fetched_at_epoch_seconds)}
_cache: Dict[str, Tuple[Optional[dict], float]] = {}
_lock = threading.Lock()


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
    """Drop the entire cache. Test-only; production relies on TTL expiry."""
    with _lock:
        _cache.clear()


def _testing_override_ttl(new_ttl_seconds: float) -> None:
    """Test-only: swap the module TTL to make TTL-boundary tests fast.

    The helper exists so tests do not need to wait 60 wall-clock seconds
    to exercise the stale-entry refresh path.
    """
    global TTL_SECONDS
    TTL_SECONDS = new_ttl_seconds
