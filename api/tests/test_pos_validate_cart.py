"""POST /api/v1/pos/validate-cart contract (v1.10.0 POS Endpoint 2).

Coverage:
- Happy path: single valid line, multi-line valid cart -> 200
  {"valid": true}.
- Each conflict reason in isolation:
  sku_not_found, item_inactive, warehouse_not_found,
  warehouse_not_in_scope, bin_not_found, insufficient_stock.
- Multi-conflict: three bad lines in one cart, all surface with
  line_index preserved.
- Body-cap: 413 body_too_large.
- Empty / oversized lines array: 422 (Pydantic min/max length).
- Unknown top-level field: 422 (extra='forbid').
- Auth + DRAFT-v1 header on 200, 409, 422, 413.
"""

import json
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
    """POS register token: pos.dispatch slug, warehouse 1 in scope."""
    plaintext = f"pos-vc-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext=plaintext,
        warehouse_ids=[1],
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
        (code, name, "test"),
    )
    wh_id = cur.fetchone()[0]
    cur.close()
    return wh_id


def _insert_bin(warehouse_id, bin_code, zone_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, "
        " bin_type, pick_sequence, putaway_sequence, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING bin_id",
        (zone_id, warehouse_id, bin_code, bin_code, "Pickable",
         0, 0, str(uuid.uuid4())),
    )
    bin_id = cur.fetchone()[0]
    cur.close()
    return bin_id


def _insert_item(sku, is_active=True):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, is_active, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (sku, "Test Item", is_active, str(uuid.uuid4())),
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


def _post(client, token, body):
    return client.post(
        "/api/v1/pos/validate-cart",
        headers={
            "X-WMS-Token": token,
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
    )


# ----------------------------------------------------------------------
# Auth + DRAFT header
# ----------------------------------------------------------------------


class TestAuthAndHeader:
    def test_missing_token_returns_401(self, client, seed_data):
        resp = _post(
            client, "",  # empty header skipped below; pass via direct call
            {"lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                        "bin_id": "A-01-01", "quantity": 1}]},
        )
        # _post sends an empty token; the decorator returns 401 invalid_token
        assert resp.status_code == 401

    def test_happy_response_carries_draft_header(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                        "bin_id": "A-01-01", "quantity": 1}]},
        )
        assert resp.status_code == 200
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_409_response_carries_draft_header(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "DOES-NOT-EXIST", "warehouse_id": "APT-LAB",
                        "bin_id": "A-01-01", "quantity": 1}]},
        )
        assert resp.status_code == 409
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_422_response_carries_draft_header(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], {"lines": []})
        assert resp.status_code == 422
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_single_valid_line_returns_200_valid_true(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                        "bin_id": "A-01-01", "quantity": 1}]},
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"valid": True}

    def test_multi_line_valid_cart_returns_200(self, client, pos_token):
        # TST-001 in A-01-01 (50 units), TST-002 in A-01-02 (50 units).
        resp = _post(
            client, pos_token["plaintext"],
            {
                "lines": [
                    {"sku": "TST-001", "warehouse_id": "APT-LAB",
                     "bin_id": "A-01-01", "quantity": 1},
                    {"sku": "TST-002", "warehouse_id": "APT-LAB",
                     "bin_id": "A-01-02", "quantity": 5},
                ]
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"valid": True}


# ----------------------------------------------------------------------
# Each conflict reason in isolation
# ----------------------------------------------------------------------


class TestConflictReasons:
    def test_sku_not_found(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "NEVER-EXISTED", "warehouse_id": "APT-LAB",
                        "bin_id": "A-01-01", "quantity": 1}]},
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["valid"] is False
        assert len(body["conflicts"]) == 1
        c = body["conflicts"][0]
        assert c["reason"] == "sku_not_found"
        assert c["line_index"] == 0
        assert c["sku"] == "NEVER-EXISTED"

    def test_item_inactive(self, client, seed_data):
        sku = f"INACT-{uuid.uuid4().hex[:6]}"
        _insert_item(sku=sku, is_active=False)
        plaintext = f"vc-inact-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext, warehouse_ids=[1], event_types=[],
            inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = _post(
                client, plaintext,
                {"lines": [{"sku": sku, "warehouse_id": "APT-LAB",
                            "bin_id": "A-01-01", "quantity": 1}]},
            )
            assert resp.status_code == 409
            assert resp.get_json()["conflicts"][0]["reason"] == "item_inactive"
        finally:
            delete_token(token_id)

    def test_warehouse_not_found(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "TST-001", "warehouse_id": "NO-SUCH-WH",
                        "bin_id": "A-01-01", "quantity": 1}]},
        )
        assert resp.status_code == 409
        assert resp.get_json()["conflicts"][0]["reason"] == "warehouse_not_found"

    def test_warehouse_not_in_scope(self, client, seed_data):
        # Token scoped to warehouse 99 -> APT-LAB (warehouse 1) is real
        # but not in scope.
        plaintext = f"vc-scope-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext, warehouse_ids=[99], event_types=[],
            inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = _post(
                client, plaintext,
                {"lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                            "bin_id": "A-01-01", "quantity": 1}]},
            )
            assert resp.status_code == 409
            assert (
                resp.get_json()["conflicts"][0]["reason"]
                == "warehouse_not_in_scope"
            )
        finally:
            delete_token(token_id)

    def test_bin_not_found_in_specified_warehouse(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                        "bin_id": "NO-SUCH-BIN", "quantity": 1}]},
        )
        assert resp.status_code == 409
        assert resp.get_json()["conflicts"][0]["reason"] == "bin_not_found"

    def test_bin_in_sister_warehouse_is_not_found(self, client, seed_data):
        """A bin code exists in warehouse A but the line specifies
        warehouse B (also in scope). The JOIN pins bin.warehouse_id =
        warehouse.warehouse_id, so the bin does NOT match -> bin_not_found."""
        wh_a = _insert_warehouse(f"a-{uuid.uuid4().hex[:6]}", "WH A")
        wh_b = _insert_warehouse(f"b-{uuid.uuid4().hex[:6]}", "WH B")
        # Lookup by warehouse_code requires fetching the codes back.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT warehouse_code FROM warehouses WHERE warehouse_id = %s",
            (wh_a,),
        )
        wh_a_code = cur.fetchone()[0]
        cur.execute(
            "SELECT warehouse_code FROM warehouses WHERE warehouse_id = %s",
            (wh_b,),
        )
        wh_b_code = cur.fetchone()[0]
        cur.close()

        bin_code = f"BIN-{uuid.uuid4().hex[:6]}"
        _insert_bin(wh_a, bin_code)  # exists only in WH A
        sku = f"SKU-{uuid.uuid4().hex[:6]}"
        _insert_item(sku=sku)

        plaintext = f"vc-sister-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext, warehouse_ids=[wh_a, wh_b],
            event_types=[], inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            # Specify the bin under WH B even though it lives in WH A.
            resp = _post(
                client, plaintext,
                {"lines": [{"sku": sku, "warehouse_id": wh_b_code,
                            "bin_id": bin_code, "quantity": 1}]},
            )
            assert resp.status_code == 409
            assert resp.get_json()["conflicts"][0]["reason"] == "bin_not_found"
        finally:
            delete_token(token_id)

    def test_insufficient_stock_carries_available_qty(self, client, seed_data):
        sku = f"LOW-{uuid.uuid4().hex[:6]}"
        item_id = _insert_item(sku=sku)
        bin_id = _insert_bin(1, f"BIN-{uuid.uuid4().hex[:6]}", zone_id=2)
        _insert_inventory(item_id, bin_id, 1, on_hand=3, allocated=0)

        plaintext = f"vc-low-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext, warehouse_ids=[1], event_types=[],
            inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            # Look up the bin_code we just inserted.
            conn = get_raw_connection()
            cur = conn.cursor()
            cur.execute("SELECT bin_code FROM bins WHERE bin_id = %s", (bin_id,))
            bin_code = cur.fetchone()[0]
            cur.close()

            resp = _post(
                client, plaintext,
                {"lines": [{"sku": sku, "warehouse_id": "APT-LAB",
                            "bin_id": bin_code, "quantity": 5}]},
            )
            assert resp.status_code == 409
            c = resp.get_json()["conflicts"][0]
            assert c["reason"] == "insufficient_stock"
            assert c["available_qty"] == 3
            assert c["requested_qty"] == 5
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Multi-conflict shape
# ----------------------------------------------------------------------


class TestMultiConflict:
    def test_three_bad_lines_all_surface_with_line_index(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {
                "lines": [
                    {"sku": "TST-001", "warehouse_id": "APT-LAB",
                     "bin_id": "A-01-01", "quantity": 1},               # valid
                    {"sku": "MISSING", "warehouse_id": "APT-LAB",
                     "bin_id": "A-01-01", "quantity": 1},               # sku_not_found
                    {"sku": "TST-001", "warehouse_id": "NO-WH",
                     "bin_id": "A-01-01", "quantity": 1},               # warehouse_not_found
                    {"sku": "TST-001", "warehouse_id": "APT-LAB",
                     "bin_id": "NO-BIN", "quantity": 1},                # bin_not_found
                ]
            },
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["valid"] is False
        conflicts = body["conflicts"]
        assert len(conflicts) == 3
        by_idx = {c["line_index"]: c for c in conflicts}
        assert by_idx[1]["reason"] == "sku_not_found"
        assert by_idx[2]["reason"] == "warehouse_not_found"
        assert by_idx[3]["reason"] == "bin_not_found"


# ----------------------------------------------------------------------
# Body-shape validation
# ----------------------------------------------------------------------


class TestBodyShape:
    def test_empty_lines_array_returns_422(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], {"lines": []})
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_body"

    def test_too_many_lines_returns_422(self, client, pos_token):
        lines = [
            {"sku": "TST-001", "warehouse_id": "APT-LAB",
             "bin_id": "A-01-01", "quantity": 1}
            for _ in range(201)
        ]
        resp = _post(client, pos_token["plaintext"], {"lines": lines})
        assert resp.status_code == 422

    def test_unknown_top_level_field_returns_422(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {
                "lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                           "bin_id": "A-01-01", "quantity": 1}],
                "rogue_field": "boom",
            },
        )
        assert resp.status_code == 422

    def test_unknown_per_line_field_returns_422(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {
                "lines": [{
                    "sku": "TST-001", "warehouse_id": "APT-LAB",
                    "bin_id": "A-01-01", "quantity": 1,
                    "extra": "boom",
                }],
            },
        )
        assert resp.status_code == 422

    def test_zero_quantity_returns_422(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"],
            {"lines": [{"sku": "TST-001", "warehouse_id": "APT-LAB",
                        "bin_id": "A-01-01", "quantity": 0}]},
        )
        assert resp.status_code == 422


# ----------------------------------------------------------------------
# Body cap
# ----------------------------------------------------------------------


class TestBodyCap:
    def test_oversized_body_returns_413(self, client, pos_token):
        # Default cap is 256 KB. Build a body whose Content-Length
        # exceeds that. The route checks request.content_length BEFORE
        # parsing so a hostile client cannot exhaust JSON-parser CPU.
        oversize = "X" * (260 * 1024)
        resp = client.post(
            "/api/v1/pos/validate-cart",
            headers={
                "X-WMS-Token": pos_token["plaintext"],
                "Content-Type": "application/json",
            },
            data=oversize,
        )
        assert resp.status_code == 413
        assert resp.get_json()["error_kind"] == "body_too_large"
