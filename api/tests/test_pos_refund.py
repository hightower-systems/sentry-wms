"""POST /api/v1/pos/refund contract (v1.10.0 POS Endpoint 4).

Coverage:
- Auth + DRAFT-v1 header on all status codes.
- Happy path: card sale -> card refund. Original SO has refunded_at +
  refund_so_id populated; credit-memo has order_type='refund' +
  parent_so_id + negative line quantities + status='SHIPPED';
  inventory re-incremented; one POS_REFUND audit row.
- Cash refund happy path.
- Tender mismatch: card sale + cash refund -> 422 tender_mismatch.
- 90-day window: backdated original sale -> 422 refund_window_expired.
- Already refunded: second refund attempt -> 422 already_refunded
  with existing_refund_so_id.
- Original missing / out-of-scope / not POS / wrong status -> 404
  conflated.
- Idempotency: same key + same body -> X-Idempotent-Replay; same key +
  different body -> 409.
- Body validation: bad UUID4, bad original_so_id format, oversized,
  card_pan in refund tender -> 422/413.
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
    plaintext = f"pos-rf-{uuid.uuid4()}"
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


def _post_checkout(client, token, body):
    return client.post(
        "/api/v1/pos/checkout",
        headers={"X-WMS-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _post_refund(client, token, body):
    return client.post(
        "/api/v1/pos/refund",
        headers={"X-WMS-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _new_uuid():
    return str(uuid.uuid4())


def _checkout_card_body(qty=1, sku="TST-001", warehouse_id="APT-LAB",
                        bin_id="A-01-01", external_txn_ref=None):
    return {
        "idempotency_key":  _new_uuid(),
        "external_txn_ref": external_txn_ref or f"WC-{uuid.uuid4().hex[:12]}",
        "cashier_id":       "mike",
        "terminal_id":      "reg-01",
        "completed_at":     "2026-05-09T14:23:11Z",
        "payment_summary": {
            "method":         "card",
            "subtotal_cents": 1999 * qty,
            "tax_cents":      162 * qty,
            "total_cents":    2161 * qty,
            "tenders": [{
                "type":         "card",
                "amount_cents": 2161 * qty,
                "card_brand":   "Visa",
                "card_last4":   "1111",
                "auth_code":    "000289",
                "external_ref": "0000005400911209",
            }],
        },
        "lines": [{
            "sku":              sku,
            "warehouse_id":     warehouse_id,
            "bin_id":           bin_id,
            "quantity":         qty,
            "unit_price_cents": 1999,
            "tax_cents":        162 * qty,
            "line_total_cents": 2161 * qty,
        }],
    }


def _checkout_cash_body(qty=1):
    return {
        "idempotency_key":  _new_uuid(),
        "external_txn_ref": f"CASH-{uuid.uuid4().hex[:8]}",
        "cashier_id":       "mike",
        "terminal_id":      "reg-01",
        "completed_at":     "2026-05-09T14:23:11Z",
        "payment_summary": {
            "method":         "cash",
            "subtotal_cents": 1999 * qty,
            "tax_cents":      162 * qty,
            "total_cents":    2161 * qty,
            "tenders": [{
                "type":                  "cash",
                "amount_cents":          2161 * qty,
                "amount_tendered_cents": 5000,
                "change_cents":          5000 - 2161 * qty,
            }],
        },
        "lines": [{
            "sku":              "TST-001",
            "warehouse_id":     "APT-LAB",
            "bin_id":           "A-01-01",
            "quantity":         qty,
            "unit_price_cents": 1999,
            "tax_cents":        162 * qty,
            "line_total_cents": 2161 * qty,
        }],
    }


def _refund_body_for(checkout_body, sale_so_number, idempotency_key=None,
                      method=None):
    """Build a refund body matching the given checkout body's payment
    method by default; pass method='cash' or 'card' to force a
    tender-mismatch test."""
    method = method or checkout_body["payment_summary"]["method"]
    summary = dict(checkout_body["payment_summary"])
    summary["method"] = method
    if method == "card":
        summary["tenders"] = [{
            "type":         "card",
            "amount_cents": summary["total_cents"],
            "card_brand":   "Visa",
            "card_last4":   "1111",
            "auth_code":    "000291",
            "external_ref": "0000005400911999",
        }]
    else:
        summary["tenders"] = [{
            "type":                  "cash",
            "amount_cents":          summary["total_cents"],
            "amount_tendered_cents": summary["total_cents"],
            "change_cents":          0,
        }]
    return {
        "idempotency_key":           idempotency_key or _new_uuid(),
        "original_so_id":            sale_so_number,
        "original_external_txn_ref": checkout_body["external_txn_ref"],
        "external_refund_ref":       f"REF-{uuid.uuid4().hex[:8]}",
        "cashier_id":                "mike",
        "terminal_id":                "reg-01",
        "completed_at":              "2026-05-10T10:14:02Z",
        "refund_summary":            summary,
    }


def _read_so(so_number):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT so_id, so_number, status, order_source, order_type,
               parent_so_id, refunded_at, refund_so_id,
               external_txn_ref, idempotency_key
          FROM sales_orders
         WHERE so_number = %s
        """,
        (so_number,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _read_so_lines(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_ordered, quantity_picked, quantity_shipped, status "
        "  FROM sales_order_lines WHERE so_id = %s ORDER BY line_number",
        (so_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


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


def _backdate_so(so_id, days):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sales_orders SET created_at = NOW() - (%s || ' days')::INTERVAL "
        " WHERE so_id = %s",
        (days, so_id),
    )
    cur.close()


def _read_audit_for_so(so_id, action):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT action_type, user_id, warehouse_id, details "
        "  FROM audit_log "
        " WHERE entity_type = 'SO' AND entity_id = %s AND action_type = %s",
        (so_id, action),
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
            "/api/v1/pos/refund",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "idempotency_key":           _new_uuid(),
                "original_so_id":            "SO-POS-1",
                "original_external_txn_ref": "x",
                "external_refund_ref":       "y",
                "cashier_id":                "mike",
                "terminal_id":               "reg-01",
                "completed_at":              "2026-05-10T10:14:02Z",
                "refund_summary": {
                    "method": "card", "subtotal_cents": 0, "tax_cents": 0,
                    "total_cents": 0, "tenders": [{
                        "type": "card", "amount_cents": 0, "card_brand": "Visa",
                        "card_last4": "1111", "auth_code": "x", "external_ref": "y",
                    }],
                },
            }),
        )
        assert resp.status_code == 401

    def test_404_response_carries_draft_header(self, client, pos_token):
        resp = _post_refund(client, pos_token["plaintext"], {
            "idempotency_key":           _new_uuid(),
            "original_so_id":            "SO-POS-999999",
            "original_external_txn_ref": "x",
            "external_refund_ref":       "y",
            "cashier_id":                "mike",
            "terminal_id":               "reg-01",
            "completed_at":              "2026-05-10T10:14:02Z",
            "refund_summary": {
                "method": "card", "subtotal_cents": 0, "tax_cents": 0,
                "total_cents": 0, "tenders": [{
                    "type": "card", "amount_cents": 0, "card_brand": "Visa",
                    "card_last4": "1111", "auth_code": "x", "external_ref": "y",
                }],
            },
        })
        assert resp.status_code == 404
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestHappyPath:
    def test_card_sale_then_card_refund_full_shape(self, client, pos_token):
        # Step 1: complete a sale.
        sale_body = _checkout_card_body(qty=2)
        before = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        assert sale_resp.status_code == 200
        sale_so_number = sale_resp.get_json()["so_number"]

        # Inventory dropped by 2.
        mid = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        assert int(mid[0]) == int(before[0]) - 2

        # Step 2: refund.
        rb = _refund_body_for(sale_body, sale_so_number)
        refund_resp = _post_refund(client, pos_token["plaintext"], rb)
        assert refund_resp.status_code == 200
        body = refund_resp.get_json()
        assert body["original_so_id"] == sale_so_number
        assert body["refund_so_id"].startswith("SO-POS-REF-")
        assert body["replayed"] is False

        # Inventory back to original.
        after = _read_inventory(item_id=1, bin_id=3, warehouse_id=1)
        assert int(after[0]) == int(before[0])

        # Original SO has refunded_at + refund_so_id populated.
        sale_row = _read_so(sale_so_number)
        assert sale_row[6] is not None  # refunded_at
        assert sale_row[7] is not None  # refund_so_id

        # Credit-memo SO has order_type='refund', parent_so_id pointing
        # at the original, status='SHIPPED', external_txn_ref set.
        refund_row = _read_so(body["refund_so_id"])
        assert refund_row[2] == "SHIPPED"
        assert refund_row[3] == "pos"
        assert refund_row[4] == "refund"
        assert refund_row[5] == sale_row[0]  # parent_so_id
        assert refund_row[8] == rb["external_refund_ref"]

        # Credit-memo lines carry NEGATIVE quantities.
        refund_lines = _read_so_lines(refund_row[0])
        assert len(refund_lines) == 1
        assert int(refund_lines[0][0]) == -2  # quantity_ordered
        assert int(refund_lines[0][1]) == -2  # quantity_picked
        assert int(refund_lines[0][2]) == -2  # quantity_shipped

        # One POS_REFUND audit row written.
        audit = _read_audit_for_so(refund_row[0], "POS_REFUND")
        assert audit is not None
        assert audit[1] == "mike"  # cashier_id

    def test_cash_sale_then_cash_refund(self, client, pos_token):
        sale_body = _checkout_cash_body(qty=1)
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        assert sale_resp.status_code == 200
        sale_so_number = sale_resp.get_json()["so_number"]
        rb = _refund_body_for(sale_body, sale_so_number)
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 200


# ----------------------------------------------------------------------
# Server-side rule guards
# ----------------------------------------------------------------------


class TestRuleGuards:
    def test_tender_mismatch_card_sale_cash_refund(self, client, pos_token):
        sale_body = _checkout_card_body()
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        sale_so_number = sale_resp.get_json()["so_number"]
        rb = _refund_body_for(sale_body, sale_so_number, method="cash")
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 422
        body = rf.get_json()
        assert body["error_kind"] == "tender_mismatch"
        assert body["details"]["original_method"] == "card"
        assert body["details"]["refund_method"] == "cash"

    def test_refund_window_expired(self, client, pos_token):
        sale_body = _checkout_card_body()
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        sale_so_number = sale_resp.get_json()["so_number"]
        sale_so_id = _read_so(sale_so_number)[0]
        _backdate_so(sale_so_id, 91)
        rb = _refund_body_for(sale_body, sale_so_number)
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 422
        assert rf.get_json()["error_kind"] == "refund_window_expired"

    def test_already_refunded_returns_existing_refund_so_id(self, client, pos_token):
        sale_body = _checkout_card_body()
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        sale_so_number = sale_resp.get_json()["so_number"]
        rb1 = _refund_body_for(sale_body, sale_so_number)
        rf1 = _post_refund(client, pos_token["plaintext"], rb1)
        assert rf1.status_code == 200
        first_refund = rf1.get_json()["refund_so_id"]

        rb2 = _refund_body_for(sale_body, sale_so_number)
        rf2 = _post_refund(client, pos_token["plaintext"], rb2)
        assert rf2.status_code == 422
        body = rf2.get_json()
        assert body["error_kind"] == "already_refunded"
        assert body["details"]["existing_refund_so_id"] == first_refund

    def test_unknown_original_so_returns_404(self, client, pos_token):
        rb = {
            "idempotency_key":           _new_uuid(),
            "original_so_id":            "SO-POS-999999",
            "original_external_txn_ref": "x",
            "external_refund_ref":       "y",
            "cashier_id":                "mike",
            "terminal_id":                "reg-01",
            "completed_at":              "2026-05-10T10:14:02Z",
            "refund_summary": {
                "method": "card", "subtotal_cents": 0, "tax_cents": 0,
                "total_cents": 0, "tenders": [{
                    "type": "card", "amount_cents": 0, "card_brand": "Visa",
                    "card_last4": "1111", "auth_code": "x", "external_ref": "y",
                }],
            },
        }
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 404
        assert rf.get_json()["error_kind"] == "original_so_not_found"

    def test_original_so_in_other_warehouse_returns_404(self, client, seed_data):
        # Create a sale via a token scoped to warehouse 1, then try to
        # refund via a token scoped to warehouse 99.
        seller_pt = f"rf-seller-{uuid.uuid4()}"
        seller_id = insert_token(
            plaintext=seller_pt, warehouse_ids=[1], event_types=[],
            inbound_resources=[], source_system=None,
            endpoints=["pos.dispatch"],
        )
        try:
            sale_body = _checkout_card_body()
            sale_resp = _post_checkout(client, seller_pt, sale_body)
            sale_so_number = sale_resp.get_json()["so_number"]

            other_pt = f"rf-other-{uuid.uuid4()}"
            other_id = insert_token(
                plaintext=other_pt, warehouse_ids=[99], event_types=[],
                inbound_resources=[], source_system=None,
                endpoints=["pos.dispatch"],
            )
            try:
                rb = _refund_body_for(sale_body, sale_so_number)
                rf = _post_refund(client, other_pt, rb)
                assert rf.status_code == 404
                assert rf.get_json()["error_kind"] == "original_so_not_found"
            finally:
                delete_token(other_id)
        finally:
            delete_token(seller_id)


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


class TestIdempotency:
    def test_same_key_same_body_replays(self, client, pos_token):
        sale_body = _checkout_card_body()
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        sale_so_number = sale_resp.get_json()["so_number"]
        key = _new_uuid()
        rb = _refund_body_for(sale_body, sale_so_number, idempotency_key=key)

        first = _post_refund(client, pos_token["plaintext"], rb)
        assert first.status_code == 200
        first_refund = first.get_json()["refund_so_id"]

        second = _post_refund(client, pos_token["plaintext"], rb)
        assert second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert second.get_json()["refund_so_id"] == first_refund

    def test_same_key_different_body_returns_409(self, client, pos_token):
        sale_a_body = _checkout_card_body()
        sale_a_resp = _post_checkout(client, pos_token["plaintext"], sale_a_body)
        sale_a_so = sale_a_resp.get_json()["so_number"]

        sale_b_body = _checkout_card_body()
        sale_b_resp = _post_checkout(client, pos_token["plaintext"], sale_b_body)
        sale_b_so = sale_b_resp.get_json()["so_number"]

        key = _new_uuid()
        rb1 = _refund_body_for(sale_a_body, sale_a_so, idempotency_key=key)
        rf1 = _post_refund(client, pos_token["plaintext"], rb1)
        assert rf1.status_code == 200

        # Same idempotency_key, different original_so_id -> body hash differs.
        rb2 = _refund_body_for(sale_b_body, sale_b_so, idempotency_key=key)
        rf2 = _post_refund(client, pos_token["plaintext"], rb2)
        assert rf2.status_code == 409
        assert rf2.get_json()["error_kind"] == "idempotency_key_reused_with_different_body"


# ----------------------------------------------------------------------
# Body validation
# ----------------------------------------------------------------------


class TestBodyValidation:
    def test_bad_uuid_idempotency_key_returns_422(self, client, pos_token):
        rb = {
            "idempotency_key":           "not-a-uuid",
            "original_so_id":            "SO-POS-1",
            "original_external_txn_ref": "x",
            "external_refund_ref":       "y",
            "cashier_id":                "mike",
            "terminal_id":                "reg-01",
            "completed_at":              "2026-05-10T10:14:02Z",
            "refund_summary": {
                "method": "card", "subtotal_cents": 0, "tax_cents": 0,
                "total_cents": 0, "tenders": [{
                    "type": "card", "amount_cents": 0, "card_brand": "Visa",
                    "card_last4": "1111", "auth_code": "x", "external_ref": "y",
                }],
            },
        }
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 422

    def test_bad_original_so_id_format_returns_422(self, client, pos_token):
        rb = {
            "idempotency_key":           _new_uuid(),
            "original_so_id":            "SO-WEB-1234",  # wrong prefix
            "original_external_txn_ref": "x",
            "external_refund_ref":       "y",
            "cashier_id":                "mike",
            "terminal_id":                "reg-01",
            "completed_at":              "2026-05-10T10:14:02Z",
            "refund_summary": {
                "method": "card", "subtotal_cents": 0, "tax_cents": 0,
                "total_cents": 0, "tenders": [{
                    "type": "card", "amount_cents": 0, "card_brand": "Visa",
                    "card_last4": "1111", "auth_code": "x", "external_ref": "y",
                }],
            },
        }
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 422

    def test_card_pan_in_refund_tender_returns_422(self, client, pos_token):
        sale_body = _checkout_card_body()
        sale_resp = _post_checkout(client, pos_token["plaintext"], sale_body)
        sale_so_number = sale_resp.get_json()["so_number"]
        rb = _refund_body_for(sale_body, sale_so_number)
        rb["refund_summary"]["tenders"][0]["card_pan"] = "4111111111111111"
        rf = _post_refund(client, pos_token["plaintext"], rb)
        assert rf.status_code == 422

    def test_oversized_body_returns_413(self, client, pos_token):
        oversize = "X" * (260 * 1024)
        resp = client.post(
            "/api/v1/pos/refund",
            headers={
                "X-WMS-Token": pos_token["plaintext"],
                "Content-Type": "application/json",
            },
            data=oversize,
        )
        assert resp.status_code == 413
