"""Productivity dashboard aggregations (v1.8.0 #297).

Reads from audit_log (canonical "who did what"), not from
integration_events (the outbox for downstream consumers). The
ix_audit_log_dashboard covering index landed in mig 051 (#283)
keeps cold-cache p95 under 500ms over a 30-day window with ~100k
audit rows.

Five event kinds, each with its own metric:

  picking       COUNT(DISTINCT entity_id) WHERE entity_type='SO'  (P11.1)
  packing       SUM(details.total_items)
  received_skus COUNT(DISTINCT details.item_id)
  putaway_skus  COUNT(DISTINCT entity_id) WHERE entity_type='ITEM'
  shipped       COUNT(*)

Per-action SQL is preferred over a single aggregator because the
audit_log details JSONB shape varies per action (the field-path
heterogeneity is documented at the call sites; centralising it in
the service module keeps the dashboard query honest about each
metric's origin).

60s in-process TTL cache absorbs repeat hits from the handful of
admins who refresh. Pattern adapted from token_cache without the
cross-worker pubsub layer (60s staleness across workers is fine
for a refresh-driven view).

Packing visibility honours app_settings.require_packing_before_
shipping; when False, the packing event is excluded from the
response so the UI does not render a perpetually-zero card.
"""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text


_CACHE_TTL_SECONDS = 60.0
_cache_lock = threading.Lock()
_cache: Dict[Tuple[int, str, str], Tuple[float, dict]] = {}


# ============================================================
# Catalog
# ============================================================
#
# Order matches the default chart_order column on
# user_dashboard_preferences (mig 051) so the response array order
# is operator-meaningful when the per-user override is absent.

DASHBOARD_EVENTS: List[Tuple[str, str, str]] = [
    # (slug, action_type, metric_kind)
    # P11.1: picking measures distinct SOs picked, not summed line
    # quantities. A picker who closes ten 1-unit SOs is doing more
    # discrete work than one who fills a single 10-unit SO; the unit
    # count buried that signal under raw throughput.
    ("picking",       "PICK",    "orders"),
    ("packing",       "PACK",    "units"),
    ("shipped",       "SHIP",    "orders"),
    ("received_skus", "RECEIVE", "unique_skus"),
    ("putaway_skus",  "PUTAWAY", "unique_skus"),
]

EVENT_LABELS = {
    "picking":       "Picking (orders)",
    "packing":       "Packing (units)",
    "shipped":       "Shipped (orders)",
    "received_skus": "Received (unique SKUs)",
    "putaway_skus":  "Put Away (unique SKUs)",
}


# ============================================================
# Per-event aggregation queries
# ============================================================
#
# Each helper returns a list of (user_id, value) rows. The rollup
# function below stitches them into the per-user metrics dict.


def _agg_picking(db, warehouse_id, start, end):
    # P11.1: count distinct SOs the user closed, not summed units.
    # PICK rows are written one-per-line at pick-confirm time with
    # entity_type='SO' and entity_id=so_id, so COUNT(DISTINCT
    # entity_id) collapses multi-line SOs back to one credit per
    # picker per SO. The 'SO' entity filter excludes the TO-line
    # picks (entity_type='TO_LINE') that also use action='PICK'.
    rows = db.execute(
        text(
            """
            SELECT user_id, COUNT(DISTINCT entity_id) AS v
              FROM audit_log
             WHERE action_type = 'PICK'
               AND entity_type = 'SO'
               AND created_at >= :start AND created_at < :end
               AND warehouse_id = :wid
             GROUP BY user_id
            """
        ),
        {"start": start, "end": end, "wid": warehouse_id},
    ).fetchall()
    return [(r.user_id, int(r.v)) for r in rows]


def _agg_packing(db, warehouse_id, start, end):
    rows = db.execute(
        text(
            """
            SELECT user_id, COALESCE(SUM((details->>'total_items')::int), 0) AS v
              FROM audit_log
             WHERE action_type = 'PACK'
               AND created_at >= :start AND created_at < :end
               AND warehouse_id = :wid
               AND details ? 'total_items'
             GROUP BY user_id
            """
        ),
        {"start": start, "end": end, "wid": warehouse_id},
    ).fetchall()
    return [(r.user_id, int(r.v)) for r in rows]


def _agg_shipped(db, warehouse_id, start, end):
    rows = db.execute(
        text(
            """
            SELECT user_id, COUNT(*) AS v
              FROM audit_log
             WHERE action_type = 'SHIP'
               AND created_at >= :start AND created_at < :end
               AND warehouse_id = :wid
             GROUP BY user_id
            """
        ),
        {"start": start, "end": end, "wid": warehouse_id},
    ).fetchall()
    return [(r.user_id, int(r.v)) for r in rows]


def _agg_received_skus(db, warehouse_id, start, end):
    rows = db.execute(
        text(
            """
            SELECT user_id, COUNT(DISTINCT details->>'item_id') AS v
              FROM audit_log
             WHERE action_type = 'RECEIVE'
               AND created_at >= :start AND created_at < :end
               AND warehouse_id = :wid
               AND details ? 'item_id'
             GROUP BY user_id
            """
        ),
        {"start": start, "end": end, "wid": warehouse_id},
    ).fetchall()
    return [(r.user_id, int(r.v)) for r in rows]


def _agg_putaway_skus(db, warehouse_id, start, end):
    rows = db.execute(
        text(
            """
            SELECT user_id, COUNT(DISTINCT entity_id) AS v
              FROM audit_log
             WHERE action_type = 'PUTAWAY'
               AND created_at >= :start AND created_at < :end
               AND warehouse_id = :wid
               AND entity_type = 'ITEM'
             GROUP BY user_id
            """
        ),
        {"start": start, "end": end, "wid": warehouse_id},
    ).fetchall()
    return [(r.user_id, int(r.v)) for r in rows]


_AGGREGATORS = {
    "picking":       _agg_picking,
    "packing":       _agg_packing,
    "shipped":       _agg_shipped,
    "received_skus": _agg_received_skus,
    "putaway_skus":  _agg_putaway_skus,
}


def _is_packing_required(db) -> bool:
    row = db.execute(
        text(
            "SELECT value FROM app_settings "
            " WHERE key = 'require_packing_before_shipping'"
        )
    ).fetchone()
    if row is None:
        return True
    return str(row.value).lower() != "false"


# ============================================================
# Public entry point
# ============================================================


def get_productivity(
    db,
    warehouse_id: int,
    start,
    end,
    *,
    skip_cache: bool = False,
) -> dict:
    """Returns the productivity payload for the requested window.

    Cached per (warehouse_id, start, end) for 60 seconds; pass
    skip_cache=True for tests that need fresh reads after seeding
    audit rows.
    """
    cache_key = (
        warehouse_id,
        start.isoformat() if hasattr(start, "isoformat") else str(start),
        end.isoformat() if hasattr(end, "isoformat") else str(end),
    )
    now = time.monotonic()
    if not skip_cache:
        with _cache_lock:
            cached = _cache.get(cache_key)
            if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
                return cached[1]

    show_packing = _is_packing_required(db)
    visible = [
        (slug, action, kind)
        for (slug, action, kind) in DASHBOARD_EVENTS
        if slug != "packing" or show_packing
    ]
    visible_slugs = [slug for (slug, _, _) in visible]

    # users[user_id] = {slug: value, ...}
    users: Dict[str, Dict[str, int]] = {}
    totals_per_event: Dict[str, int] = {slug: 0 for slug in visible_slugs}
    for slug, _action, _kind in visible:
        for user_id, value in _AGGREGATORS[slug](db, warehouse_id, start, end):
            users.setdefault(user_id, {})[slug] = value
            totals_per_event[slug] += value

    serialised_users = []
    for user_id, metrics in users.items():
        # Backfill missing slugs with 0 so the UI does not need to
        # null-check per cell.
        full_metrics = {slug: int(metrics.get(slug, 0)) for slug in visible_slugs}
        serialised_users.append({
            "user_id": user_id,
            "username": user_id,  # audit_log.user_id IS the username
            "display_name": user_id,
            "metrics": full_metrics,
            "total": sum(full_metrics.values()),
        })
    # Sort by total desc, ties broken by user_id asc so the response
    # is stable for diff-based tests + UI rendering.
    serialised_users.sort(key=lambda u: (-u["total"], u["user_id"]))

    payload = {
        "range": {
            "start": cache_key[1],
            "end": cache_key[2],
        },
        "warehouse_id": warehouse_id,
        "events_visible": visible_slugs,
        "users": serialised_users,
        "totals_per_event": totals_per_event,
    }

    with _cache_lock:
        _cache[cache_key] = (now, payload)
    return payload


def clear_cache() -> None:
    """Test-only hook: drop all cached entries so a fresh aggregation
    runs on the next call."""
    with _cache_lock:
        _cache.clear()
