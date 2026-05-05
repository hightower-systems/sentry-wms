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
    """SENTRY_INBOUND_MAX_BODY_KB env var with hard floor 16 KB and
    ceiling 4096 KB (V-201 shape); default 256 KB. Boot validation
    runs separately in app.create_app(); this helper is the read-side
    path the handler uses on each request."""
    raw = os.getenv("SENTRY_INBOUND_MAX_BODY_KB", "256")
    try:
        v = int(raw)
    except ValueError:
        return 256
    return max(16, min(4096, v))


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

    # v1.7.0 (#269): mapping_overrides is disabled until v1.7.1 resolves
    # the source-path-remap vs canonical-value-replacement semantics
    # ambiguity (see #270). Reject the request regardless of token
    # capability so neither shape becomes the de-facto contract by
    # accident. The capability column on wms_tokens stays so the v1.7.1
    # decision lands without a migration.
    overrides = body.get("mapping_overrides")
    if overrides is not None:
        return HandlerError(
            status_code=403,
            body={
                "error_kind": "feature_not_available_in_v1_7_0",
                "detail": (
                    "mapping_overrides is deferred to v1.7.1 pending "
                    "semantics decision (#270)."
                ),
            },
        )
    # Belt-and-braces: even with overrides=None, force the rest of the
    # handler to behave as if the capability is absent. Future v1.7.x
    # shouldn't accidentally reintroduce the value-replacement path
    # without revisiting the gate.
    overrides = None

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
    is_new, canonical_id = _upsert_canonical(
        db, cfg, source_system, external_id, canonical_payload,
        write_field_set,
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
