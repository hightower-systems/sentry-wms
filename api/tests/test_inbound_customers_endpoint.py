"""POST /api/v1/inbound/customers end-to-end tests (v1.7.0).

customers is the first new-canonical-table endpoint. The handler's
has_canonical_id_col=True branch sets BOTH canonical_id (PK) and
external_id (V-216 alias) on the new row at first-write so
cross_system_mappings.canonical_id resolves consistently. This file
verifies that pairing plus the customers-specific surface
(field-set isolation in particular -- the canonical UPDATE on
subsequent writers should touch only the columns the writer's
mapping doc declared).
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


_CUSTOMERS_MAPPING_AB = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "customer_name"
        source_path: "$.name"
        type: "string"
      - canonical: "email"
        source_path: "$.email"
        type: "string"
      - canonical: "phone"
        source_path: "$.phone"
        type: "string"
"""


# Source A maps email + phone; source B maps a different field set.
_CUSTOMERS_MAPPING_A = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "customer_name"
        source_path: "$.name"
        type: "string"
        required: true
      - canonical: "email"
        source_path: "$.email"
        type: "string"
      - canonical: "phone"
        source_path: "$.phone"
        type: "string"
"""


_CUSTOMERS_MAPPING_B = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "lexicographic"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "billing_address"
        source_path: "$.billingAddress"
        type: "string"
"""


def _load_one(app, ss: str, body_yaml: str) -> MappingDocument:
    parsed = yaml.safe_load(body_yaml)
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>",
        sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


def _load_two(app, ss_a: str, body_a: str, ss_b: str, body_b: str):
    """Load two source_systems' mapping docs into one registry so a
    field-set isolation test can fire writes from both against the same
    canonical entity (resolved via cross_system_mappings)."""
    registry = MappingRegistry()
    for ss, body in ((ss_a, body_a), (ss_b, body_b)):
        parsed = yaml.safe_load(body)
        doc = MappingDocument.model_validate(parsed)
        registry.register(LoadedMappingFile(
            document=doc, path=f"<test:{ss}>",
            sha256="0" * 64,
        ))
    app.config["MAPPING_REGISTRY"] = registry


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
                f"customers-test-{uuid.uuid4().hex[:6]}",
                token_hash, "active",
                [1], [], [], ss,
                kw.get("inbound_resources", ["customers"]),
                False,
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
        "/api/v1/inbound/customers",
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
    return {
        "ss": f"custtest-{uuid.uuid4().hex[:8]}",
        "ss_b": f"custtest-{uuid.uuid4().hex[:8]}",
    }


# ----------------------------------------------------------------------


class TestCustomersEndpoint:
    def test_first_post_sets_canonical_id_equals_external_id(
        self, client, app, scenario
    ):
        """customers has both canonical_id PK and external_id UNIQUE
        columns. First-write must set them equal so
        cross_system_mappings.canonical_id resolves consistently to
        the external_id alias the inbound staging table also uses."""
        ss = scenario["ss"]
        _load_one(app, ss, _CUSTOMERS_MAPPING_A.format(ss=ss))
        _insert_token_via_test_conn(ss, "cust-1")
        resp = _post(client, "cust-1", {
            "external_id": "C-1",
            "external_version": "v1",
            "source_payload": {
                "name": "Acme",
                "email": "ops@acme.example",
                "phone": "555-1212",
            },
        })
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["canonical_type"] == "customer"

        rows = _query(
            "SELECT canonical_id, external_id, customer_name, email, phone, "
            "       latest_inbound_id, billing_address, is_active, tax_id "
            "  FROM customers WHERE external_id = %s",
            (body["canonical_id"],),
        )
        assert rows
        canon, ext, name, email, phone, lib, billing, is_active, tax_id = rows[0]
        assert str(canon) == str(ext)  # canonical_id == external_id at first-write
        assert str(canon) == body["canonical_id"]
        assert name == "Acme"
        assert email == "ops@acme.example"
        assert phone == "555-1212"
        assert lib == body["inbound_id"]
        # Conservative-NOT-NULL posture: untouched columns remain NULL.
        assert billing is None
        assert is_active is None
        assert tax_id is None

    def test_field_set_isolation_on_subsequent_write(
        self, client, app, scenario
    ):
        """Source A writes (customer_name, email, phone). Source B then
        writes via cross_system_lookup-resolved canonical_id and only
        declares billing_address. The canonical row should carry both
        contributions; B must not overwrite A's email / phone."""
        ss_a = scenario["ss"]
        ss_b = scenario["ss_b"]
        _load_two(app, ss_a, _CUSTOMERS_MAPPING_A.format(ss=ss_a),
                  ss_b, _CUSTOMERS_MAPPING_B.format(ss=ss_b))
        _insert_token_via_test_conn(ss_a, "cust-A")
        _insert_token_via_test_conn(ss_b, "cust-B")

        # Source A: first-write.
        rA = _post(client, "cust-A", {
            "external_id": "C-FS",
            "external_version": "v1",
            "source_payload": {
                "name": "Acme",
                "email": "ops@acme.example",
                "phone": "555-1212",
            },
        })
        assert rA.status_code == 201
        canonical_id = rA.get_json()["canonical_id"]

        # cross_system_mappings: pre-insert source B's mapping pointing at
        # the same canonical_id so source B's POST resolves via the
        # cross-system path rather than creating a fresh canonical row.
        # The handler's first-receipt detection looks at
        # (source_system, source_type, source_id) -- we INSERT the mapping
        # row for B explicitly so its first POST is treated as a
        # subsequent write to canonical_id.
        conn = db_test_context.get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO cross_system_mappings "
            "  (source_system, source_type, source_id, canonical_type, canonical_id) "
            "VALUES (%s, 'customer', 'C-FS', 'customer', %s)",
            (ss_b, canonical_id),
        )
        cur.close()

        # Source B: writes only billing_address; should NOT touch email / phone.
        rB = _post(client, "cust-B", {
            "external_id": "C-FS",
            "external_version": "v1",
            "source_payload": {"billingAddress": "1 Main St"},
        })
        assert rB.status_code == 201
        # B resolves to the same canonical_id (cross-system mapping
        # already exists pointing at A's canonical row).
        assert rB.get_json()["canonical_id"] == canonical_id

        rows = _query(
            "SELECT customer_name, email, phone, billing_address "
            "  FROM customers WHERE canonical_id = %s",
            (canonical_id,),
        )
        assert rows
        name, email, phone, billing = rows[0]
        assert name == "Acme", "B must not overwrite A's customer_name"
        assert email == "ops@acme.example", "B's mapping does not declare email"
        assert phone == "555-1212", "B's mapping does not declare phone"
        assert billing == "1 Main St", "B should have written billing_address"

        # audit_log captures both writes' field_set + override_fields.
        rows = _query(
            "SELECT details FROM audit_log "
            " WHERE entity_type='INBOUND_CUSTOMER' "
            "   AND details->>'external_id' = 'C-FS' "
            " ORDER BY created_at",
        )
        assert len(rows) == 2
        a_details, b_details = rows[0][0], rows[1][0]
        assert set(a_details["field_set"]) == {"customer_name", "email", "phone"}
        assert set(b_details["field_set"]) == {"billing_address"}
        assert a_details["override_fields"] == []
        assert b_details["override_fields"] == []
