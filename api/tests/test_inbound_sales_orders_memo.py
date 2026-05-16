"""Inbound sales_orders.memo round-trip (v1.9.0).

The memo column is inbound-mappable: a connector author declares
`canonical: "memo"` in their mapping doc, the inbound POST carries
the value in source_payload, and the canonical row stores it. This
file pins that round-trip so a future change to the inbound service
or canonical-column validator does not silently break the memo path.
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


_MAPPING_WITH_MEMO = """\
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
      - canonical: "memo"
        source_path: "$.notes"
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
    parsed = yaml.safe_load(_MAPPING_WITH_MEMO.format(ss=ss))
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
            f"inbound-memo-{uuid.uuid4().hex[:6]}",
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
    ss = f"memo-test-{uuid.uuid4().hex[:8]}"
    _build_registry(app, ss)
    plaintext = f"memo-token-{uuid.uuid4().hex[:8]}"
    _insert_token_and_allowlist(ss, plaintext)
    return {"ss": ss, "plaintext": plaintext}


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/sales_orders",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


class TestInboundMemo:
    def test_memo_round_trips_to_canonical(self, client, app, scenario):
        plaintext = scenario["plaintext"]
        resp = _post(client, plaintext, {
            "external_id": "SO-MEMO-1",
            "external_version": "2026-05-08T10:00:00Z",
            "source_payload": {
                "orderNumber": "SO-MEMO-1",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                "notes": "leave at back door",
            },
        })
        assert resp.status_code == 201
        canonical_id = resp.get_json()["canonical_id"]

        memo = _query(
            "SELECT memo FROM sales_orders WHERE external_id = %s",
            (canonical_id,),
        )[0][0]
        assert memo == "leave at back door"

    def test_memo_omitted_lands_as_null(self, client, app, scenario):
        plaintext = scenario["plaintext"]
        resp = _post(client, plaintext, {
            "external_id": "SO-MEMO-2",
            "external_version": "2026-05-08T10:00:00Z",
            "source_payload": {
                "orderNumber": "SO-MEMO-2",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                # no notes field
            },
        })
        assert resp.status_code == 201
        canonical_id = resp.get_json()["canonical_id"]

        memo = _query(
            "SELECT memo FROM sales_orders WHERE external_id = %s",
            (canonical_id,),
        )[0][0]
        assert memo is None

    def test_memo_supersession_overwrites(self, client, app, scenario):
        plaintext = scenario["plaintext"]
        _post(client, plaintext, {
            "external_id": "SO-MEMO-3",
            "external_version": "2026-05-08T10:00:00Z",
            "source_payload": {
                "orderNumber": "SO-MEMO-3",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                "notes": "first note",
            },
        })
        _post(client, plaintext, {
            "external_id": "SO-MEMO-3",
            "external_version": "2026-05-08T11:00:00Z",
            "source_payload": {
                "orderNumber": "SO-MEMO-3",
                "warehouseId": 1,
                "customer": {"name": "Acme"},
                "notes": "second note",
            },
        })
        memo = _query(
            "SELECT memo FROM sales_orders "
            " WHERE external_id IN ("
            "   SELECT canonical_id FROM cross_system_mappings "
            "    WHERE source_system = %s AND source_id = 'SO-MEMO-3'"
            " )",
            (scenario["ss"],),
        )[0][0]
        assert memo == "second note"
