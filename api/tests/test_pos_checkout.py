"""POST /api/v1/pos/checkout contract (v1.10.0 POS Endpoint 3).

The heaviest of the four POS routes: atomic SO-create + inventory
decrement + audit log + response cache, idempotent on idempotency_key,
lock-aware. Coverage:

- Auth + DRAFT-v1 header on all status codes.
- Happy path: single-line card sale, multi-line multi-warehouse, cash sale.
- Idempotency: same key + same body -> X-Idempotent-Replay: true with
  the cached response bytes; same key + different body -> 409
  idempotency_key_reused_with_different_body with existing_so_id.
- Body validation: extra top-level / per-line / per-tender fields,
  bad UUID4 idempotency_key, missing required field, zero quantity,
  oversized body.
- Card-data allowlist: tender carrying card_pan -> 422.
- Warehouse scope: line.warehouse_id outside token scope -> 403
  warehouse_not_in_scope with line_index.
- Fulfillment failure: unknown SKU, unknown warehouse, bin in sister
  warehouse, insufficient on_hand-minus-allocated -> 422
  fulfillment_failed.
- Post-success row shape: order_source, order_type, status, created_by,
  external_txn_ref, idempotency_key, idempotency_body_hash, and
  cached_response_body all populated.
- Inventory math: post-success quantity_on_hand decremented; allocated
  unchanged.
- Audit log: one POS_CHECKOUT row written with cashier_id as user_id;
  details JSON contains the wire fields.
- so_number format: SO-POS-<integer>.
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
    plaintext = f"pos-co-{uuid.uuid4()}"
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


# --- helpers ---------------------------------------------------------


def _post(client, token, body):
    return client.post(
        "/api/v1/pos/checkout",
        headers={
            "X-WMS-Token": token,
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
    )


def _new_uuid():
    return str(uuid.uuid4())


def _card_body(idempotency_key=None, qty=1, sku="TST-001",
               warehouse_id="APT-LAB", bin_id="A-01-01",
               cashier_id="mike", terminal_id="reg-01",
               external_txn_ref=None):
    return {
        "idempotency_key":  idempotency_key or _new_uuid(),
        "external_txn_ref": external_txn_ref or f"WC-{uuid.uuid4().hex[:12]}",
        "cashier_id":       cashier_id,
        "terminal_id":      terminal_id,
        "completed_at":     "2026-05-09T14:23:11Z",
        "payment_summary": {
            "method":         "card",
            "subtotal_cents": 1999 * qty,
            "tax_cents":      162 * qty,
            "total_cents":    2161 * qty,
            "tenders": [
                {
                    "type":         "card",
                    "amount_cents": 2161 * qty,
                    "card_brand":   "Visa",
                    "card_last4":   "1111",
                    "auth_code":    "000289",
                    "external_ref": "0000005400911209",
                }
            ],
        },
        "lines": [
            {
                "sku":              sku,
                "warehouse_id":     warehouse_id,
                "bin_id":           bin_id,
                "quantity":         qty,
                "unit_price_cents": 1999,
                "tax_cents":        162 * qty,
                "line_total_cents": 2161 * qty,
            }
        ],
    }


def _cash_body(idempotency_key=None, qty=1):
    return {
        "idempotency_key":  idempotency_key or _new_uuid(),
        "external_txn_ref": f"CASH-{uuid.uuid4().hex[:8]}",
        "cashier_id":       "mike",
        "terminal_id":      "reg-01",
        "completed_at":     "2026-05-09T14:23:11Z",
        "payment_summary": {
            "method":         "cash",
            "subtotal_cents": 1999 * qty,
            "tax_cents":      162 * qty,
            "total_cents":    2161 * qty,
            "tenders": [
                {
                    "type":                  "cash",
                    "amount_cents":          2161 * qty,
                    "amount_tendered_cents": 5000,
                    "change_cents":          5000 - 2161 * qty,
                }
            ],
        },
        "lines": [
            {
                "sku":              "TST-001",
                "warehouse_id":     "APT-LAB",
                "bin_id":           "A-01-01",
                "quantity":         qty,
                "unit_price_cents": 1999,
                "tax_cents":        162 * qty,
                "line_total_cents": 2161 * qty,
            }
        ],
    }


def _read_so(so_number):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT so_id, so_number, status, warehouse_id, created_by, shipped_at,
               order_source, order_type, external_txn_ref,
               idempotency_key, idempotency_body_hash, cached_response_body
          FROM sales_orders
         WHERE so_number = %s
        """,
        (so_number,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _read_inventory(item_id, bin_id, warehouse_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory "
        " WHERE item_id = %s AND bin_id = %s AND warehouse_id = %s",
        (item_id, bin_id, warehouse_id),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _read_audit_for_so(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT action_type, user_id, warehouse_id, details "
        "  FROM audit_log "
        " WHERE entity_type = 'SO' AND entity_id = %s "
        "   AND action_type = 'POS_CHECKOUT'",
        (so_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row


# ----------------------------------------------------------------------
# Auth + DRAFT header
# ----------------------------------------------------------------------


class TestAuthAndHeader:
    def test_missing_token_returns_401(self, client, seed_data):
        resp = client.post(
            "/api/v1/pos/checkout",
            headers={"Content-Type": "application/json"},
            data=json.dumps(_card_body()),
        )
        assert resp.status_code == 401

    def test_happy_response_carries_draft_header(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], _card_body())
        assert resp.status_code == 200
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_409_response_carries_draft_header(self, client, pos_token):
        # Same key, two different bodies -> second is 409.
        key = _new_uuid()
        first = _post(client, pos_token["plaintext"], _card_body(idempotency_key=key))
        assert first.status_code == 200
        second_body = _card_body(idempotency_key=key, qty=2)
        resp = _post(client, pos_token["plaintext"], second_body)
        assert resp.status_code == 409
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_422_response_carries_draft_header(self, client, pos_token):
        body = _card_body()
        body["lines"][0].pop("quantity")  # missing required
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_single_line_card_sale_returns_so_number(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], _card_body())
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["so_number"].startswith("SO-POS-")
        assert body["so_id"] == body["so_number"]
        assert body["replayed"] is False

    def test_so_row_carries_pos_columns(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], _card_body())
        body = resp.get_json()
        row = _read_so(body["so_number"])
        # Columns: 0 so_id, 1 so_number, 2 status, 3 warehouse_id,
        # 4 created_by, 5 shipped_at, 6 order_source, 7 order_type,
        # 8 external_txn_ref, 9 idempotency_key, 10 idempotency_body_hash,
        # 11 cached_response_body.
        assert row[2] == "SHIPPED"
        assert row[4] == "pos"
        assert row[6] == "pos"
        assert row[7] == "sale"
        assert row[8] is not None  # external_txn_ref set
        assert row[9] is not None  # idempotency_key set
        assert row[10] is not None  # idempotency_body_hash set
        assert row[11] is not None  # cached_response_body set

    def test_inventory_decremented(self, client, pos_token):
        # TST-001 starts at 50 in bin 3, warehouse 1.
        before = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        resp = _post(client, pos_token["plaintext"], _card_body(qty=3))
        assert resp.status_code == 200
        after = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        assert int(after[0]) == int(before[0]) - 3
        assert int(after[1]) == int(before[1])  # allocated unchanged

    def test_cash_sale_succeeds(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], _cash_body())
        assert resp.status_code == 200

    def test_audit_log_written_with_cashier_id(self, client, pos_token):
        body = _card_body(cashier_id="alice")
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 200
        so_number = resp.get_json()["so_number"]
        so_row = _read_so(so_number)
        so_id = so_row[0]
        audit = _read_audit_for_so(so_id)
        assert audit is not None
        assert audit[0] == "POS_CHECKOUT"
        assert audit[1] == "alice"
        # details JSON shape (psycopg2 returns dict already).
        details = audit[3]
        assert details["external_txn_ref"] == body["external_txn_ref"]
        assert details["terminal_id"] == "reg-01"
        assert details["payment_method"] == "card"
        assert "lines" in details and len(details["lines"]) == 1


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


class TestIdempotency:
    def test_same_key_same_body_replays(self, client, pos_token):
        body = _card_body()
        first = _post(client, pos_token["plaintext"], body)
        assert first.status_code == 200
        first_so = first.get_json()["so_number"]

        second = _post(client, pos_token["plaintext"], body)
        assert second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert second.get_json()["so_number"] == first_so

    def test_same_key_different_body_returns_409(self, client, pos_token):
        key = _new_uuid()
        a = _post(client, pos_token["plaintext"], _card_body(idempotency_key=key, qty=1))
        assert a.status_code == 200
        b = _post(client, pos_token["plaintext"], _card_body(idempotency_key=key, qty=2))
        assert b.status_code == 409
        body = b.get_json()
        assert body["error_kind"] == "idempotency_key_reused_with_different_body"
        assert body["details"]["existing_so_id"].startswith("SO-POS-")

    def test_replay_does_not_consume_inventory_twice(self, client, pos_token):
        body = _card_body(qty=2)
        before = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        _post(client, pos_token["plaintext"], body)
        _post(client, pos_token["plaintext"], body)  # replay
        after = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        assert int(after[0]) == int(before[0]) - 2  # decremented exactly once


# ----------------------------------------------------------------------
# Body validation + card-data allowlist
# ----------------------------------------------------------------------


class TestBodyValidation:
    def test_extra_top_level_field_returns_422(self, client, pos_token):
        body = _card_body()
        body["rogue"] = "boom"
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422

    def test_extra_per_line_field_returns_422(self, client, pos_token):
        body = _card_body()
        body["lines"][0]["bonus"] = "extra"
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422

    def test_card_pan_field_rejected_at_pydantic(self, client, pos_token):
        """PCI-scope guard: a tender carrying card_pan or any field
        outside the {brand, last4, auth_code, external_ref} allowlist
        fails extra='forbid' at the schema boundary so Sentry never
        sees PAN-shaped data on the wire."""
        body = _card_body()
        body["payment_summary"]["tenders"][0]["card_pan"] = "4111111111111111"
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422

    def test_bad_uuid_idempotency_key_returns_422(self, client, pos_token):
        body = _card_body()
        body["idempotency_key"] = "not-a-uuid"
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422

    def test_zero_quantity_returns_422(self, client, pos_token):
        body = _card_body()
        body["lines"][0]["quantity"] = 0
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422

    def test_oversized_body_returns_413(self, client, pos_token):
        # Default cap is 256 KB. Send a 260 KB raw body.
        oversize = "X" * (260 * 1024)
        resp = client.post(
            "/api/v1/pos/checkout",
            headers={
                "X-WMS-Token": pos_token["plaintext"],
                "Content-Type": "application/json",
            },
            data=oversize,
        )
        assert resp.status_code == 413


# ----------------------------------------------------------------------
# Warehouse scope
# ----------------------------------------------------------------------


class TestWarehouseScope:
    def test_line_in_out_of_scope_warehouse_returns_403(self, client, seed_data):
        # APT-LAB is warehouse 1; token only has warehouse 99.
        plaintext = f"co-scope-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext, warehouse_ids=[99], event_types=[],
            inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = _post(client, plaintext, _card_body())
            assert resp.status_code == 403
            body = resp.get_json()
            assert body["error_kind"] == "warehouse_not_in_scope"
            assert body["details"]["line_index"] == 0
            assert body["details"]["warehouse_id"] == "APT-LAB"
        finally:
            delete_token(token_id)


# ----------------------------------------------------------------------
# Fulfillment failure
# ----------------------------------------------------------------------


class TestFulfillmentFailure:
    def test_unknown_sku_returns_422(self, client, pos_token):
        body = _card_body(sku="DOES-NOT-EXIST")
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "fulfillment_failed"

    def test_unknown_warehouse_returns_422(self, client, pos_token):
        body = _card_body(warehouse_id="NO-SUCH-WH")
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "fulfillment_failed"

    def test_bin_in_sister_warehouse_returns_422(self, client, seed_data):
        # Bin 'A-01-01' lives in warehouse 1; specifying warehouse
        # VIRTUAL (warehouse 2) means the bin doesn't resolve.
        body = _card_body(warehouse_id="VIRTUAL")
        # The pos_token fixture is scoped to warehouse 1 only -> 403
        # would fire before the fulfillment branch. Issue a token
        # with both warehouses so we land on the bulk-resolve check.
        plaintext = f"co-bins-{uuid.uuid4()}"
        token_id = insert_token(
            plaintext=plaintext, warehouse_ids=[1, 2], event_types=[],
            inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            resp = _post(client, plaintext, body)
            assert resp.status_code == 422
            assert resp.get_json()["error_kind"] == "fulfillment_failed"
        finally:
            delete_token(token_id)

    def test_insufficient_stock_returns_422(self, client, pos_token):
        # TST-001 has 50 units; ask for 51.
        body = _card_body(qty=51)
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422
        body_json = resp.get_json()
        assert body_json["error_kind"] == "fulfillment_failed"
        assert body_json["details"]["available_qty"] == 50
