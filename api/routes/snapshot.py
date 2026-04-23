"""GET /api/v1/snapshot/inventory bulk snapshot paging (v1.5.0 #133).

Pairs with the snapshot-keeper daemon (#132). The keeper holds
REPEATABLE READ transactions with an exported pg_snapshot_id; this
endpoint imports the same snapshot on short-lived connections and
pages through inventory via a keyset cursor so every page of a scan
sees the same state as of ``snapshot_event_id``.

Wire shape (plan 4.2):

- First page: no cursor. Endpoint INSERTs a pending row into
  ``snapshot_scans``; the keeper picks it up (NOTIFY + poll), flips
  it to 'active', and writes back pg_snapshot_id + snapshot_event_id.
  Endpoint polls the row for up to 5s; if still pending, returns
  503 ``snapshot_keeper_unavailable``.
- Every page (first and subsequent): opens a fresh psycopg2
  connection, runs ``BEGIN RR; SET TRANSACTION SNAPSHOT :id``,
  executes the keyset query, COMMITs.
- Cursor is base64 JSON of ``{scan_id, w, i, b}`` (Decision G).
  On subsequent pages the endpoint rejects cursors whose scan_id
  was not created by the requesting token (403
  ``cursor_scope_violation``) or whose warehouse_id disagrees with
  the query param (same 403).
- Status lifecycle:
  - ``pending``: waiting for keeper promotion.
  - ``active``: snapshot ready; paging in progress.
  - ``done``: final page served; keeper reaps the row.
  - ``expired`` / ``aborted``: keeper already released the held
    transaction. Next client call returns 410 ``snapshot_expired``.

Rate limit: 2 per minute per token (plan 7.5). Well below the
keeper's 4-slot pool under normal concurrency.
"""

import base64
import json
import time
import uuid
from typing import Optional, Tuple

import psycopg2
from flask import Blueprint, Response, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.polling import SnapshotQuery
from services.rate_limit import limiter


snapshot_bp = Blueprint("snapshot", __name__)


KEEPER_POLL_INTERVAL_S = 0.25
KEEPER_PROMOTION_TIMEOUT_S = 5.0

# v1.5.1 V-203 (#144): per-token concurrent-scan cap. The keeper's
# pool holds 4 REPEATABLE READ transactions; pre-v1.5.1 a single
# token could open all 4 slots in quick succession and pin them for
# the full 5-minute idle timeout by touching each scan once per page
# pull. Enforcing one in-flight scan per token forces any attacker
# to amortise the pool-exhaustion cost across multiple credentials;
# it also matches the standard "page your snapshot to completion
# before starting a new one" client pattern.
MAX_CONCURRENT_SCANS_PER_TOKEN = 1


# ── Cursor encoding ──────────────────────────────────────────────────


def _encode_cursor(scan_id: str, warehouse_id: int, item_id: int, bin_id: int) -> str:
    payload = json.dumps(
        {"scan_id": scan_id, "w": warehouse_id, "i": item_id, "b": bin_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(raw: str) -> Optional[dict]:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return {
            "scan_id": str(data["scan_id"]),
            "warehouse_id": int(data["w"]),
            "item_id": int(data["i"]),
            "bin_id": int(data["b"]),
        }
    except Exception:  # noqa: BLE001 -- malformed cursor is a 400, not a crash
        return None


# ── Handler ──────────────────────────────────────────────────────────


@snapshot_bp.route("/inventory", methods=["GET"])
@require_wms_token
@limiter.limit("2 per minute")
@with_db
def snapshot_inventory():
    try:
        query = SnapshotQuery(
            warehouse_id=request.args.get("warehouse_id", type=int),
            cursor=request.args.get("cursor") or None,
            limit=request.args.get("limit", default=500, type=int),
        )
    except Exception as e:  # noqa: BLE001 -- Pydantic validation
        return jsonify({"error": str(e)}), 400

    token = g.current_token
    allowed_warehouses = list(token.get("warehouse_ids") or [])
    token_id = token.get("token_id")

    # Strict-subset warehouse scope (Decision H).
    if query.warehouse_id not in allowed_warehouses:
        return jsonify({"error": "scope_violation", "field": "warehouse_id"}), 403

    if query.cursor:
        decoded = _decode_cursor(query.cursor)
        if decoded is None:
            return jsonify({"error": "invalid_cursor"}), 400
        scan_id = decoded["scan_id"]
        # Cursor tamper: warehouse_id in cursor must match the query
        # param, and the underlying scan must have been created by
        # this token.
        if decoded["warehouse_id"] != query.warehouse_id:
            return jsonify({"error": "cursor_scope_violation"}), 403
        scan = _load_scan(g.db, scan_id)
        if scan is None:
            return jsonify({"error": "snapshot_expired"}), 410
        if scan["created_by_token_id"] != token_id:
            return jsonify({"error": "cursor_scope_violation"}), 403
        if scan["status"] in ("expired", "aborted"):
            return jsonify({"error": "snapshot_expired"}), 410
        if scan["status"] != "active":
            # A pending-status row on a cursor request means something
            # is badly out of sync; treat as expired.
            return jsonify({"error": "snapshot_expired"}), 410
        last_w, last_i, last_b = decoded["warehouse_id"], decoded["item_id"], decoded["bin_id"]
        pg_snapshot_id = scan["pg_snapshot_id"]
        snapshot_event_id = scan["snapshot_event_id"]
    else:
        # v1.5.1 V-203 (#144): reject 429 before INSERT when this
        # token already holds a pending or active scan. Keeps the
        # keeper pool fair across concurrent tokens; pool size is 4
        # globally and this cap forces spreading across credentials.
        in_flight = g.db.execute(
            text(
                "SELECT COUNT(*) FROM snapshot_scans "
                " WHERE created_by_token_id = :tid "
                "   AND status IN ('pending', 'active')"
            ),
            {"tid": token_id},
        ).scalar()
        if in_flight >= MAX_CONCURRENT_SCANS_PER_TOKEN:
            return (
                jsonify(
                    {
                        "error": "snapshot_in_flight",
                        "message": (
                            "This token already has a snapshot in flight. "
                            "Page it to completion (partial page = done) or "
                            "wait for the keeper's idle timeout before "
                            "starting a new scan."
                        ),
                    }
                ),
                429,
            )

        # First page: mint a pending scan and wait for the keeper
        # to promote it. Timeout 5s; past that the keeper is unhealthy.
        scan_id = str(uuid.uuid4())
        g.db.execute(
            text(
                "INSERT INTO snapshot_scans "
                "(scan_id, warehouse_id, status, created_by_token_id) "
                "VALUES (:sid, :wh, 'pending', :token_id)"
            ),
            {"sid": scan_id, "wh": query.warehouse_id, "token_id": token_id},
        )
        g.db.commit()

        promoted = _wait_for_promotion(g.db, scan_id, KEEPER_PROMOTION_TIMEOUT_S)
        if promoted is None:
            # Clean up the orphan pending row so a downed keeper does
            # not accumulate stale scans.
            g.db.execute(
                text("DELETE FROM snapshot_scans WHERE scan_id = :sid"),
                {"sid": scan_id},
            )
            g.db.commit()
            return jsonify({"error": "snapshot_keeper_unavailable"}), 503
        pg_snapshot_id = promoted["pg_snapshot_id"]
        snapshot_event_id = promoted["snapshot_event_id"]
        # Initial keyset anchor: the "smallest-possible" tuple so the
        # first page starts at the beginning. PostgreSQL accepts the
        # row-value comparison as long as all three are provided.
        last_w, last_i, last_b = query.warehouse_id, 0, 0

    rows = _run_keyset_query(
        pg_snapshot_id=pg_snapshot_id,
        warehouse_id=query.warehouse_id,
        last_w=last_w,
        last_i=last_i,
        last_b=last_b,
        limit=query.limit,
    )

    # Touch last_accessed_at so the keeper's idle-timeout clock is
    # reset while the client is actively paging.
    g.db.execute(
        text(
            "UPDATE snapshot_scans SET last_accessed_at = NOW() "
            " WHERE scan_id = :sid"
        ),
        {"sid": scan_id},
    )

    if len(rows) < query.limit:
        # Partial page => scan is complete. Flip to 'done' so the
        # keeper reaps the row and releases its held transaction.
        g.db.execute(
            text(
                "UPDATE snapshot_scans SET status = 'done' "
                " WHERE scan_id = :sid AND status = 'active'"
            ),
            {"sid": scan_id},
        )
        next_cursor = None
    else:
        tail = rows[-1]
        next_cursor = _encode_cursor(
            scan_id=scan_id,
            warehouse_id=query.warehouse_id,
            item_id=tail["_item_id"],
            bin_id=tail["_bin_id"],
        )

    g.db.commit()

    return jsonify(
        {
            "snapshot_event_id": snapshot_event_id,
            "rows": [_strip_internal_keys(r) for r in rows],
            "next_cursor": next_cursor,
        }
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _load_scan(db, scan_id: str) -> Optional[dict]:
    row = db.execute(
        text(
            "SELECT status, pg_snapshot_id, snapshot_event_id, "
            "       created_by_token_id "
            "  FROM snapshot_scans WHERE scan_id::text = :sid"
        ),
        {"sid": scan_id},
    ).fetchone()
    if row is None:
        return None
    return {
        "status": row.status,
        "pg_snapshot_id": row.pg_snapshot_id,
        "snapshot_event_id": row.snapshot_event_id,
        "created_by_token_id": row.created_by_token_id,
    }


def _wait_for_promotion(db, scan_id: str, timeout_s: float) -> Optional[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = db.execute(
            text(
                "SELECT status, pg_snapshot_id, snapshot_event_id "
                "  FROM snapshot_scans WHERE scan_id::text = :sid"
            ),
            {"sid": scan_id},
        ).fetchone()
        if row is not None and row.status == "active" and row.pg_snapshot_id:
            return {
                "pg_snapshot_id": row.pg_snapshot_id,
                "snapshot_event_id": row.snapshot_event_id,
            }
        # Fresh statement: release the snapshot so the next SELECT
        # sees the keeper's UPDATE (the ambient session uses
        # READ COMMITTED by default, so a commit-free re-select
        # works here even without an explicit rollback).
        db.execute(text("SELECT 1"))
        db.commit()
        time.sleep(KEEPER_POLL_INTERVAL_S)
    return None


def _run_keyset_query(
    pg_snapshot_id: str,
    warehouse_id: int,
    last_w: int,
    last_i: int,
    last_b: int,
    limit: int,
):
    """Open a fresh connection, import the keeper's snapshot, and
    run the keyset-paginated inventory query. Returns a list of
    dicts (each carrying ``_item_id`` / ``_bin_id`` for cursor use,
    stripped before the response is serialised)."""
    import os as _os

    database_url = _os.environ["DATABASE_URL"]
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SET TRANSACTION SNAPSHOT %s", (pg_snapshot_id,))
        cur.execute(
            """
            SELECT i.external_id::text AS item_external_id,
                   inv.item_id,
                   inv.warehouse_id,
                   b.external_id::text AS bin_external_id,
                   inv.bin_id,
                   inv.quantity_on_hand,
                   COALESCE(inv.quantity_allocated, 0) AS quantity_allocated,
                   (inv.quantity_on_hand - COALESCE(inv.quantity_allocated, 0)) AS quantity_available,
                   inv.lot_number
              FROM inventory inv
              JOIN items i ON i.item_id = inv.item_id
              JOIN bins b  ON b.bin_id  = inv.bin_id
             WHERE inv.warehouse_id = %s
               AND (inv.warehouse_id, inv.item_id, inv.bin_id) > (%s, %s, %s)
             ORDER BY inv.warehouse_id, inv.item_id, inv.bin_id
             LIMIT %s
            """,
            (warehouse_id, last_w, last_i, last_b, limit),
        )
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.commit()
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append(
            {
                "item_external_id": r["item_external_id"],
                "warehouse_id": r["warehouse_id"],
                "bin_external_id": r["bin_external_id"],
                "quantity_on_hand": r["quantity_on_hand"],
                "quantity_allocated": r["quantity_allocated"],
                "quantity_available": r["quantity_available"],
                "lot_number": r["lot_number"],
                # Sentry does not track serial numbers at the inventory
                # row level in v1.5.0; emit null so the wire shape
                # stays stable for a future schema bump.
                "serial_number": None,
                "_item_id": r["item_id"],
                "_bin_id": r["bin_id"],
            }
        )
    return out


def _strip_internal_keys(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}
