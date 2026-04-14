from db_test_context import get_raw_connection


def _query_one(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


class TestPOLookup:
    def test_lookup_po_by_barcode(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_number"] == "PO-2026-001"
        assert data["purchase_order"]["status"] == "OPEN"
        assert len(data["lines"]) == 10, "PO-2026-001 should have 10 lines"

    def test_lookup_po_by_number(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_id"] == 1

    def test_lookup_po_not_found(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-FAKE", headers=auth_headers)
        assert resp.status_code == 404

    def test_lookup_po_closed(self, client, auth_headers):
        # Close the PO directly in the DB, then try to look it up
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("UPDATE purchase_orders SET status = 'CLOSED' WHERE po_id = 1")
        cur.close()

        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 400
        assert "closed" in resp.get_json()["error"].lower()


class TestReceiveItems:
    def test_receive_items_success(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 10, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["receipt_ids"]) == 1
        assert data["po_status"] in ("PARTIAL", "RECEIVED")

    def test_receive_updates_inventory(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        client.post("/api/receiving/receive", json=payload, headers=auth_headers)

        row = _query_one(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = %s",
            (seed_data["staging_bin_id"],),
        )
        assert row is not None, "Inventory row should exist in staging bin"
        assert row[0] == 5

    def test_receive_partial_updates_po_status(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 10, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.get_json()["po_status"] == "PARTIAL"

    def test_receive_all_items_completes_po(self, client, auth_headers, seed_data):
        bid = seed_data["staging_bin_id"]
        # Receive all 10 PO-2026-001 lines fully
        payload = {
            "po_id": 1,
            "items": [
                {"item_id": 1, "quantity": 100, "bin_id": bid},
                {"item_id": 2, "quantity": 100, "bin_id": bid},
                {"item_id": 3, "quantity": 100, "bin_id": bid},
                {"item_id": 4, "quantity": 100, "bin_id": bid},
                {"item_id": 5, "quantity": 50, "bin_id": bid},
                {"item_id": 6, "quantity": 20, "bin_id": bid},
                {"item_id": 7, "quantity": 200, "bin_id": bid},
                {"item_id": 8, "quantity": 30, "bin_id": bid},
                {"item_id": 9, "quantity": 40, "bin_id": bid},
                {"item_id": 10, "quantity": 60, "bin_id": bid},
            ],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.get_json()["po_status"] == "RECEIVED"

    def test_receive_creates_audit_log(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        client.post("/api/receiving/receive", json=payload, headers=auth_headers)

        row = _query_one(
            "SELECT log_id FROM audit_log WHERE action_type = 'RECEIVE' AND entity_id = 1"
        )
        assert row is not None, "Audit log entry should exist for receive action"

    def test_receive_invalid_po(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 9999,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 404

    def test_receive_invalid_item(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 11, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "not on PO" in resp.get_json()["error"]

    def test_receive_zero_quantity(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 0, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400

    def test_receive_over_receipt_blocked_by_default(self, client, auth_headers, seed_data):
        # PO line 1 has 100 ordered. Receiving 110 is blocked unless allow_over_receipt=true.
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 110, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "Over-receipt" in resp.get_json()["error"]

    def test_receive_missing_body(self, client, auth_headers):
        resp = client.post("/api/receiving/receive", json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_receive_requires_auth(self, client, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload)
        assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Bug #2: Cancel receiving should discard ALL progress
# ══════════════════════════════════════════════════════════════════════════════


class TestCancelReceiving:
    """Bug #2: Cancel receiving session should reverse all receipts."""

    def test_cancel_reverses_receipts(self, client, auth_headers, seed_data):
        """Cancelling should undo inventory additions and PO line updates."""
        po_id = seed_data["po_id"]
        bin_id = seed_data["staging_bin_id"]

        # Get initial state of PO line
        resp = client.get(f"/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        initial_lines = resp.get_json()["lines"]
        initial_received = initial_lines[0]["quantity_received"]
        item_id = initial_lines[0]["item_id"]

        # Receive some items
        resp = client.post("/api/receiving/receive", json={
            "po_id": po_id,
            "items": [{"item_id": item_id, "quantity": 5, "bin_id": bin_id}],
            "warehouse_id": seed_data["warehouse_id"],
        }, headers=auth_headers)
        assert resp.status_code == 200
        receipt_ids = resp.get_json()["receipt_ids"]
        assert len(receipt_ids) == 1

        # Verify qty increased
        resp = client.get(f"/api/receiving/po/PO-2026-001", headers=auth_headers)
        after_receive = resp.get_json()["lines"]
        item_line = [l for l in after_receive if l["item_id"] == item_id][0]
        assert item_line["quantity_received"] == initial_received + 5

        # Cancel
        resp = client.post("/api/receiving/cancel", json={
            "receipt_ids": receipt_ids,
            "po_id": po_id,
            "warehouse_id": seed_data["warehouse_id"],
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["reversed"] == 1

        # Verify qty reverted
        resp = client.get(f"/api/receiving/po/PO-2026-001", headers=auth_headers)
        after_cancel = resp.get_json()["lines"]
        item_line = [l for l in after_cancel if l["item_id"] == item_id][0]
        assert item_line["quantity_received"] == initial_received

    def test_cancel_empty_list(self, client, auth_headers):
        """Cancelling with no receipt_ids should return 200."""
        resp = client.post("/api/receiving/cancel", json={
            "receipt_ids": [],
        }, headers=auth_headers)
        assert resp.status_code == 200
