"""v1.10.0 POS endpoint surface.

Endpoints (this commit):

    GET /api/v1/pos/availability      -- per-warehouse, per-bin stock

The remaining three POST routes (validate-cart, checkout, refund)
arrive in subsequent commits and reuse this blueprint.

Per-request shape:
- @require_wms_token: validates X-WMS-Token, refuses cross-direction
  bridging, refuses tokens without pos.dispatch in endpoints. The
  decorator's V1100 dispatcher branch is what gates this surface.
- @limiter.limit per route, keyed on the token. Availability is the
  high-frequency path (one call per barcode scan); the 120/min budget
  reflects that.
- @with_db opens the request-scoped SQLAlchemy session.
- Every response (success and failure) carries
  X-Sentry-Canonical-Model: DRAFT-v1 so consumers can detect schema-
  stability stage on each response, including 4xx.
- Standard error body shape:
      {"error_kind": str, "message": str, "details": {}}

Path / query parameter validation:
- barcode and sku are matched against ^[A-Za-z0-9_\\-#.]+$ length 1..64
  before any DB query so a malformed value never reaches the
  parameterized binding. The regex is defense-in-depth on top of
  SQLAlchemy's bind parameter handling, mirroring the dockd
  _validate_so_number posture.
"""

import json
import re
import uuid as _uuid

from flask import Blueprint, g, jsonify, make_response, request
from psycopg2.errors import LockNotAvailable, QueryCanceled
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from constants import ACTION_POS_CHECKOUT, ACTION_POS_REFUND
from middleware.auth_middleware import require_wms_token
from middleware.db import with_db
from schemas.pos import CheckoutBody, RefundBody, ValidateCartBody
from services.audit_service import write_audit_log
from services.pos_service import get_max_body_kb, lock_timeouts_ms
from services.rate_limit import limiter
from services.dockd_service import canonical_body_sha256


pos_bp = Blueprint("pos", __name__)


# Lookup-key regex. Matches the dockd so_number shape with a tighter
# 64-char cap (UPCs and SKUs are short; barcodes scanned by the
# Honeywell readers cap at ~50 chars).
_LOOKUP_RE = re.compile(r"^[A-Za-z0-9_\-#.]+$")
_LOOKUP_MAX_LEN = 64


def _draft_response(body, status_code=200, extra_headers=None):
    """Build a Flask response carrying the POS canonical-model header
    on every response (success or failure). Mirrors the dockd helper."""
    response = make_response(jsonify(body), status_code)
    response.headers["X-Sentry-Canonical-Model"] = "DRAFT-v1"
    if extra_headers:
        for k, v in extra_headers.items():
            response.headers[k] = v
    return response


def _err(error_kind, message, status_code, details=None, extra_headers=None):
    return _draft_response(
        {
            "error_kind": error_kind,
            "message": message,
            "details": details or {},
        },
        status_code,
        extra_headers=extra_headers,
    )


def _lock_contention():
    """503 lock_contention with Retry-After: 1 so the POS Service's
    outbox-style retry replays cleanly. Returned from any branch where
    a SET LOCAL lock_timeout fires under SELECT FOR UPDATE / INSERT
    contention."""
    return _err(
        "lock_contention",
        "database is busy; retry shortly",
        503,
        extra_headers={"Retry-After": "1"},
    )


def _validate_lookup_value(value, field_name):
    """Returns None if valid, otherwise an error response."""
    if not value or len(value) > _LOOKUP_MAX_LEN:
        return _err(
            "invalid_query_param",
            f"{field_name} length out of range",
            422,
            {"field": field_name},
        )
    if not _LOOKUP_RE.match(value):
        return _err(
            "invalid_query_param",
            f"{field_name} contains disallowed characters",
            422,
            {"field": field_name},
        )
    return None


# ----------------------------------------------------------------------
# GET /api/v1/pos/availability
# ----------------------------------------------------------------------


@pos_bp.route("/availability", methods=["GET"])
@require_wms_token
@limiter.limit("120 per minute")
@with_db
def availability():
    """Per-warehouse, per-bin availability for one item.

    One of barcode or sku is required (XOR). The response groups
    inventory by warehouse, then by bin. Empty bins (qty <= 0) and
    empty warehouses (sum(qty) <= 0) are omitted entirely; the POS
    Service surfaces the empty case as "out of stock" via the
    `availability: []` shape rather than a 404.

    SKU truly missing OR only present in warehouses outside the token
    scope -> 404 item_not_found (conflated to prevent enumeration).
    """
    barcode = request.args.get("barcode")
    sku = request.args.get("sku")

    if (barcode and sku) or (not barcode and not sku):
        return _err(
            "invalid_query_param",
            "exactly one of barcode or sku is required",
            422,
            {"field": "barcode|sku"},
        )

    if barcode is not None:
        err = _validate_lookup_value(barcode, "barcode")
        if err is not None:
            return err
    if sku is not None:
        err = _validate_lookup_value(sku, "sku")
        if err is not None:
            return err

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Look up the item. Both branches return the same canonical row so
    # the rest of the function does not branch on which key was used.
    if barcode is not None:
        item = g.db.execute(
            text(
                """
                SELECT item_id, sku, item_name, upc, is_active
                  FROM items
                 WHERE upc = :barcode
                 LIMIT 1
                """
            ),
            {"barcode": barcode},
        ).fetchone()
    else:
        item = g.db.execute(
            text(
                """
                SELECT item_id, sku, item_name, upc, is_active
                  FROM items
                 WHERE sku = :sku
                 LIMIT 1
                """
            ),
            {"sku": sku},
        ).fetchone()

    if item is None or not item.is_active:
        return _err("item_not_found", "no item matches the given identifier", 404)

    # No warehouses in token scope: same 404 conflation as dockd.
    if not token_warehouse_ids:
        return _err("item_not_found", "no item matches the given identifier", 404)

    # Pull every inventory row for this item that the token can see.
    # Collapse lots within a (warehouse, bin) so the response shows one
    # qty per bin, not per lot. The lot dimension is internal; the POS
    # surface presents bins.
    rows = g.db.execute(
        text(
            """
            SELECT w.warehouse_code,
                   w.warehouse_name,
                   b.bin_code,
                   SUM(inv.quantity_on_hand - inv.quantity_allocated) AS qty
              FROM inventory inv
              JOIN warehouses w ON w.warehouse_id = inv.warehouse_id
              JOIN bins b       ON b.bin_id       = inv.bin_id
             WHERE inv.item_id      = :item_id
               AND inv.warehouse_id = ANY(:wh_ids)
             GROUP BY w.warehouse_code, w.warehouse_name, b.bin_code
             HAVING SUM(inv.quantity_on_hand - inv.quantity_allocated) > 0
             ORDER BY w.warehouse_code, b.bin_code
            """
        ),
        {"item_id": item.item_id, "wh_ids": token_warehouse_ids},
    ).fetchall()

    # In-scope produced no available stock. Distinguish "genuinely out
    # of stock everywhere" (return 200 [] so the POS Service can show
    # 'out of stock') from "stock only in warehouses outside the token
    # scope" (return 404 to prevent the token from inferring sister-
    # warehouse membership). A single LIMIT 1 probe is enough; we just
    # need to know whether ANY out-of-scope row holds available qty.
    if not rows:
        leak = g.db.execute(
            text(
                """
                SELECT 1
                  FROM inventory inv
                 WHERE inv.item_id      = :item_id
                   AND inv.warehouse_id != ALL(:wh_ids)
                   AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                 LIMIT 1
                """
            ),
            {"item_id": item.item_id, "wh_ids": token_warehouse_ids},
        ).fetchone()
        if leak is not None:
            return _err("item_not_found", "no item matches the given identifier", 404)

    # Group by warehouse. Rows are pre-ordered by warehouse_code so a
    # single pass is enough.
    availability_by_warehouse = []
    current = None
    for r in rows:
        if current is None or current["warehouse_id"] != r.warehouse_code:
            if current is not None:
                availability_by_warehouse.append(current)
            current = {
                "warehouse_id":   r.warehouse_code,
                "warehouse_name": r.warehouse_name,
                "qty_available":  0,
                "bins":           [],
            }
        current["bins"].append({
            "bin_id":   r.bin_code,
            "bin_name": r.bin_code,
            "qty":      int(r.qty),
        })
        current["qty_available"] += int(r.qty)
    if current is not None:
        availability_by_warehouse.append(current)

    body = {
        "sku":          item.sku,
        "name":         item.item_name,
        "barcode":      item.upc,
        # is_taxable is hardcoded true. items has no is_taxable column
        # in v1.10; a follow-up adds the column when a deployment
        # needs tax-exempt SKUs. The POS Service treats every item as
        # taxable under the universal tax rate from its .env.
        "is_taxable":   True,
        "availability": availability_by_warehouse,
    }
    return _draft_response(body, 200)


# ----------------------------------------------------------------------
# POST /api/v1/pos/validate-cart
# ----------------------------------------------------------------------


def _classify_line(row, token_warehouse_ids):
    """Map one bulk-query row to a conflict reason or None.

    Reason precedence (most informative first):
      sku_not_found ->
      item_inactive ->
      warehouse_not_found ->
      warehouse_not_in_scope ->
      bin_not_found ->
      insufficient_stock

    A line that has multiple problems surfaces under the first
    precedence-order reason that applies; the cashier sees the most
    actionable cause without enumerating sister failures.
    """
    if row.item_id is None:
        return "sku_not_found", None
    if not row.is_active:
        return "item_inactive", None
    if row.warehouse_id is None:
        return "warehouse_not_found", None
    if row.warehouse_id not in token_warehouse_ids:
        return "warehouse_not_in_scope", None
    if row.bin_id is None:
        return "bin_not_found", None
    available = int(row.available)
    if available < int(row.requested_qty):
        return "insufficient_stock", available
    return None, None


@pos_bp.route("/validate-cart", methods=["POST"])
@require_wms_token
@limiter.limit("60 per minute")
@with_db
def validate_cart():
    """Pre-flight cart validation called by the POS Service just before
    initiating a Windcave charge. Read-only. Returns 200 valid:true
    when every line passes; 409 valid:false with all conflicts in one
    response when any line fails.
    """
    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        return _err(
            "body_too_large",
            "request body exceeds SENTRY_POS_MAX_BODY_KB",
            413,
            {"max_body_kb": get_max_body_kb()},
        )

    try:
        body = ValidateCartBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ()))
        return _err(
            "invalid_body",
            "body failed schema validation",
            422,
            {"field": field, "reason": first.get("type", "value_error")},
        )
    except Exception:
        return _err("invalid_body", "body is not valid JSON", 422)

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Bulk classification query. unnest() turns the five parallel arrays
    # into one row per line, then LEFT JOINs resolve each lookup
    # independently so a missing item / warehouse / bin produces a NULL
    # column instead of dropping the row. The aggregate keeps only
    # in-scope inventory contributing to available qty (the warehouse-
    # scope conflation lives in the Python classifier, not the SQL).
    rows = g.db.execute(
        text(
            """
            SELECT i.idx,
                   i.sku, i.warehouse_code, i.bin_code, i.requested_qty,
                   itm.item_id, itm.is_active,
                   w.warehouse_id,
                   b.bin_id,
                   COALESCE(SUM(inv.quantity_on_hand - inv.quantity_allocated), 0) AS available
              FROM unnest(
                       CAST(:idxs       AS int[]),
                       CAST(:skus       AS text[]),
                       CAST(:wh_codes   AS text[]),
                       CAST(:bin_codes  AS text[]),
                       CAST(:qtys       AS int[])
                   ) AS i(idx, sku, warehouse_code, bin_code, requested_qty)
              LEFT JOIN items      itm ON itm.sku            = i.sku
              LEFT JOIN warehouses w   ON w.warehouse_code   = i.warehouse_code
              LEFT JOIN bins       b   ON b.bin_code         = i.bin_code
                                       AND b.warehouse_id    = w.warehouse_id
              LEFT JOIN inventory  inv ON inv.item_id        = itm.item_id
                                       AND inv.bin_id        = b.bin_id
                                       AND inv.warehouse_id  = w.warehouse_id
             GROUP BY i.idx, i.sku, i.warehouse_code, i.bin_code, i.requested_qty,
                      itm.item_id, itm.is_active, w.warehouse_id, b.bin_id
             ORDER BY i.idx
            """
        ),
        {
            "idxs":      list(range(len(body.lines))),
            "skus":      [ln.sku for ln in body.lines],
            "wh_codes":  [ln.warehouse_id for ln in body.lines],
            "bin_codes": [ln.bin_id for ln in body.lines],
            "qtys":      [ln.quantity for ln in body.lines],
        },
    ).fetchall()

    conflicts = []
    for row in rows:
        reason, available_qty = _classify_line(row, token_warehouse_ids)
        if reason is None:
            continue
        entry = {
            "line_index":    row.idx,
            "sku":           row.sku,
            "warehouse_id":  row.warehouse_code,
            "bin_id":        row.bin_code,
            "requested_qty": int(row.requested_qty),
            "reason":        reason,
        }
        if available_qty is not None:
            entry["available_qty"] = available_qty
        conflicts.append(entry)

    if not conflicts:
        return _draft_response({"valid": True}, 200)

    return _draft_response({"valid": False, "conflicts": conflicts}, 409)


# ----------------------------------------------------------------------
# POST /api/v1/pos/checkout
# ----------------------------------------------------------------------


def _replay_response(cached_body, headers=None):
    """Return the cached checkout response with X-Idempotent-Replay: true."""
    extra = {"X-Idempotent-Replay": "true"}
    if headers:
        extra.update(headers)
    return _draft_response(cached_body, 200, extra_headers=extra)


def _pydantic_invalid_body(exc):
    """Translate a Pydantic ValidationError into the POS error envelope."""
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first.get("loc", ()))
    return _err(
        "invalid_body",
        "body failed schema validation",
        422,
        {"field": field, "reason": first.get("type", "value_error")},
    )


def _set_lock_timeouts(db):
    """Apply SENTRY_POS_LOCK_TIMEOUT_MS / STATEMENT_TIMEOUT_MS to the
    current transaction. SQLite or any non-Postgres engine in tests
    rejects SET LOCAL; swallow so unit tests against an in-memory
    engine still work. Production is Postgres."""
    lock_ms, stmt_ms = lock_timeouts_ms()
    try:
        db.execute(text(f"SET LOCAL lock_timeout = '{int(lock_ms)}ms'"))
        db.execute(text(f"SET LOCAL statement_timeout = '{int(stmt_ms)}ms'"))
    except OperationalError:
        pass


@pos_bp.route("/checkout", methods=["POST"])
@require_wms_token
@limiter.limit(
    "30 per minute",
    exempt_when=lambda: getattr(g, "_pos_replay_hit", False),
)
@with_db
def checkout():
    """Atomically create a counter-sale SO and decrement inventory.

    Idempotent on idempotency_key. The first call commits the SO and
    caches the response body in sales_orders.cached_response_body; a
    retry with the same key + same body short-circuits to that cached
    response with X-Idempotent-Replay: true. Same key + different body
    returns 409 idempotency_key_reused_with_different_body so the POS
    Service detects a tampered retry instead of silently overwriting.
    """
    # Step 0: body cap.
    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        return _err(
            "body_too_large",
            "request body exceeds SENTRY_POS_MAX_BODY_KB",
            413,
            {"max_body_kb": get_max_body_kb()},
        )

    # Step 0a: parse.
    try:
        body = CheckoutBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        return _pydantic_invalid_body(exc)
    except Exception:
        return _err("invalid_body", "body is not valid JSON", 422)

    body_dict = body.model_dump(mode="json")

    # Step 0b: canonical body hash (idempotency_key excluded).
    body_hash = canonical_body_sha256(body_dict)
    idempotency_key_str = body_dict["idempotency_key"]

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Step 0c: warm-cache replay short-circuit. cached_response_body is
    # written in the same transaction as the SO insert and stays
    # immutable thereafter, so read-committed semantics are sound.
    cached = g.db.execute(
        text(
            """
            SELECT so_id, so_number, idempotency_body_hash, cached_response_body
              FROM sales_orders
             WHERE idempotency_key = :key
             LIMIT 1
            """
        ),
        {"key": idempotency_key_str},
    ).fetchone()
    if cached is not None:
        if cached.idempotency_body_hash != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key matches an existing SO but body differs",
                409,
                {"existing_so_id": cached.so_number},
            )
        if cached.cached_response_body is not None:
            g._pos_replay_hit = True
            return _replay_response(cached.cached_response_body)
        # cached_response_body NULL: a peer is still in-flight under
        # the same key. Fall through; the ON CONFLICT path on our
        # INSERT will block on the unique constraint until the peer
        # commits or aborts.

    # Step 1: per-line warehouse scope check. Wire-level warehouse_id
    # is warehouses.warehouse_code; resolve to integer id for the scope
    # comparison. Out-of-scope warehouses surface as 403 immediately
    # (before any locks) so the cashier sees a fast reject.
    wh_codes = sorted({ln.warehouse_id for ln in body.lines})
    wh_rows = g.db.execute(
        text(
            "SELECT warehouse_id, warehouse_code FROM warehouses "
            " WHERE warehouse_code = ANY(:codes)"
        ),
        {"codes": wh_codes},
    ).fetchall()
    wh_code_to_id = {r.warehouse_code: r.warehouse_id for r in wh_rows}
    for idx, ln in enumerate(body.lines):
        wh_id = wh_code_to_id.get(ln.warehouse_id)
        if wh_id is None:
            # Falls into fulfillment_failed below; classified at step 3.
            continue
        if wh_id not in token_warehouse_ids:
            return _err(
                "warehouse_not_in_scope",
                "token cannot fulfill from this warehouse",
                403,
                {"line_index": idx, "warehouse_id": ln.warehouse_id},
            )

    # Step 2: per-transaction timeouts.
    _set_lock_timeouts(g.db)

    # Step 3: bulk key resolve. unnest() one row per line; LEFT JOIN
    # items / warehouses / bins. Any unresolved row -> 422
    # fulfillment_failed with the offending line_index. The check uses
    # the same b.warehouse_id = w.warehouse_id constraint as validate-
    # cart so a bin in a sister warehouse does not match.
    try:
        resolved = g.db.execute(
            text(
                """
                SELECT i.idx,
                       i.sku, i.warehouse_code, i.bin_code, i.requested_qty,
                       itm.item_id, itm.is_active,
                       w.warehouse_id,
                       b.bin_id
                  FROM unnest(
                           CAST(:idxs       AS int[]),
                           CAST(:skus       AS text[]),
                           CAST(:wh_codes   AS text[]),
                           CAST(:bin_codes  AS text[]),
                           CAST(:qtys       AS int[])
                       ) AS i(idx, sku, warehouse_code, bin_code, requested_qty)
                  LEFT JOIN items      itm ON itm.sku           = i.sku
                  LEFT JOIN warehouses w   ON w.warehouse_code  = i.warehouse_code
                  LEFT JOIN bins       b   ON b.bin_code        = i.bin_code
                                           AND b.warehouse_id   = w.warehouse_id
                 ORDER BY i.idx
                """
            ),
            {
                "idxs":      list(range(len(body.lines))),
                "skus":      [ln.sku for ln in body.lines],
                "wh_codes":  [ln.warehouse_id for ln in body.lines],
                "bin_codes": [ln.bin_id for ln in body.lines],
                "qtys":      [ln.quantity for ln in body.lines],
            },
        ).fetchall()
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _lock_contention()
        raise

    for row in resolved:
        if row.item_id is None or not row.is_active or row.warehouse_id is None or row.bin_id is None:
            g.db.rollback()
            return _err(
                "fulfillment_failed",
                "could not resolve sku / warehouse / bin for line",
                422,
                {
                    "failed_line_index": row.idx,
                    "sku":              row.sku,
                    "warehouse_id":     row.warehouse_code,
                    "bin_id":           row.bin_code,
                },
            )

    # Step 4: pre-fetch so_id; build so_number.
    so_id = g.db.execute(
        text("SELECT nextval('sales_orders_so_id_seq')")
    ).scalar()
    so_number = f"SO-POS-{so_id}"

    # Header warehouse_id: the SO row carries one warehouse_id (NOT
    # NULL); per-line allocations capture the cross-warehouse truth.
    # Pick the first line's resolved warehouse for the header label.
    header_wh_id = resolved[0].warehouse_id
    header_wh_code = resolved[0].warehouse_code

    # Step 5: INSERT sales_orders ON CONFLICT (idempotency_key) DO
    # NOTHING. The unique constraint is the cross-request sentinel;
    # two concurrent retries with the same key cannot both create an
    # SO. ON CONFLICT returning zero rows means a peer committed
    # during step 0c -> re-read for the cached body and replay or 409.
    try:
        inserted = g.db.execute(
            text(
                """
                INSERT INTO sales_orders (
                    so_id, so_number, so_barcode, status, warehouse_id,
                    created_by, created_at, shipped_at, external_id,
                    order_source, order_type,
                    external_txn_ref, idempotency_key, idempotency_body_hash
                ) VALUES (
                    :so_id, :so_number, :so_number, 'SHIPPED', :wh_id,
                    'pos', NOW(), :shipped_at, :ext_id,
                    'pos', 'sale',
                    :external_txn_ref, :idempotency_key, :body_hash
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING so_id
                """
            ),
            {
                "so_id":            so_id,
                "so_number":        so_number,
                "wh_id":            header_wh_id,
                "shipped_at":       body.completed_at,
                "ext_id":           str(_uuid.uuid4()),
                "external_txn_ref": body.external_txn_ref,
                "idempotency_key":  idempotency_key_str,
                "body_hash":        body_hash,
            },
        ).fetchone()
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _lock_contention()
        raise

    if inserted is None:
        # Peer committed under the same key while we were preparing
        # this transaction. Re-read for the cached body.
        peer = g.db.execute(
            text(
                """
                SELECT so_number, idempotency_body_hash, cached_response_body
                  FROM sales_orders
                 WHERE idempotency_key = :key
                """
            ),
            {"key": idempotency_key_str},
        ).fetchone()
        g.db.rollback()
        if peer is None:
            return _lock_contention()
        if peer.idempotency_body_hash != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key matches an existing SO but body differs",
                409,
                {"existing_so_id": peer.so_number},
            )
        if peer.cached_response_body is None:
            return _lock_contention()
        g._pos_replay_hit = True
        return _replay_response(peer.cached_response_body)

    # Step 6: per-line FOR UPDATE + decrement, ORDER BY (item_id, bin_id)
    # deterministic to prevent deadlock between concurrent checkouts
    # touching overlapping inventory.
    sorted_resolved = sorted(
        resolved,
        key=lambda r: (r.item_id, r.bin_id),
    )
    try:
        for r in sorted_resolved:
            ln = body.lines[r.idx]
            inv = g.db.execute(
                text(
                    """
                    SELECT inventory_id, quantity_on_hand, quantity_allocated
                      FROM inventory
                     WHERE item_id = :item_id
                       AND warehouse_id = :wh_id
                       AND bin_id = :bin_id
                     ORDER BY inventory_id
                     LIMIT 1
                     FOR UPDATE
                    """
                ),
                {
                    "item_id": r.item_id,
                    "wh_id":   r.warehouse_id,
                    "bin_id":  r.bin_id,
                },
            ).fetchone()
            available = 0 if inv is None else (
                int(inv.quantity_on_hand) - int(inv.quantity_allocated)
            )
            if inv is None or available < ln.quantity:
                g.db.rollback()
                return _err(
                    "fulfillment_failed",
                    f"could not decrement inventory for line {r.idx}",
                    422,
                    {
                        "failed_line_index": r.idx,
                        "sku":               r.sku,
                        "warehouse_id":      r.warehouse_code,
                        "bin_id":            r.bin_code,
                        "available_qty":     available,
                    },
                )
            # Insert the SO line. POS sales skip the OPEN -> PICKED ->
            # PACKED -> SHIPPED lifecycle: a counter sale is fulfilled
            # the moment Sentry gets the request, so all the per-line
            # quantity columns equal the line quantity and the line
            # status is SHIPPED.
            g.db.execute(
                text(
                    """
                    INSERT INTO sales_order_lines (
                        so_id, item_id, quantity_ordered, quantity_allocated,
                        quantity_picked, quantity_packed, quantity_shipped,
                        line_number, status
                    ) VALUES (
                        :so_id, :item_id, :qty, 0,
                        :qty, :qty, :qty,
                        :line_number, 'SHIPPED'
                    )
                    """
                ),
                {
                    "so_id":       so_id,
                    "item_id":     r.item_id,
                    "qty":         ln.quantity,
                    "line_number": r.idx + 1,
                },
            )
            # Decrement on_hand by the line quantity. POS skips the
            # allocation reservation step so quantity_allocated stays
            # 0 on the inventory row; the available calculation
            # (on_hand - allocated) drops by the line quantity.
            g.db.execute(
                text(
                    """
                    UPDATE inventory
                       SET quantity_on_hand = quantity_on_hand - :qty,
                           updated_at       = NOW()
                     WHERE inventory_id = :inventory_id
                    """
                ),
                {"qty": ln.quantity, "inventory_id": inv.inventory_id},
            )
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _lock_contention()
        raise

    # Step 7: audit_log. Pricing fields ride in details; mig 056 did
    # not add per-line price columns, and the audit log is the
    # archival venue. The hash chain trigger anchors the entry so a
    # post-incident reconstruction is tamper-evident.
    audit_lines = [
        {
            "sku":              ln.sku,
            "warehouse_id":     ln.warehouse_id,
            "bin_id":           ln.bin_id,
            "quantity":         ln.quantity,
            "unit_price_cents": ln.unit_price_cents,
            "tax_cents":        ln.tax_cents,
            "line_total_cents": ln.line_total_cents,
        }
        for ln in body.lines
    ]
    write_audit_log(
        g.db,
        action_type=ACTION_POS_CHECKOUT,
        entity_type="SO",
        entity_id=so_id,
        user_id=body.cashier_id,
        warehouse_id=header_wh_id,
        details={
            "idempotency_key":  idempotency_key_str,
            "external_txn_ref": body.external_txn_ref,
            "terminal_id":      body.terminal_id,
            "so_number":        so_number,
            "total_cents":      body.payment_summary.total_cents,
            "payment_method":   body.payment_summary.method,
            "header_warehouse": header_wh_code,
            "lines":            audit_lines,
        },
    )

    # Step 8 + 9: build the response and cache it on the SO row.
    response_body_dict = {
        "so_id":     so_number,
        "so_number": so_number,
        "replayed":  False,
    }
    g.db.execute(
        text(
            """
            UPDATE sales_orders
               SET cached_response_body = CAST(:body AS jsonb)
             WHERE so_id = :so_id
            """
        ),
        {
            "so_id": so_id,
            "body":  json.dumps(response_body_dict),
        },
    )

    g.db.commit()

    return _draft_response(response_body_dict, 200)


# ----------------------------------------------------------------------
# POST /api/v1/pos/refund
# ----------------------------------------------------------------------


# 90-day refund window from the doc. The original SO must have been
# created within this window for a refund to be accepted.
_REFUND_WINDOW_DAYS = 90


def _bulk_resolve_locations(db, lines_locations):
    """Resolve a list of (sku, warehouse_code, bin_code) tuples to the
    matching (item_id, warehouse_id, bin_id) integer triples via a
    single unnest()-LEFT JOIN query.

    Used by the refund route to translate the audit_log-captured
    line locations of the original sale into internal IDs for the
    SELECT FOR UPDATE + re-increment step. Returns one result row
    per input tuple, in input order; any unresolved row carries
    NULL columns and the caller treats that as a data-integrity
    surprise (the original sale created these inventory rows; they
    should not have been deleted between sale and refund).
    """
    return db.execute(
        text(
            """
            SELECT i.idx, i.sku, i.warehouse_code, i.bin_code, i.qty,
                   itm.item_id,
                   w.warehouse_id,
                   b.bin_id
              FROM unnest(
                       CAST(:idxs       AS int[]),
                       CAST(:skus       AS text[]),
                       CAST(:wh_codes   AS text[]),
                       CAST(:bin_codes  AS text[]),
                       CAST(:qtys       AS int[])
                   ) AS i(idx, sku, warehouse_code, bin_code, qty)
              LEFT JOIN items      itm ON itm.sku           = i.sku
              LEFT JOIN warehouses w   ON w.warehouse_code  = i.warehouse_code
              LEFT JOIN bins       b   ON b.bin_code        = i.bin_code
                                       AND b.warehouse_id   = w.warehouse_id
             ORDER BY i.idx
            """
        ),
        {
            "idxs":      list(range(len(lines_locations))),
            "skus":      [ln["sku"] for ln in lines_locations],
            "wh_codes":  [ln["warehouse_id"] for ln in lines_locations],
            "bin_codes": [ln["bin_id"] for ln in lines_locations],
            "qtys":      [ln["quantity"] for ln in lines_locations],
        },
    ).fetchall()


@pos_bp.route("/refund", methods=["POST"])
@require_wms_token
@limiter.limit(
    "10 per minute",
    exempt_when=lambda: getattr(g, "_pos_replay_hit", False),
)
@with_db
def refund():
    """Atomically reverse a previously-completed POS sale.

    Creates a credit-memo SO (negative-quantity sibling of the
    original) and re-increments inventory back to the original
    warehouse + bin. Idempotent on the refund's idempotency_key
    (separate from the original sale's key). Marks the original SO
    with refunded_at + refund_so_id so a second refund attempt
    surfaces as 422 already_refunded.

    Server-side rules:
    - 90-day window from the original sale's created_at.
    - Tender lock: card sales refund to card, cash sales refund to
      cash. Comparison reads the original payment_method from the
      POS_CHECKOUT audit_log row.
    - Once refunded, never again.
    - Original SO must be POS-source + sale (not refund) + SHIPPED;
      missing / out-of-scope / wrong-source / wrong-state all
      conflate to 404 original_so_not_found to prevent enumeration.
    """
    cap_bytes = get_max_body_kb() * 1024
    if request.content_length is not None and request.content_length > cap_bytes:
        return _err(
            "body_too_large",
            "request body exceeds SENTRY_POS_MAX_BODY_KB",
            413,
            {"max_body_kb": get_max_body_kb()},
        )

    try:
        body = RefundBody.model_validate(request.get_json(silent=False))
    except ValidationError as exc:
        return _pydantic_invalid_body(exc)
    except Exception:
        return _err("invalid_body", "body is not valid JSON", 422)

    body_dict = body.model_dump(mode="json")
    body_hash = canonical_body_sha256(body_dict)
    idempotency_key_str = body_dict["idempotency_key"]

    token_warehouse_ids = list(g.current_token.get("warehouse_ids") or [])

    # Warm-cache replay short-circuit on the REFUND idempotency_key.
    # The credit-memo SO row carries the key; the original sale's row
    # carries its own (different) key.
    cached = g.db.execute(
        text(
            """
            SELECT so_id, so_number, idempotency_body_hash, cached_response_body
              FROM sales_orders
             WHERE idempotency_key = :key
             LIMIT 1
            """
        ),
        {"key": idempotency_key_str},
    ).fetchone()
    if cached is not None:
        if cached.idempotency_body_hash != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key matches an existing refund but body differs",
                409,
                {"existing_refund_so_id": cached.so_number},
            )
        if cached.cached_response_body is not None:
            g._pos_replay_hit = True
            return _replay_response(cached.cached_response_body)
        # cached_response_body NULL: in-flight peer; fall through.

    _set_lock_timeouts(g.db)

    # Lock the original SO. Conflate every "you can't refund this"
    # cause to 404 original_so_not_found (missing, out-of-scope,
    # wrong source, wrong state) so the token cannot enumerate
    # sister-warehouse SOs or distinguish a non-POS SO from a missing
    # one. The 422 conditions (90-day window, tender mismatch,
    # already refunded) are intentional informational responses
    # because the token already knows the SO exists -- they are
    # operator-actionable errors, not enumeration vectors.
    if not token_warehouse_ids:
        return _err("original_so_not_found", "no POS SO found with the given id", 404)

    try:
        original = g.db.execute(
            text(
                """
                SELECT so_id, so_number, status, warehouse_id, created_at,
                       order_source, order_type,
                       refunded_at, refund_so_id
                  FROM sales_orders
                 WHERE so_number      = :osn
                   AND order_source   = 'pos'
                   AND order_type     = 'sale'
                   AND warehouse_id   = ANY(:wh_ids)
                 FOR UPDATE
                """
            ),
            {
                "osn":     body.original_so_id,
                "wh_ids":  token_warehouse_ids,
            },
        ).fetchone()
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _lock_contention()
        raise

    if original is None:
        g.db.rollback()
        return _err("original_so_not_found", "no POS SO found with the given id", 404)

    if original.status != "SHIPPED":
        # Wrong state. Conflate to 404 so a token cannot probe SO
        # state by issuing successive refund attempts.
        g.db.rollback()
        return _err("original_so_not_found", "no POS SO found with the given id", 404)

    if original.refunded_at is not None or original.refund_so_id is not None:
        existing_refund_so_number = None
        if original.refund_so_id is not None:
            row = g.db.execute(
                text("SELECT so_number FROM sales_orders WHERE so_id = :id"),
                {"id": original.refund_so_id},
            ).fetchone()
            if row is not None:
                existing_refund_so_number = row.so_number
        g.db.rollback()
        return _err(
            "already_refunded",
            "original SO has already been refunded",
            422,
            {"existing_refund_so_id": existing_refund_so_number},
        )

    # 90-day window. Comparing the original sale's created_at against
    # NOW(). Postgres handles the interval math.
    window_check = g.db.execute(
        text(
            "SELECT (:created_at >= NOW() - INTERVAL '90 days') AS within_window"
        ),
        {"created_at": original.created_at},
    ).fetchone()
    if not window_check.within_window:
        g.db.rollback()
        return _err(
            "refund_window_expired",
            f"original sale is older than {_REFUND_WINDOW_DAYS} days",
            422,
            {"original_created_at": original.created_at.isoformat()},
        )

    # Tender mismatch + line-location lookup both come from the
    # POS_CHECKOUT audit_log row for the original SO. The audit_log
    # is the canonical archival venue for POS sale details (no
    # per-line price columns on sales_order_lines in v1.10).
    audit_row = g.db.execute(
        text(
            """
            SELECT details
              FROM audit_log
             WHERE entity_type = 'SO'
               AND entity_id   = :so_id
               AND action_type = :action
             ORDER BY log_id ASC
             LIMIT 1
            """
        ),
        {"so_id": original.so_id, "action": ACTION_POS_CHECKOUT},
    ).fetchone()
    if audit_row is None:
        # Data-integrity surprise: every POS checkout writes one
        # POS_CHECKOUT audit row in the same transaction as the SO
        # insert. A missing audit row means audit_log was tampered
        # with or the original SO was created via a different path
        # that bypassed checkout(). Fail closed; no refund without
        # the canonical line-location data.
        g.db.rollback()
        return _err(
            "original_so_not_found",
            "audit details missing for original SO",
            404,
        )

    audit_details = audit_row.details or {}
    original_payment_method = audit_details.get("payment_method")
    refund_method = body.refund_summary.method
    if original_payment_method != refund_method:
        g.db.rollback()
        return _err(
            "tender_mismatch",
            f"{original_payment_method} sale cannot be refunded as {refund_method}",
            422,
            {
                "original_method": original_payment_method,
                "refund_method":   refund_method,
            },
        )

    original_lines_locations = audit_details.get("lines") or []
    if not original_lines_locations:
        g.db.rollback()
        return _err(
            "original_so_not_found",
            "audit details carry no line locations",
            404,
        )

    # Resolve original-line (sku, warehouse_code, bin_code) to internal
    # IDs. Any unresolved row is a data-integrity surprise.
    resolved = _bulk_resolve_locations(g.db, original_lines_locations)
    for r in resolved:
        if r.item_id is None or r.warehouse_id is None or r.bin_id is None:
            g.db.rollback()
            return _err(
                "original_so_not_found",
                "could not resolve original line locations",
                404,
            )

    # Pre-fetch credit-memo so_id; build refund_so_number.
    refund_so_id = g.db.execute(
        text("SELECT nextval('sales_orders_so_id_seq')")
    ).scalar()
    refund_so_number = f"SO-POS-REF-{refund_so_id}"

    # INSERT the credit-memo SO. ON CONFLICT (idempotency_key) DO
    # NOTHING handles the concurrent-retry case; a peer that
    # committed during the warm-cache step gets re-read for replay.
    try:
        inserted = g.db.execute(
            text(
                """
                INSERT INTO sales_orders (
                    so_id, so_number, so_barcode, status, warehouse_id,
                    created_by, created_at, shipped_at, external_id,
                    order_source, order_type, parent_so_id,
                    external_txn_ref, idempotency_key, idempotency_body_hash
                ) VALUES (
                    :so_id, :so_number, :so_number, 'SHIPPED', :wh_id,
                    'pos', NOW(), :shipped_at, :ext_id,
                    'pos', 'refund', :parent_so_id,
                    :external_txn_ref, :idempotency_key, :body_hash
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING so_id
                """
            ),
            {
                "so_id":            refund_so_id,
                "so_number":        refund_so_number,
                "wh_id":            original.warehouse_id,
                "shipped_at":       body.completed_at,
                "ext_id":           str(_uuid.uuid4()),
                "parent_so_id":     original.so_id,
                "external_txn_ref": body.external_refund_ref,
                "idempotency_key":  idempotency_key_str,
                "body_hash":        body_hash,
            },
        ).fetchone()
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _lock_contention()
        raise

    if inserted is None:
        # Peer committed a refund under the same key while we were
        # preparing this transaction. Re-read for the cached body.
        peer = g.db.execute(
            text(
                """
                SELECT so_number, idempotency_body_hash, cached_response_body
                  FROM sales_orders
                 WHERE idempotency_key = :key
                """
            ),
            {"key": idempotency_key_str},
        ).fetchone()
        g.db.rollback()
        if peer is None:
            return _lock_contention()
        if peer.idempotency_body_hash != body_hash:
            return _err(
                "idempotency_key_reused_with_different_body",
                "idempotency_key matches an existing refund but body differs",
                409,
                {"existing_refund_so_id": peer.so_number},
            )
        if peer.cached_response_body is None:
            return _lock_contention()
        g._pos_replay_hit = True
        return _replay_response(peer.cached_response_body)

    # Per-line: SELECT FOR UPDATE inventory, INSERT credit-memo
    # sales_order_lines with NEGATIVE quantities, UPDATE inventory
    # SET on_hand = on_hand + original_qty (re-increment).
    # Deterministic ordering by (item_id, bin_id) prevents deadlock
    # between concurrent refunds touching overlapping inventory.
    sorted_resolved = sorted(resolved, key=lambda r: (r.item_id, r.bin_id))
    try:
        for r in sorted_resolved:
            qty = int(r.qty)
            inv = g.db.execute(
                text(
                    """
                    SELECT inventory_id, quantity_on_hand
                      FROM inventory
                     WHERE item_id      = :item_id
                       AND warehouse_id = :wh_id
                       AND bin_id       = :bin_id
                     ORDER BY inventory_id
                     LIMIT 1
                     FOR UPDATE
                    """
                ),
                {
                    "item_id": r.item_id,
                    "wh_id":   r.warehouse_id,
                    "bin_id":  r.bin_id,
                },
            ).fetchone()
            if inv is None:
                # The original sale created this inventory row.
                # Missing means operator-run SQL (or a deletion bug)
                # has wiped it; refusing to silently fail.
                g.db.rollback()
                return _err(
                    "original_so_not_found",
                    "could not relocate original inventory row",
                    404,
                )
            g.db.execute(
                text(
                    """
                    INSERT INTO sales_order_lines (
                        so_id, item_id, quantity_ordered, quantity_allocated,
                        quantity_picked, quantity_packed, quantity_shipped,
                        line_number, status
                    ) VALUES (
                        :so_id, :item_id, :neg_qty, 0,
                        :neg_qty, :neg_qty, :neg_qty,
                        :line_number, 'SHIPPED'
                    )
                    """
                ),
                {
                    "so_id":       refund_so_id,
                    "item_id":     r.item_id,
                    "neg_qty":     -qty,
                    "line_number": r.idx + 1,
                },
            )
            g.db.execute(
                text(
                    """
                    UPDATE inventory
                       SET quantity_on_hand = quantity_on_hand + :qty,
                           updated_at       = NOW()
                     WHERE inventory_id = :inventory_id
                    """
                ),
                {"qty": qty, "inventory_id": inv.inventory_id},
            )
    except OperationalError as exc:
        if isinstance(exc.orig, (LockNotAvailable, QueryCanceled)):
            g.db.rollback()
            return _lock_contention()
        raise

    # Mark the original SO as refunded.
    g.db.execute(
        text(
            """
            UPDATE sales_orders
               SET refunded_at  = NOW(),
                   refund_so_id = :refund_so_id
             WHERE so_id = :original_so_id
            """
        ),
        {
            "refund_so_id":   refund_so_id,
            "original_so_id": original.so_id,
        },
    )

    # Audit log on the credit-memo SO. Mirrors POS_CHECKOUT details
    # shape so the refund row reads cleanly alongside the sale row
    # in any forensic timeline query.
    write_audit_log(
        g.db,
        action_type=ACTION_POS_REFUND,
        entity_type="SO",
        entity_id=refund_so_id,
        user_id=body.cashier_id,
        warehouse_id=original.warehouse_id,
        details={
            "idempotency_key":           idempotency_key_str,
            "external_refund_ref":       body.external_refund_ref,
            "original_external_txn_ref": body.original_external_txn_ref,
            "original_so_id":            body.original_so_id,
            "refund_so_number":          refund_so_number,
            "terminal_id":               body.terminal_id,
            "total_cents":               body.refund_summary.total_cents,
            "payment_method":            refund_method,
            "lines":                     original_lines_locations,
        },
    )

    response_body_dict = {
        "refund_so_id":   refund_so_number,
        "original_so_id": original.so_number,
        "replayed":       False,
    }
    g.db.execute(
        text(
            """
            UPDATE sales_orders
               SET cached_response_body = CAST(:body AS jsonb)
             WHERE so_id = :so_id
            """
        ),
        {
            "so_id": refund_so_id,
            "body":  json.dumps(response_body_dict),
        },
    )

    g.db.commit()

    return _draft_response(response_body_dict, 200)
