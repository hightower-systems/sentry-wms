"""ERP-driven cancel via the inbound surface (v1.9.0 #311).

When an external system pushes an updated sales_order whose mapped
status field equals 'CANCELLED' on a row that is NOT already
CANCELLED, inbound_service routes through the shared
sales_order_service.cancel_sales_order. That handler runs the
per-status inventory unwind and writes one ACTION_CANCEL audit row
with source='inbound'.

This file pins the handoff between the inbound service and the
shared cancel handler. Per-status unwind itself is covered in
test_cancel_sales_order_service.py; this file covers the inbound
hookup.
"""

import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

import db_test_context
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


_MAPPING_WITH_STATUS = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "so_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "customer_name"
        source_path: "$.customer.name"
        type: "string"
      - canonical: "status"
        source_path: "$.status"
        type: "string"
"""


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _build_registry(app, ss: str) -> MappingDocument:
    parsed = yaml.safe_load(_MAPPING_WITH_STATUS.format(ss=ss))
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>", sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


def _insert_token_and_allowlist(ss: str, plaintext: str) -> int:
    import hashlib
    import json as _json
    from _wms_token_helpers import PEPPER

    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (ss,),
    )
    token_hash = hashlib.sha256((PEPPER + plaintext).encode()).hexdigest()
    cur.execute(
        "INSERT INTO wms_tokens "
        "(token_name, token_hash, status, warehouse_ids, event_types, "
        " endpoints, source_system, inbound_resources, mapping_override, "
        " mapping_overrides) "
        "VALUES (%s, %s, 'active', %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "RETURNING token_id",
        (
            f"inbound-cancel-{uuid.uuid4().hex[:6]}",
            token_hash,
            [1], [], [], ss, ["sales_orders"], False, _json.dumps({}),
        ),
    )
    token_id = cur.fetchone()[0]
    cur.close()
    return token_id


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scenario(app):
    ss = f"cancel-test-{uuid.uuid4().hex[:8]}"
    _build_registry(app, ss)
    plaintext = f"cancel-token-{uuid.uuid4().hex[:8]}"
    _insert_token_and_allowlist(ss, plaintext)
    return {"ss": ss, "plaintext": plaintext}


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/sales_orders",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _push_so(client, plaintext, *, external_id, version, status, customer="Acme"):
    return _post(client, plaintext, {
        "external_id": external_id,
        "external_version": version,
        "source_payload": {
            "orderNumber": external_id,
            "warehouseId": 1,
            "customer": {"name": customer},
            "status": status,
        },
    })


# ----------------------------------------------------------------------
# Cancel transition via inbound
# ----------------------------------------------------------------------


class TestInboundCancel:
    def test_open_to_cancelled_via_inbound(self, client, app, scenario):
        # Push an OPEN SO first.
        ss = scenario["ss"]
        plaintext = scenario["plaintext"]
        r1 = _push_so(client, plaintext, external_id="SO-CXL-1",
                      version="2026-05-08T10:00:00Z", status="OPEN")
        assert r1.status_code == 201
        canonical_id = r1.get_json()["canonical_id"]

        rows = _query(
            "SELECT so_id, status FROM sales_orders WHERE external_id = %s",
            (canonical_id,),
        )
        so_id, status = rows[0]
        assert status == "OPEN"

        # Now push the same SO with status=CANCELLED.
        r2 = _push_so(client, plaintext, external_id="SO-CXL-1",
                      version="2026-05-08T11:00:00Z", status="CANCELLED")
        assert r2.status_code == 201

        # Status flipped.
        new_status = _query(
            "SELECT status FROM sales_orders WHERE so_id = %s", (so_id,),
        )[0][0]
        assert new_status == "CANCELLED"

        # One ACTION_CANCEL audit row exists with source='inbound'.
        rows = _query(
            "SELECT user_id, details FROM audit_log "
            "WHERE entity_type = 'SO' AND entity_id = %s "
            "  AND action_type = 'CANCEL'",
            (so_id,),
        )
        assert len(rows) == 1
        user_id, details = rows[0]
        assert user_id == f"inbound:{ss}"
        # details is JSONB; psycopg2 returns dict
        assert details["source"] == "inbound"
        assert details["pre_status"] == "OPEN"

    def test_already_cancelled_inbound_repush_writes_no_audit(
        self, client, app, scenario
    ):
        plaintext = scenario["plaintext"]
        # Initial CANCELLED push (treated as first-time-receipt -> normal upsert).
        r1 = _push_so(client, plaintext, external_id="SO-CXL-2",
                      version="2026-05-08T10:00:00Z", status="CANCELLED")
        assert r1.status_code == 201
        canonical_id = r1.get_json()["canonical_id"]

        # Idempotent re-push with same version returns 200; no second audit row.
        r2 = _push_so(client, plaintext, external_id="SO-CXL-2",
                      version="2026-05-08T10:00:00Z", status="CANCELLED")
        assert r2.status_code == 200

        # New version with CANCELLED arrives but row is already CANCELLED;
        # the cancel hook short-circuits and no ACTION_CANCEL row writes.
        r3 = _push_so(client, plaintext, external_id="SO-CXL-2",
                      version="2026-05-08T11:00:00Z", status="CANCELLED")
        assert r3.status_code == 201

        rows = _query(
            "SELECT so_id FROM sales_orders WHERE external_id = %s",
            (canonical_id,),
        )
        so_id = rows[0][0]
        cancel_rows = _query(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE entity_type = 'SO' AND entity_id = %s "
            "  AND action_type = 'CANCEL'",
            (so_id,),
        )[0][0]
        # First-time-receipt with status=CANCELLED writes 0 rows (no
        # transition; SO is created already-cancelled). Subsequent
        # CANCELLED pushes also write 0.
        assert cancel_rows == 0

    def test_audit_chain_intact_after_inbound_cancel(self, client, app, scenario):
        plaintext = scenario["plaintext"]
        _push_so(client, plaintext, external_id="SO-CXL-3",
                 version="2026-05-08T10:00:00Z", status="OPEN")
        _push_so(client, plaintext, external_id="SO-CXL-3",
                 version="2026-05-08T11:00:00Z", status="CANCELLED")
        broken = _query("SELECT verify_audit_log_chain()")[0][0]
        assert broken is None
