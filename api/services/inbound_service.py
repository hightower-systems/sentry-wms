"""Shared 10-step inbound handler for v1.7.0 Pipe B.

One handler implementation services all five inbound POST endpoints
(sales_orders / items / customers / vendors / purchase_orders). The
per-resource route registers a thin wrapper that supplies the
resource_key; everything else (advisory lock, idempotency,
stale-version, mapping apply, canonical upsert + field-set isolation,
cross_system_mappings autocreate, supersession, audit_log) is handled
here.

Per plan §2.4 the handler runs the following steps inside one
transaction. The advisory xact lock ensures no two concurrent inbound
POSTs for the same (source_system, external_id) can both write
'applied' rows; lock release on commit/rollback.

  Step 1: pg_try_advisory_xact_lock(hashtext(source_system || ':' || external_id))
  Step 2: idempotent re-POST short-circuit (200 OK)
  Step 3: stale-version check (409 stale_version)
  Step 4: mapping_loader.apply(); cross_system_lookup_miss raises through
  Step 5: upsert canonical row
  Step 6: cross_system_mappings INSERT on first-time-receipt
  Step 7: insert inbound row + supersede prior 'applied' row
  Step 8: backfill canonical.latest_inbound_id
  Step 9: emit_inbound_outbound_event (mapping table empty in v1.7;
          plan §2.6 keeps the call site for v1.8 to fill in)
  Step 10: audit_log write with field_set + override_fields

line_items are stored in the inbound row's canonical_payload JSONB
column but are NOT written to canonical *_lines tables in v1.7.
That sync lands at v1.8+ once a real consumer demonstrates the
shape; the v1.7 inbound row preserves the full forensic chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID

from psycopg2.extras import Json
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services.mapping_loader import (
    CrossSystemLookupMiss,
    MappingDocument,
    MappingRegistry,
    apply as apply_mapping,
)


_LOG = logging.getLogger("services.inbound_service")


# Per-resource configuration. canonical_id_col is the column on the
# canonical table that the cross_system_mappings.canonical_id and the
# inbound staging table's canonical_id resolve to. For both existing
# (V-216 retrofit) and new tables this is external_id UUID; new tables
# also have a canonical_id PK column that is set equal to external_id
# at first-receipt for shape parity.
@dataclass(frozen=True)
class _ResourceConfig:
    resource_key: str        # plural form, also matches inbound_resources scope
    inbound_table: str       # inbound_<resource>
    canonical_table: str     # warehouse-floor table
    canonical_type: str      # singular form for cross_system_mappings
    has_canonical_id_col: bool  # True for new tables (customers/vendors),
                                 # False for existing tables (sales_orders /
                                 # items / purchase_orders)
    has_updated_at_col: bool    # sales_orders + purchase_orders pre-date the
                                 # updated_at convention (mig 001-019); their
                                 # UPDATE skips the column.
    audit_entity_type: str


_CONFIGS: Dict[str, _ResourceConfig] = {
    "sales_orders": _ResourceConfig(
        resource_key="sales_orders",
        inbound_table="inbound_sales_orders",
        canonical_table="sales_orders",
        canonical_type="sales_order",
        has_canonical_id_col=False,
        has_updated_at_col=False,
        audit_entity_type="INBOUND_SALES_ORDER",
    ),
    "items": _ResourceConfig(
        resource_key="items",
        inbound_table="inbound_items",
        canonical_table="items",
        canonical_type="item",
        has_canonical_id_col=False,
        has_updated_at_col=True,
        audit_entity_type="INBOUND_ITEM",
    ),
    "customers": _ResourceConfig(
        resource_key="customers",
        inbound_table="inbound_customers",
        canonical_table="customers",
        canonical_type="customer",
        has_canonical_id_col=True,
        has_updated_at_col=True,
        audit_entity_type="INBOUND_CUSTOMER",
    ),
    "vendors": _ResourceConfig(
        resource_key="vendors",
        inbound_table="inbound_vendors",
        canonical_table="vendors",
        canonical_type="vendor",
        has_canonical_id_col=True,
        has_updated_at_col=True,
        audit_entity_type="INBOUND_VENDOR",
    ),
    "purchase_orders": _ResourceConfig(
        resource_key="purchase_orders",
        inbound_table="inbound_purchase_orders",
        canonical_table="purchase_orders",
        canonical_type="purchase_order",
        has_canonical_id_col=False,
        has_updated_at_col=False,
        audit_entity_type="INBOUND_PURCHASE_ORDER",
    ),
}


def get_config(resource_key: str) -> _ResourceConfig:
    cfg = _CONFIGS.get(resource_key)
    if cfg is None:
        raise KeyError(f"no inbound config for resource_key={resource_key!r}")
    return cfg


# ============================================================
# Result types
# ============================================================


@dataclass
class HandlerOK:
    status_code: int  # 200 (idempotent re-POST) or 201 (new write)
    body: Dict[str, Any]


@dataclass
class HandlerError:
    status_code: int
    body: Dict[str, Any]
    headers: Optional[Dict[str, str]] = None


HandlerResult = HandlerOK | HandlerError


# ============================================================
# Body-size cap
# ============================================================


def get_max_body_kb() -> int:
    """SENTRY_INBOUND_MAX_BODY_KB env var (16..4096; default 256).
    Boot validates the range in app.create_app() (#273); this helper
    is the read-side path the handler uses on each request and trusts
    the boot guard rather than re-clamping silently."""
    return int(os.getenv("SENTRY_INBOUND_MAX_BODY_KB", "256"))


# ============================================================
# Version comparison
# ============================================================


def _is_newer(strategy: str, server: str, incoming: str) -> bool:
    """Return True iff `server` represents a strictly newer version than
    `incoming`. The comparison strategy is declared by the source's
    mapping document at the top level (version_compare).

    iso_timestamp uses datetime.fromisoformat() so offset-bearing values
    compare correctly across timezones (lex comparison would lie).
    integer parses both sides; lexicographic falls back to plain string
    comparison."""
    if strategy == "iso_timestamp":
        try:
            return datetime.fromisoformat(server) > datetime.fromisoformat(incoming)
        except ValueError:
            return server > incoming
    if strategy == "integer":
        try:
            return int(server) > int(incoming)
        except ValueError:
            return server > incoming
    return server > incoming


# ============================================================
# The handler
# ============================================================


def handle_inbound(
    *,
    db,
    resource_key: str,
    body: Dict[str, Any],
    token: Dict[str, Any],
    registry: MappingRegistry,
    source_txn_id: Optional[uuid.UUID] = None,
) -> HandlerResult:
    """Execute the 10-step inbound flow inside `db` (a SQLAlchemy session
    holding an open transaction).

    Caller is responsible for db.commit() on success and db.rollback()
    on exception. The handler returns a HandlerResult; routes serialise
    that into the Flask response. Routes also handle 422 (Pydantic) +
    413 (body size) before reaching this function.
    """
    cfg = get_config(resource_key)

    # v1.8.0 (#270): mapping_overrides resolution.
    #
    # Per-request body-level overrides (Option A) stay rejected: the
    # connector author cannot remap canonical fields by sneaking JSON
    # into individual POSTs. Body-level Option A may land in v1.x if
    # real demand surfaces; for v1.8 the surface is locked.
    if body.get("mapping_overrides") is not None:
        return HandlerError(
            status_code=403,
            body={
                "error_kind": "mapping_overrides_not_supported_in_body",
                "detail": (
                    "Per-request mapping_overrides is not supported. Issue "
                    "a token with mapping_override=true and the desired "
                    "mapping_overrides JSONB; per-token static overrides "
                    "apply automatically. See docs/erp-integration.md."
                ),
            },
        )
    # Per-token static overrides (Option B) apply only when both the
    # capability flag is TRUE and the JSONB is non-empty. Migration 052
    # ensures the column is NOT NULL DEFAULT '{}' so the empty-dict
    # path is the universal default.
    overrides = None
    if token.get("mapping_override") and token.get("mapping_overrides"):
        overrides = token["mapping_overrides"]

    source_system: str = token["source_system"]
    external_id: str = body["external_id"]
    external_version: str = body["external_version"]
    source_payload: Dict[str, Any] = body["source_payload"]
    token_id: int = token["token_id"]

    # Mapping doc is loaded at boot; refusal here means the boot-time
    # cross-check is broken (allowlist permits a source the loader did
    # not load). Treat as 503 so operators see a clear "this should have
    # failed at boot" signal.
    document = registry.for_source(source_system)
    if document is None:
        return HandlerError(
            status_code=503,
            body={"error_kind": "mapping_document_not_loaded",
                  "message": f"No mapping document loaded for source_system "
                             f"{source_system!r}. Restart required after "
                             f"placing the YAML at db/mappings/."},
        )

    # ----- Step 1: advisory xact lock -----
    locked = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
        {"k": f"{source_system}:{external_id}"},
    ).scalar()
    if not locked:
        return HandlerError(
            status_code=409,
            body={"error_kind": "lock_held",
                  "message": "Concurrent upsert in progress for this external_id; retry."},
            headers={"Retry-After": "1"},
        )

    # ----- Step 2: idempotent re-POST -----
    existing = db.execute(
        text(
            f"SELECT inbound_id, canonical_id, received_at "
            f"  FROM {cfg.inbound_table} "
            f" WHERE source_system = :ss "
            f"   AND external_id = :eid "
            f"   AND external_version = :ev"
        ),
        {"ss": source_system, "eid": external_id, "ev": external_version},
    ).fetchone()
    if existing is not None:
        return HandlerOK(
            status_code=200,
            body={
                "inbound_id": existing.inbound_id,
                "canonical_id": str(existing.canonical_id),
                "canonical_type": cfg.canonical_type,
                "received_at": _iso(existing.received_at),
                "warning": _DRAFT_WARNING,
            },
        )

    # ----- Step 3: stale-version -----
    current = db.execute(
        text(
            f"SELECT external_version, received_at "
            f"  FROM {cfg.inbound_table} "
            f" WHERE source_system = :ss "
            f"   AND external_id = :eid "
            f"   AND status = 'applied' "
            f" ORDER BY received_at DESC LIMIT 1"
        ),
        {"ss": source_system, "eid": external_id},
    ).fetchone()
    if current is not None and _is_newer(
        document.version_compare, current.external_version, external_version,
    ):
        return HandlerError(
            status_code=409,
            body={
                "error_kind": "stale_version",
                "current_version": current.external_version,
                "current_received_at": _iso(current.received_at),
                "message": "A newer version exists for this external_id; "
                           "refetch and retry if needed.",
            },
        )

    # ----- Step 4: apply mapping -----
    def _lookup(ss: str, st: str, sid: str) -> Optional[UUID]:
        row = db.execute(
            text(
                "SELECT canonical_id FROM cross_system_mappings "
                " WHERE source_system = :ss AND source_type = :st "
                "   AND source_id = :sid"
            ),
            {"ss": ss, "st": st, "sid": sid},
        ).fetchone()
        return row.canonical_id if row else None

    try:
        canonical_payload = apply_mapping(
            document, resource_key, source_payload,
            lookup_fn=_lookup, override=overrides,
        )
    except CrossSystemLookupMiss as miss:
        return HandlerError(
            status_code=409,
            body={
                "error_kind": "cross_system_lookup_miss",
                "missing": {
                    "source_system": miss.source_system,
                    "source_type": miss.source_type,
                    "source_id": miss.source_id,
                },
                "message": "Required cross-system lookup did not resolve. "
                           "Ensure the referenced entity has been ingested.",
            },
        )
    except ValueError as exc:
        return HandlerError(
            status_code=422,
            body={"error_kind": "mapping_apply_error",
                  "message": str(exc)},
        )

    # ----- Step 5 + 6: canonical upsert + cross_system_mappings -----
    # write_field_set is the union of base mapping fields and any
    # override fields. Filtering by union keeps the field-set isolation
    # contract on subsequent writers (only declared fields written) while
    # letting mapping_override capability tokens carry override-only
    # columns through to the canonical row.
    write_field_set = (
        document.field_set(resource_key) | set(overrides.keys())
        if overrides
        else document.field_set(resource_key)
    )

    # v1.8.0 (#300): warehouse_id token fallback. When the resolved
    # canonical_payload has no warehouse_id (source did not provide,
    # mapping doc did not declare a default) AND the token's
    # warehouse_ids array carries at least one entry, fill in the
    # first entry. Single-warehouse tokens (the common case for a
    # connector author who scopes a token to one site) get the
    # natural fallback without per-mapping-doc plumbing. Multi-
    # warehouse tokens still take the first entry; operators who
    # need different routing per inbound POST should set warehouse_id
    # in source or declare a mapping doc default. Only sales_orders +
    # purchase_orders have a warehouse_id column (items / customers
    # / vendors are warehouse-agnostic by design).
    if (
        cfg.canonical_table in ("sales_orders", "purchase_orders")
        and not canonical_payload.get("warehouse_id")
        and token.get("warehouse_ids")
    ):
        canonical_payload["warehouse_id"] = int(token["warehouse_ids"][0])
        write_field_set = write_field_set | {"warehouse_id"}

    # v1.9.0 #311: ERP-driven cancel detection. When the inbound
    # canonical_payload carries status='CANCELLED' on an existing SO
    # whose canonical row is NOT already CANCELLED, route through the
    # shared sales_order_service.cancel_sales_order so the inventory
    # unwind + audit_log row land identically to the admin cancel path.
    # First-time-receipt case (no cross_system_mappings row yet) falls
    # through to the normal upsert; a brand-new SO landing as CANCELLED
    # just inserts with status='CANCELLED' and no inventory state to
    # unwind. The cancel call sets status='CANCELLED' itself so the
    # subsequent _upsert_canonical UPDATE is a no-op for that field
    # while still applying any other field updates the mapping doc
    # declares (customer_name, address fields, etc.).
    if (
        cfg.canonical_table == "sales_orders"
        and canonical_payload.get("status") == "CANCELLED"
    ):
        existing_so = db.execute(
            text(
                "SELECT so.so_id, so.status "
                "  FROM sales_orders so "
                "  JOIN cross_system_mappings csm ON csm.canonical_id = so.external_id "
                " WHERE csm.source_system = :ss "
                "   AND csm.source_type   = :st "
                "   AND csm.source_id     = :sid"
            ),
            {"ss": source_system, "st": cfg.canonical_type, "sid": external_id},
        ).fetchone()
        if existing_so is not None and existing_so.status != "CANCELLED":
            from services.sales_order_service import cancel_sales_order
            cancel_sales_order(
                db,
                so_id=existing_so.so_id,
                source="inbound",
                username=f"inbound:{source_system}",
            )

    is_new, canonical_id = _upsert_canonical(
        db, cfg, source_system, external_id, canonical_payload,
        write_field_set,
    )

    # ----- Step 6.5: line-item write-through (v1.8.0 #289) -----
    # PO + SO resources flow line_items through to the relational
    # *_lines tables so receiving / picking can scan against them.
    # Other resources (items / customers / vendors) have no line tables
    # and the helper short-circuits.
    try:
        _write_inbound_lines(
            db, document, resource_key, canonical_id, canonical_payload,
        )
    except CrossSystemLookupMiss as miss:
        return HandlerError(
            status_code=409,
            body={
                "error_kind": "cross_system_lookup_miss",
                "missing": {
                    "source_system": miss.source_system,
                    "source_type": miss.source_type,
                    "source_id": miss.source_id,
                },
                "message": (
                    "Required cross-system lookup did not resolve on a "
                    "line item. Ensure the referenced entity has been "
                    "ingested."
                ),
            },
        )
    except _LinesInFlight as exc:
        return HandlerError(
            status_code=409,
            body={
                "error_kind": "lines_in_flight",
                "message": str(exc),
            },
        )
    except ValueError as exc:
        return HandlerError(
            status_code=422,
            body={"error_kind": "mapping_apply_error",
                  "message": str(exc)},
        )

    # ----- Step 7: insert inbound row + supersede -----
    db.execute(
        text(
            f"UPDATE {cfg.inbound_table} "
            f"   SET status = 'superseded', superseded_at = NOW() "
            f" WHERE source_system = :ss AND external_id = :eid "
            f"   AND status = 'applied'"
        ),
        {"ss": source_system, "eid": external_id},
    )
    inbound_row = db.execute(
        text(
            f"INSERT INTO {cfg.inbound_table} "
            f"  (source_system, external_id, external_version, canonical_id, "
            f"   canonical_payload, source_payload, ingested_via_token_id) "
            f"VALUES (:ss, :eid, :ev, :cid, :cp, :sp, :tid) "
            f"RETURNING inbound_id, received_at"
        ),
        {
            "ss": source_system, "eid": external_id, "ev": external_version,
            "cid": str(canonical_id),
            "cp": json.dumps(canonical_payload, default=_json_default),
            "sp": json.dumps(source_payload, default=_json_default),
            "tid": token_id,
        },
    ).fetchone()

    # ----- Step 8: latest_inbound_id backfill -----
    db.execute(
        text(
            f"UPDATE {cfg.canonical_table} SET latest_inbound_id = :iid "
            f" WHERE external_id = :cid"
        ),
        {"iid": inbound_row.inbound_id, "cid": str(canonical_id)},
    )

    # ----- Step 9: emit (deferred per plan §2.6; mapping table empty) -----

    # ----- Step 10: audit_log -----
    base_fields = document.field_set(resource_key)
    override_fields = set(overrides.keys()) if overrides else set()
    field_set = sorted(base_fields | override_fields)
    _write_audit_log(
        db,
        action_type="CREATE" if is_new else "UPDATE",
        entity_type=cfg.audit_entity_type,
        entity_id=inbound_row.inbound_id,
        token_id=token_id,
        details={
            "source_system": source_system,
            "external_id": external_id,
            "field_set": field_set,
            "override_fields": sorted(override_fields),
            "source_txn_id": str(source_txn_id) if source_txn_id else None,
        },
    )

    return HandlerOK(
        status_code=201,
        body={
            "inbound_id": inbound_row.inbound_id,
            "canonical_id": str(canonical_id),
            "canonical_type": cfg.canonical_type,
            "received_at": _iso(inbound_row.received_at),
            "warning": _DRAFT_WARNING,
        },
    )


# ============================================================
# Helpers
# ============================================================


_DRAFT_WARNING = (
    "Canonical model is DRAFT in v1.7.0. Schema may change at v2.0 "
    "(NetSuite validation)."
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Decimal):
        # v1.8.0 (#285): mapping_loader now coerces type='decimal'
        # to Decimal. Stored as a string in the JSONB so investigators
        # can recover the exact value (vs JSON's lossy float).
        return str(value)
    raise TypeError(f"non-serialisable value {type(value).__name__}: {value!r}")


def _upsert_canonical(
    db,
    cfg: _ResourceConfig,
    source_system: str,
    external_id: str,
    canonical_payload: Dict[str, Any],
    field_set: set,
) -> Tuple[bool, UUID]:
    """Return (is_new, canonical_id).

    First-time-receipt path (no cross_system_mappings row): generate a
    fresh UUID, INSERT canonical with the field_set columns, INSERT
    cross_system_mappings.

    Subsequent path: read canonical_id from cross_system_mappings, UPDATE
    canonical with only the field_set columns + updated_at +
    latest_inbound_id stays NULL until step 8. line_items (any list
    value in canonical_payload) are excluded from the canonical write
    by design (v1.7 first-pass; line tables sync at v1.8+)."""

    existing_mapping = db.execute(
        text(
            "SELECT canonical_id FROM cross_system_mappings "
            " WHERE source_system = :ss AND source_type = :st "
            "   AND source_id = :sid"
        ),
        {"ss": source_system, "st": cfg.canonical_type, "sid": external_id},
    ).fetchone()

    # Filter to columns the mapping declares; line_items (lists) excluded.
    write_payload = {
        k: v for k, v in canonical_payload.items()
        if k in field_set and not isinstance(v, list)
    }

    if existing_mapping is None:
        # First-time-receipt: INSERT canonical + INSERT cross_system_mappings.
        canonical_id = uuid.uuid4()
        # Existing tables (V-216 retrofit): set external_id only.
        # New tables (customers/vendors): set both canonical_id and external_id
        # equal so the cross_system_mappings.canonical_id resolves consistently.
        cols = list(write_payload.keys()) + ["external_id"]
        vals = list(write_payload.values()) + [canonical_id]
        if cfg.has_canonical_id_col:
            cols.append("canonical_id")
            vals.append(canonical_id)
        placeholders = ", ".join([f":{c}" for c in cols])
        col_list = ", ".join(cols)
        params = {c: v for c, v in zip(cols, vals)}
        try:
            db.execute(
                text(f"INSERT INTO {cfg.canonical_table} ({col_list}) "
                     f"VALUES ({placeholders})"),
                params,
            )
        except IntegrityError as exc:
            # Likely a NOT NULL miss on a column the mapping doc didn't
            # cover. Let the caller surface the message; the routes layer
            # turns this into a 422 with the column name.
            raise
        db.execute(
            text(
                "INSERT INTO cross_system_mappings "
                "  (source_system, source_type, source_id, "
                "   canonical_type, canonical_id) "
                "VALUES (:ss, :st, :sid, :ct, :cid)"
            ),
            {
                "ss": source_system, "st": cfg.canonical_type,
                "sid": external_id, "ct": cfg.canonical_type,
                "cid": str(canonical_id),
            },
        )
        return True, canonical_id

    # Subsequent path: UPDATE existing canonical with only the field_set.
    canonical_id = existing_mapping.canonical_id
    if write_payload:
        set_clause = ", ".join([f"{c} = :{c}" for c in write_payload.keys()])
        if cfg.has_updated_at_col:
            set_clause += ", updated_at = NOW()"
        params = dict(write_payload)
        params["cid"] = str(canonical_id)
        db.execute(
            text(f"UPDATE {cfg.canonical_table} "
                 f"   SET {set_clause} "
                 f" WHERE external_id = :cid"),
            params,
        )
    db.execute(
        text(
            "UPDATE cross_system_mappings "
            "   SET last_updated_at = NOW() "
            " WHERE source_system = :ss AND source_type = :st "
            "   AND source_id = :sid"
        ),
        {"ss": source_system, "st": cfg.canonical_type, "sid": external_id},
    )
    return False, canonical_id


def _write_audit_log(
    db,
    *,
    action_type: str,
    entity_type: str,
    entity_id: int,
    token_id: int,
    details: Dict[str, Any],
) -> None:
    """audit_log INSERT for inbound writes. user_id is the v1.7
    boot-identity convention extended to inbound: 'system:wms_token:<id>'
    so the chain attribution lands on the issuing token rather than
    a human user."""
    db.execute(
        text(
            "INSERT INTO audit_log "
            "  (action_type, entity_type, entity_id, user_id, details) "
            "VALUES (:at, :et, :eid, :uid, :d)"
        ),
        {
            "at": action_type,
            "et": entity_type,
            "eid": entity_id,
            "uid": f"system:wms_token:{token_id}",
            "d": json.dumps(details, default=_json_default),
        },
    )


# ============================================================
# v1.8.0 (#289) Line-item write-through
# ============================================================
#
# v1.7 stored line_items only in inbound_<resource>.canonical_payload
# JSONB; the relational *_lines tables stayed empty, leaving inbound
# POs unreceivable and inbound SOs unallocatable. v1.8 walks the
# resolved canonical_payload[<canonical_path>] list after the header
# upsert and writes lines to purchase_order_lines / sales_order_lines
# with FK to the just-upserted header.
#
# Items / customers / vendors do not have line_items wiring; the
# helper short-circuits.


class _LinesInFlight(Exception):
    """Existing lines have downstream activity (PO: quantity_received
    > 0; SO: any quantity_(allocated|picked|packed|shipped) > 0).
    Replacing them would silently lose the warehouse-floor state, so
    the handler returns 409 instead. Operator cancels or completes the
    in-flight work before re-POSTing the upstream record."""


_LINE_RESOURCE_SPECS = {
    "purchase_orders": {
        "header_pk_col": "po_id",
        "line_table": "purchase_order_lines",
        "downstream_predicate": "quantity_received > 0",
        "downstream_label": "quantity_received",
    },
    "sales_orders": {
        "header_pk_col": "so_id",
        "line_table": "sales_order_lines",
        "downstream_predicate": (
            "quantity_allocated > 0 OR quantity_picked > 0 "
            "OR quantity_packed > 0 OR quantity_shipped > 0"
        ),
        "downstream_label": "quantity_allocated/picked/packed/shipped",
    },
}


def _resolve_item_int_id(db, item_external_id) -> Optional[int]:
    """Translate an items.external_id UUID to the integer items.item_id
    FK target. Returns None when no row matches; caller raises
    CrossSystemLookupMiss with the unresolved UUID."""
    if item_external_id is None:
        return None
    row = db.execute(
        text("SELECT item_id FROM items WHERE external_id = :eid"),
        {"eid": str(item_external_id)},
    ).fetchone()
    return row.item_id if row else None


def _write_inbound_lines(db, document, resource_key, canonical_id,
                          canonical_payload) -> None:
    """v1.8.0 (#289): write inbound lines to the relational *_lines
    table for purchase_orders + sales_orders.

    No-op when:
    - the resource has no line wiring (items / customers / vendors), OR
    - the mapping doc declares no line_items block, OR
    - the resolved line list is empty (header-only update; preserves
      existing relational lines so a metadata re-POST does not nuke
      receiving / picking work).

    Raises:
    - CrossSystemLookupMiss when a line's item_id (canonical UUID)
      does not resolve to an items row.
    - _LinesInFlight when existing lines have downstream activity
      and replacement would lose state.
    - ValueError on mapping shape errors (missing item_id / quantity
      on a line) so the handler can surface a 422 with a clear
      message.
    """
    spec = _LINE_RESOURCE_SPECS.get(resource_key)
    if spec is None:
        return  # items / customers / vendors

    rm = document.resources.get(resource_key)
    if rm is None or rm.line_items is None:
        return  # mapping doc declares no line_items block

    lines = canonical_payload.get(rm.line_items.canonical_path) or []
    if not lines:
        return  # header-only update preserves existing relational lines

    # Resolve integer header PK; the just-upserted row must exist.
    header_pk_col = spec["header_pk_col"]
    canonical_table = resource_key  # plural form == canonical table name
    header_row = db.execute(
        text(
            f"SELECT {header_pk_col} FROM {canonical_table} "
            f" WHERE external_id = :cid"
        ),
        {"cid": str(canonical_id)},
    ).fetchone()
    if header_row is None:
        raise RuntimeError(
            f"line write-through: {canonical_table} row missing for "
            f"canonical_id={canonical_id}"
        )
    header_pk = getattr(header_row, header_pk_col)

    # Downstream-activity gate.
    line_table = spec["line_table"]
    in_flight = db.execute(
        text(
            f"SELECT COUNT(*) FROM {line_table} "
            f" WHERE {header_pk_col} = :hpk "
            f"   AND ({spec['downstream_predicate']})"
        ),
        {"hpk": header_pk},
    ).scalar()
    if in_flight > 0:
        raise _LinesInFlight(
            f"{resource_key}: {in_flight} existing line(s) on "
            f"{header_pk_col}={header_pk} have downstream activity "
            f"({spec['downstream_label']}). Cancel or complete the "
            f"in-flight work before re-POST."
        )

    # Resolve each line: item UUID -> integer item_id; required quantity.
    resolved: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        item_uuid = line.get("item_id")
        if item_uuid is None:
            raise ValueError(
                f"{resource_key} line {idx}: item_id is required "
                "(declare cross_system_lookup on the line_items field)"
            )
        item_int_id = _resolve_item_int_id(db, item_uuid)
        if item_int_id is None:
            raise CrossSystemLookupMiss(
                source_system=document.source_system,
                source_type="item",
                source_id=str(item_uuid),
            )
        qty = line.get("quantity_ordered")
        if qty is None:
            raise ValueError(
                f"{resource_key} line {idx}: quantity_ordered is required"
            )
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            raise ValueError(
                f"{resource_key} line {idx}: quantity_ordered "
                f"{qty!r} is not an integer"
            )
        if qty_int <= 0:
            raise ValueError(
                f"{resource_key} line {idx}: quantity_ordered must be > 0"
            )
        line_number = line.get("line_number")
        if line_number is None:
            line_number = idx + 1
        resolved.append({
            "hpk": header_pk,
            "item_id": item_int_id,
            "qty": qty_int,
            "ln": int(line_number),
        })

    # Replace lines: DELETE existing + INSERT new. Both purchase_order_lines
    # and sales_order_lines accept (header_pk, item_id, quantity_ordered,
    # line_number); other columns default. Items / customers / vendors are
    # excluded above so the literal column lists below are exhaustive.
    db.execute(
        text(f"DELETE FROM {line_table} WHERE {header_pk_col} = :hpk"),
        {"hpk": header_pk},
    )
    if resource_key == "purchase_orders":
        for params in resolved:
            db.execute(
                text(
                    "INSERT INTO purchase_order_lines "
                    "(po_id, item_id, quantity_ordered, line_number) "
                    "VALUES (:hpk, :item_id, :qty, :ln)"
                ),
                params,
            )
    else:  # sales_orders
        for params in resolved:
            db.execute(
                text(
                    "INSERT INTO sales_order_lines "
                    "(so_id, item_id, quantity_ordered, line_number) "
                    "VALUES (:hpk, :item_id, :qty, :ln)"
                ),
                params,
            )
