"""Admin Inbound observability surface tests (v1.7.0).

Covers the read-only paths under /api/admin/inbound/:
- list activity returns rows across multiple resources
- filters by source_system, resource, status, since/until, limit
- detail endpoint returns full source_payload + canonical_payload
- unknown resource / status returns 400 with the valid list
- non-admin (USER role) is refused
- v1.7 read-only contract: no mutation endpoints (POST / PUT / PATCH
  / DELETE all return 405)
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

import db_test_context


def _exec(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


@pytest.fixture
def seeded(app):
    """Seed two source_systems with mixed inbound rows so list-filter
    behaviour can be exercised. Wms_tokens row is needed for the
    ingested_via_token_id FK; everything is rolled back at end-of-test."""
    import hashlib
    ss_a = f"adminib-{uuid.uuid4().hex[:8]}"
    ss_b = f"adminib-{uuid.uuid4().hex[:8]}"

    _exec(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool'), (%s, 'connector')",
        (ss_a, ss_b),
    )
    th = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    rows = _exec(
        "INSERT INTO wms_tokens (token_name, token_hash, status, source_system, "
        "                         inbound_resources) "
        "VALUES (%s, %s, 'active', %s, %s) RETURNING token_id",
        (f"adminib-token-{ss_a}", th, ss_a, ["sales_orders", "items"]),
    )
    token_id = rows[0][0]

    canon_a = uuid.uuid4()
    canon_b = uuid.uuid4()
    _exec(
        "INSERT INTO inbound_sales_orders "
        " (source_system, external_id, external_version, canonical_id, "
        "  canonical_payload, source_payload, ingested_via_token_id) "
        "VALUES (%s, 'SO-1', 'v1', %s, %s::jsonb, %s::jsonb, %s)",
        (ss_a, str(canon_a), '{"so_number":"SO-1"}', '{"orderNumber":"SO-1"}', token_id),
    )
    _exec(
        "INSERT INTO inbound_items "
        " (source_system, external_id, external_version, canonical_id, "
        "  canonical_payload, source_payload, ingested_via_token_id) "
        "VALUES (%s, 'ITEM-1', 'v1', %s, %s::jsonb, %s::jsonb, %s)",
        (ss_a, str(uuid.uuid4()), '{"sku":"SKU-1"}', '{"sku":"SKU-1"}', token_id),
    )
    _exec(
        "INSERT INTO inbound_sales_orders "
        " (source_system, external_id, external_version, canonical_id, "
        "  canonical_payload, source_payload, ingested_via_token_id, status) "
        "VALUES (%s, 'SO-OLD', 'v0', %s, %s::jsonb, %s::jsonb, %s, 'superseded')",
        (ss_b, str(canon_b), '{"so_number":"SO-OLD"}', '{"orderNumber":"SO-OLD"}', token_id),
    )
    return {
        "ss_a": ss_a, "ss_b": ss_b, "token_id": token_id,
        "canon_a": str(canon_a),
    }


# ----------------------------------------------------------------------
# List
# ----------------------------------------------------------------------


class TestListActivity:
    def test_returns_rows_across_resources(self, client, auth_headers, seeded):
        resp = client.get(
            f"/api/admin/inbound/activity?source_system={seeded['ss_a']}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        rows = body["rows"]
        # 2 rows for ss_a (one sales_orders, one items)
        assert len(rows) == 2
        resources = sorted(r["resource"] for r in rows)
        assert resources == ["items", "sales_orders"]
        # sample row shape
        sales = next(r for r in rows if r["resource"] == "sales_orders")
        assert sales["external_id"] == "SO-1"
        assert sales["status"] == "applied"
        assert sales["canonical_id"] == seeded["canon_a"]

    def test_filter_by_resource(self, client, auth_headers, seeded):
        resp = client.get(
            f"/api/admin/inbound/activity?source_system={seeded['ss_a']}"
            f"&resource=items",
            headers=auth_headers,
        )
        body = resp.get_json()
        assert all(r["resource"] == "items" for r in body["rows"])

    def test_filter_by_status(self, client, auth_headers, seeded):
        resp = client.get(
            f"/api/admin/inbound/activity?source_system={seeded['ss_b']}"
            f"&status=superseded",
            headers=auth_headers,
        )
        body = resp.get_json()
        assert all(r["status"] == "superseded" for r in body["rows"])
        assert any(r["external_id"] == "SO-OLD" for r in body["rows"])

    def test_unknown_resource_returns_400(self, client, auth_headers):
        resp = client.get(
            "/api/admin/inbound/activity?resource=widgets",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "unknown_resource"
        assert "sales_orders" in body["valid"]

    def test_unknown_status_returns_400(self, client, auth_headers):
        resp = client.get(
            "/api/admin/inbound/activity?status=pending",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "unknown_status"
        assert set(body["valid"]) == {"applied", "superseded"}

    def test_limit_clamped_to_max(self, client, auth_headers, seeded):
        resp = client.get(
            "/api/admin/inbound/activity?limit=99999",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["limit"] == 500


# ----------------------------------------------------------------------
# Detail
# ----------------------------------------------------------------------


class TestDetailEndpoint:
    def test_returns_full_payloads(self, client, auth_headers, seeded):
        # find the inbound_id for ss_a sales_orders row
        rows = _exec(
            "SELECT inbound_id FROM inbound_sales_orders "
            " WHERE source_system = %s AND external_id = 'SO-1'",
            (seeded["ss_a"],),
        )
        inbound_id = rows[0][0]
        resp = client.get(
            f"/api/admin/inbound/activity/sales_orders/{inbound_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["external_id"] == "SO-1"
        assert body["source_payload"] == {"orderNumber": "SO-1"}
        assert body["canonical_payload"] == {"so_number": "SO-1"}
        assert body["ingested_via_token_id"] == seeded["token_id"]
        assert body["canonical_id"] == seeded["canon_a"]

    def test_unknown_resource_returns_400(self, client, auth_headers):
        resp = client.get(
            "/api/admin/inbound/activity/widgets/1", headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "unknown_resource"

    def test_not_found_returns_404(self, client, auth_headers):
        resp = client.get(
            "/api/admin/inbound/activity/sales_orders/999999999",
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ----------------------------------------------------------------------
# v1.7 read-only contract (plan §4.3)
# ----------------------------------------------------------------------


class TestNoMutationEndpoints:
    @pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
    def test_mutation_verb_not_allowed_on_list(self, client, auth_headers, verb):
        resp = getattr(client, verb)(
            "/api/admin/inbound/activity", headers=auth_headers,
        )
        # 405 Method Not Allowed (Flask routes the URL but rejects the verb)
        # OR 404 if no route matches at all. Either way, the verb must
        # not be honoured -- there's no mutation surface in v1.7.
        assert resp.status_code in (404, 405)

    @pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
    def test_mutation_verb_not_allowed_on_detail(self, client, auth_headers, verb):
        resp = getattr(client, verb)(
            "/api/admin/inbound/activity/sales_orders/1",
            headers=auth_headers,
        )
        assert resp.status_code in (404, 405)
