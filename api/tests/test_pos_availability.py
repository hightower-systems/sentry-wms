"""GET /api/v1/pos/availability contract (v1.10.0 POS Endpoint 1).

Coverage:
- Happy path: stock in two warehouses, two bins per warehouse, returns
  full per-warehouse / per-bin shape.
- Query-param contract: barcode XOR sku, malformed value, missing both,
  both supplied -> 422 invalid_query_param.
- Item not in items -> 404 item_not_found.
- Item exists but no inventory anywhere visible -> 200 with
  availability: [] (NOT 404; "out of stock" is different from "missing").
- Inventory in an out-of-scope warehouse -> 404 conflated, not leaked.
- quantity_available = quantity_on_hand - quantity_allocated math:
  rows with allocated >= on_hand contribute nothing.
- Warehouses with sum(qty) <= 0 are omitted entirely.
- DRAFT-v1 header on every response, success or failure.
- 401 invalid_token from the decorator pre-dispatcher path.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from _wms_token_helpers import delete_token, insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def pos_token(seed_data):
    """Pure-direction POS register token: pos.dispatch slug, warehouse 1
    in scope, no inbound / outbound markers."""
    plaintext = f"pos-test-{uuid.uuid4()}"
    token_id = insert_token(
        name="POS Register 1",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["pos.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


@pytest.fixture()
def pos_token_other_warehouse(seed_data):
    plaintext = f"pos-test-wh99-{uuid.uuid4()}"
    token_id = insert_token(
        name="POS Other Warehouse",
        plaintext=plaintext,
        warehouse_ids=[99],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["pos.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


def _insert_warehouse(code, name):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO warehouses (warehouse_code, warehouse_name, address) "
        "VALUES (%s, %s, %s) RETURNING warehouse_id",
        (code, name, "test address"),
    )
    wh_id = cur.fetchone()[0]
    cur.close()
    return wh_id


def _insert_bin(warehouse_id, bin_code, zone_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bins "
        "(zone_id, warehouse_id, bin_code, bin_barcode, bin_type, "
        " pick_sequence, putaway_sequence, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING bin_id",
        (zone_id, warehouse_id, bin_code, bin_code, "Pickable",
         0, 0, str(uuid.uuid4())),
    )
    bin_id = cur.fetchone()[0]
    cur.close()
    return bin_id


def _insert_item(sku, item_name="Test Item", upc=None, is_active=True):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, is_active, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING item_id",
        (sku, item_name, upc, is_active, str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_inventory(item_id, bin_id, warehouse_id, on_hand, allocated=0):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory "
        "(item_id, bin_id, warehouse_id, quantity_on_hand, quantity_allocated) "
        "VALUES (%s, %s, %s, %s, %s)",
        (item_id, bin_id, warehouse_id, on_hand, allocated),
    )
    cur.close()


# ----------------------------------------------------------------------
# Auth + DRAFT header
# ----------------------------------------------------------------------


class TestAuthAndHeader:
    def test_missing_token_returns_401(self, client, seed_data):
        resp = client.get("/api/v1/pos/availability?sku=TST-001")
        assert resp.status_code == 401

    def test_unknown_token_returns_401(self, client, seed_data):
        resp = client.get(
            "/api/v1/pos/availability?sku=TST-001",
            headers={"X-WMS-Token": "not-real"},
        )
        assert resp.status_code == 401

    def test_happy_response_carries_draft_header(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability?sku=TST-001",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 200
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_404_response_carries_draft_header(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability?sku=DOES-NOT-EXIST",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 404
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_422_response_carries_draft_header(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability?sku=has spaces",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 422
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"


# ----------------------------------------------------------------------
# Query-param contract
# ----------------------------------------------------------------------


class TestQueryParams:
    def test_neither_barcode_nor_sku_returns_422(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "invalid_query_param"

    def test_both_barcode_and_sku_returns_422(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability?sku=TST-001&barcode=100000000001",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "invalid_query_param"

    def test_malformed_sku_with_disallowed_chars_returns_422(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability?sku=has@chars",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "invalid_query_param"
        assert body["details"]["field"] == "sku"

    def test_malformed_barcode_with_disallowed_chars_returns_422(
        self, client, pos_token
    ):
        resp = client.get(
            "/api/v1/pos/availability?barcode=has spaces",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["error_kind"] == "invalid_query_param"
        assert body["details"]["field"] == "barcode"

    def test_sku_too_long_returns_422(self, client, pos_token):
        long = "A" * 65
        resp = client.get(
            f"/api/v1/pos/availability?sku={long}",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 422


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPathSeededData:
    def test_seeded_sku_returns_inventory_in_apt_lab(self, client, pos_token):
        """TST-001 is seeded with 50 units in bin 3 (A-01-01) of
        warehouse 1 (APT-LAB)."""
        resp = client.get(
            "/api/v1/pos/availability?sku=TST-001",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["sku"] == "TST-001"
        assert body["barcode"] == "100000000001"
        assert body["is_taxable"] is True
        assert len(body["availability"]) == 1
        wh = body["availability"][0]
        assert wh["warehouse_id"] == "APT-LAB"
        assert wh["warehouse_name"] == "Apartment Test Lab"
        assert wh["qty_available"] == 50
        assert len(wh["bins"]) == 1
        assert wh["bins"][0]["qty"] == 50

    def test_seeded_barcode_lookup_matches_sku_lookup(self, client, pos_token):
        sku_resp = client.get(
            "/api/v1/pos/availability?sku=TST-001",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        bc_resp = client.get(
            "/api/v1/pos/availability?barcode=100000000001",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert sku_resp.get_json() == bc_resp.get_json()


class TestMultiWarehouseMultiBin:
    def test_two_warehouses_two_bins_each(self, client, seed_data):
        store_wh = _insert_warehouse(f"store-{uuid.uuid4().hex[:6]}", "Retail Floor")
        afc_wh = _insert_warehouse(f"afc-{uuid.uuid4().hex[:6]}", "AFC Warehouse")

        store_bin1 = _insert_bin(store_wh, f"S-{uuid.uuid4().hex[:6]}")
        store_bin2 = _insert_bin(store_wh, f"S-{uuid.uuid4().hex[:6]}")
        afc_bin1 = _insert_bin(afc_wh, f"A-{uuid.uuid4().hex[:6]}")
        afc_bin2 = _insert_bin(afc_wh, f"A-{uuid.uuid4().hex[:6]}")

        sku = f"MW-{uuid.uuid4().hex[:6]}"
        item_id = _insert_item(sku=sku, upc=None)

        _insert_inventory(item_id, store_bin1, store_wh, 1)
        _insert_inventory(item_id, store_bin2, store_wh, 0)  # omit
        _insert_inventory(item_id, afc_bin1, afc_wh, 24)
        _insert_inventory(item_id, afc_bin2, afc_wh, 16)

        plaintext = f"mw-token-{uuid.uuid4()}"
        token_id = insert_token(
            name="Multi-WH",
            plaintext=plaintext,
            warehouse_ids=[store_wh, afc_wh],
            event_types=[],
            inbound_resources=[],
            source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = client.get(
                f"/api/v1/pos/availability?sku={sku}",
                headers={"X-WMS-Token": plaintext},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert len(body["availability"]) == 2
            by_code = {wh["warehouse_id"]: wh for wh in body["availability"]}
            store_block = by_code[next(c for c in by_code if c.startswith("store-"))]
            afc_block = by_code[next(c for c in by_code if c.startswith("afc-"))]
            assert store_block["qty_available"] == 1
            assert len(store_block["bins"]) == 1
            assert afc_block["qty_available"] == 40
            assert len(afc_block["bins"]) == 2
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Out-of-stock vs not-found
# ----------------------------------------------------------------------


class TestOutOfStockVsMissing:
    def test_unknown_sku_returns_404(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/availability?sku=NEVER-EXISTED",
            headers={"X-WMS-Token": pos_token["plaintext"]},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error_kind"] == "item_not_found"

    def test_inactive_sku_returns_404(self, client, seed_data):
        sku = f"INACTIVE-{uuid.uuid4().hex[:6]}"
        _insert_item(sku=sku, is_active=False)
        plaintext = f"pos-inactive-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext,
            warehouse_ids=[1],
            event_types=[],
            inbound_resources=[],
            source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = client.get(
                f"/api/v1/pos/availability?sku={sku}",
                headers={"X-WMS-Token": plaintext},
            )
            assert resp.status_code == 404
        finally:
            delete_token(token_id)

    def test_sku_with_no_inventory_anywhere_returns_200_empty(
        self, client, seed_data
    ):
        sku = f"EMPTY-{uuid.uuid4().hex[:6]}"
        _insert_item(sku=sku)
        plaintext = f"pos-empty-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext,
            warehouse_ids=[1],
            event_types=[],
            inbound_resources=[],
            source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = client.get(
                f"/api/v1/pos/availability?sku={sku}",
                headers={"X-WMS-Token": plaintext},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["sku"] == sku
            assert body["availability"] == []
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Warehouse scope (no enumeration oracle)
# ----------------------------------------------------------------------


class TestWarehouseScope:
    def test_sku_in_other_warehouse_returns_404(
        self, client, pos_token_other_warehouse
    ):
        """TST-001 has stock in warehouse 1; a token scoped to
        warehouse 99 sees the same 404 as a genuinely missing SKU.
        This is the dual-query conflation: the in-scope SELECT is
        empty, the out-of-scope leak probe finds qty>0, so the
        response collapses to 404."""
        resp = client.get(
            "/api/v1/pos/availability?sku=TST-001",
            headers={"X-WMS-Token": pos_token_other_warehouse["plaintext"]},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error_kind"] == "item_not_found"

    def test_out_of_scope_stock_returns_404_not_empty_array(
        self, client, seed_data
    ):
        """Regression test for the bug shipped in #322 and fixed in
        #323. A SKU with available stock only in out-of-scope
        warehouses must return 404, not 200 with availability: [].
        The empty-array shape is reserved for "genuinely out of stock
        everywhere"; the 404 prevents a token from inferring sister-
        warehouse membership."""
        wh_a = _insert_warehouse(f"a-{uuid.uuid4().hex[:6]}", "Warehouse A")
        wh_b = _insert_warehouse(f"b-{uuid.uuid4().hex[:6]}", "Warehouse B")
        bin_a = _insert_bin(wh_a, f"BA-{uuid.uuid4().hex[:6]}")
        sku = f"OOS-{uuid.uuid4().hex[:6]}"
        item_id = _insert_item(sku=sku)
        # Stock exists in warehouse A only.
        _insert_inventory(item_id, bin_a, wh_a, on_hand=10, allocated=0)

        # Token scoped to warehouse B sees nothing in scope but the
        # leak probe finds qty>0 in A.
        plaintext = f"oos-token-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext,
            warehouse_ids=[wh_b],
            event_types=[],
            inbound_resources=[],
            source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = client.get(
                f"/api/v1/pos/availability?sku={sku}",
                headers={"X-WMS-Token": plaintext},
            )
            assert resp.status_code == 404
            body = resp.get_json()
            assert body["error_kind"] == "item_not_found"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# quantity_available math: on_hand - allocated
# ----------------------------------------------------------------------


class TestAvailableMath:
    def test_fully_allocated_inventory_omits_bin(self, client, seed_data):
        """on_hand=10, allocated=10 -> qty_available=0; the bin must
        be omitted from the response, and if it's the only bin in the
        warehouse the warehouse is omitted too."""
        wh_id = _insert_warehouse(f"alloc-{uuid.uuid4().hex[:6]}", "Allocated WH")
        bin_id = _insert_bin(wh_id, f"B-{uuid.uuid4().hex[:6]}")
        sku = f"ALLOC-{uuid.uuid4().hex[:6]}"
        item_id = _insert_item(sku=sku)
        _insert_inventory(item_id, bin_id, wh_id, on_hand=10, allocated=10)

        plaintext = f"alloc-token-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext,
            warehouse_ids=[wh_id],
            event_types=[],
            inbound_resources=[],
            source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = client.get(
                f"/api/v1/pos/availability?sku={sku}",
                headers={"X-WMS-Token": plaintext},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["availability"] == []
        finally:
            delete_token(token_id)

    def test_partial_allocation_returns_remainder(self, client, seed_data):
        """on_hand=10, allocated=3 -> qty_available=7."""
        wh_id = _insert_warehouse(f"part-{uuid.uuid4().hex[:6]}", "Partial WH")
        bin_id = _insert_bin(wh_id, f"B-{uuid.uuid4().hex[:6]}")
        sku = f"PART-{uuid.uuid4().hex[:6]}"
        item_id = _insert_item(sku=sku)
        _insert_inventory(item_id, bin_id, wh_id, on_hand=10, allocated=3)

        plaintext = f"part-token-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext,
            warehouse_ids=[wh_id],
            event_types=[],
            inbound_resources=[],
            source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = client.get(
                f"/api/v1/pos/availability?sku={sku}",
                headers={"X-WMS-Token": plaintext},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert len(body["availability"]) == 1
            wh = body["availability"][0]
            assert wh["qty_available"] == 7
            assert wh["bins"][0]["qty"] == 7
        finally:
            delete_token(token_id)
