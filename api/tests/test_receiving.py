import psycopg2
import os


def _query_one(sql, params=None):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


class TestPOLookup:
    def test_lookup_po_by_barcode(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_number"] == "PO-001"
        assert data["purchase_order"]["status"] == "OPEN"
        assert len(data["lines"]) == 3, "PO-001 should have 3 lines"

    def test_lookup_po_by_number(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_id"] == 1

    def test_lookup_po_not_found(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-FAKE", headers=auth_headers)
        assert resp.status_code == 404

    def test_lookup_po_closed(self, client, auth_headers):
        # Close the PO directly in the DB, then try to look it up
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("UPDATE purchase_orders SET status = 'CLOSED' WHERE po_id = 1")
        cur.close()
        conn.close()

        resp = client.get("/api/receiving/po/PO-001", headers=auth_headers)
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
        # Receive all 3 PO lines fully: item 1 qty 50, item 4 qty 20, item 6 qty 100
        payload = {
            "po_id": 1,
            "items": [
                {"item_id": 1, "quantity": 50, "bin_id": bid},
                {"item_id": 4, "quantity": 20, "bin_id": bid},
                {"item_id": 6, "quantity": 100, "bin_id": bid},
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
            "items": [{"item_id": 2, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
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

    def test_receive_over_receipt_warning(self, client, auth_headers, seed_data):
        # PO line 1 has 50 ordered. Receive 60 - should succeed with warning
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 60, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["warnings"]) > 0, "Over-receipt should produce a warning"

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
