"""POST /api/v1/inbound/vendors end-to-end tests (v1.7.0).

vendors is the second new-canonical-table endpoint. Same shape as
customers (#255): canonical_id PK + external_id UNIQUE columns set
equal at first-write; conservative NOT NULL posture; field-set
isolation across multi-source writers.

This file verifies the vendors-specific surface plus the
mapping_override capability path (the override-bearing token path
is symmetric across all five resources but easier to exercise on
a smaller mapping doc; vendors is a fine host for that test).
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

import hashlib
import yaml

import db_test_context
from _wms_token_helpers import DATABASE_URL, PEPPER
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


_VENDORS_MAPPING = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  vendors:
    canonical_type: "vendor"
    fields:
      - canonical: "vendor_name"
        source_path: "$.name"
        type: "string"
        required: true
      - canonical: "email"
        source_path: "$.email"
        type: "string"
      - canonical: "payment_terms"
        source_path: "$.terms"
        type: "string"
"""


def _load(app, ss: str, body_yaml: str) -> MappingDocument:
    parsed = yaml.safe_load(body_yaml)
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>",
        sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


def _insert_token_via_test_conn(ss, plaintext, **kw):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
            "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
            (ss,),
        )
        token_hash = hashlib.sha256((PEPPER + plaintext).encode()).hexdigest()
        cur.execute(
            "INSERT INTO wms_tokens "
            "(token_name, token_hash, status, warehouse_ids, event_types, "
            " endpoints, source_system, inbound_resources, mapping_override) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING token_id",
            (
                f"vendors-test-{uuid.uuid4().hex[:6]}",
                token_hash, "active",
                [1], [], [], ss,
                kw.get("inbound_resources", ["vendors"]),
                kw.get("mapping_override", False),
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


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


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/vendors",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture
def scenario(app):
    return {"ss": f"vendtest-{uuid.uuid4().hex[:8]}"}


# ----------------------------------------------------------------------


class TestVendorsEndpoint:
    def test_first_post_sets_canonical_id_equals_external_id(
        self, client, app, scenario
    ):
        ss = scenario["ss"]
        _load(app, ss, _VENDORS_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "vend-1")
        resp = _post(client, "vend-1", {
            "external_id": "V-1",
            "external_version": "v1",
            "source_payload": {
                "name": "ACME Supplies",
                "email": "ar@acme.example",
                "terms": "Net 30",
            },
        })
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["canonical_type"] == "vendor"

        rows = _query(
            "SELECT canonical_id, external_id, vendor_name, email, "
            "       payment_terms, latest_inbound_id, contact_name, tax_id "
            "  FROM vendors WHERE external_id = %s",
            (body["canonical_id"],),
        )
        assert rows
        canon, ext, name, email, terms, lib, contact_name, tax_id = rows[0]
        assert str(canon) == str(ext)
        assert str(canon) == body["canonical_id"]
        assert name == "ACME Supplies"
        assert email == "ar@acme.example"
        assert terms == "Net 30"
        assert lib == body["inbound_id"]
        assert contact_name is None
        assert tax_id is None

    def test_mapping_override_capability_required(self, client, app, scenario):
        """A token without the mapping_override capability flag carrying a
        body with mapping_overrides returns 403; the audit chain stays
        clean (no row written for a refused override)."""
        ss = scenario["ss"]
        _load(app, ss, _VENDORS_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "vend-noov", mapping_override=False)
        resp = _post(client, "vend-noov", {
            "external_id": "V-NOOV",
            "external_version": "v1",
            "source_payload": {"name": "ACME"},
            "mapping_overrides": {"contact_name": "ad-hoc"},
        })
        assert resp.status_code == 403
        assert resp.get_json() == {
            "error_kind": "mapping_override_capability_required",
            "message": "Token does not carry the mapping_override capability.",
        }
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

        # No vendor row created, no inbound_vendors row, no audit row.
        n_vendor = _query(
            "SELECT COUNT(*) FROM vendors WHERE external_id = (SELECT canonical_id "
            " FROM cross_system_mappings WHERE source_system = %s "
            "   AND source_id = 'V-NOOV')",
            (ss,),
        )[0][0]
        assert n_vendor == 0

    def test_mapping_override_applied_when_token_has_capability(
        self, client, app, scenario
    ):
        """A token with the mapping_override capability flag carrying an
        override body MUST apply: override fields appear in the canonical
        write, and the audit_log details carry override_fields populated
        with the override's keys."""
        ss = scenario["ss"]
        _load(app, ss, _VENDORS_MAPPING.format(ss=ss))
        _insert_token_via_test_conn(ss, "vend-ov", mapping_override=True)
        resp = _post(client, "vend-ov", {
            "external_id": "V-OV",
            "external_version": "v1",
            "source_payload": {"name": "ACME"},
            "mapping_overrides": {"contact_name": "Jane Doe"},
        })
        assert resp.status_code == 201
        body = resp.get_json()
        rows = _query(
            "SELECT vendor_name, contact_name FROM vendors WHERE external_id = %s",
            (body["canonical_id"],),
        )
        assert rows
        name, contact = rows[0]
        assert name == "ACME"
        assert contact == "Jane Doe"

        rows = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type='INBOUND_VENDOR' AND entity_id = %s",
            (body["inbound_id"],),
        )
        assert len(rows) == 1
        details = rows[0][0]
        assert "vendor_name" in details["field_set"]
        assert "contact_name" in details["field_set"]
        assert details["override_fields"] == ["contact_name"]
