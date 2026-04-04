import psycopg2
import os


def _query_val(sql, params=None):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def _query_one(sql, params=None):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


class TestTransferMove:
    def test_transfer_success(self, client, auth_headers):
        # Item 1 in bin 2 has 25 units. Move 5 to bin 3.
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["transfer_id"] is not None
        assert data["quantity_moved"] == 5
        assert data["from_bin"]["remaining_quantity"] == 20
        assert data["to_bin"]["new_quantity"] == 5  # New inventory row for item 1 in bin 3

    def test_transfer_creates_record(self, client, auth_headers):
        client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 5},
            headers=auth_headers,
        )

        row = _query_one(
            "SELECT transfer_type FROM bin_transfers WHERE item_id = 1 AND transfer_type = 'MOVE'"
        )
        assert row is not None
        assert row[0] == "MOVE"

    def test_transfer_creates_audit_log(self, client, auth_headers):
        client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 5},
            headers=auth_headers,
        )

        row = _query_one("SELECT log_id FROM audit_log WHERE action_type = 'TRANSFER'")
        assert row is not None

    def test_transfer_insufficient_quantity(self, client, auth_headers):
        # Item 1 in bin 2 has 25. Try to move 100.
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 100},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Insufficient" in resp.get_json()["error"]

    def test_transfer_same_bin(self, client, auth_headers):
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 2, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "different" in resp.get_json()["error"]

    def test_transfer_invalid_item(self, client, auth_headers):
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 9999, "from_bin_id": 2, "to_bin_id": 3, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_transfer_invalid_bin(self, client, auth_headers):
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 9999, "to_bin_id": 3, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_transfer_creates_new_inventory_row(self, client, auth_headers):
        # Item 1 is not in bin 5. Transfer from bin 2 to bin 5 should create new row.
        before = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = 5"
        )
        assert before is None, "Item 1 should not be in bin 5 initially"

        client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 5, "quantity": 3},
            headers=auth_headers,
        )

        after = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = 5"
        )
        assert after == 3

    def test_transfer_deletes_empty_inventory(self, client, auth_headers):
        # Move ALL of item 1 from bin 2 (25 units)
        client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 25},
            headers=auth_headers,
        )

        remaining = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = 2"
        )
        assert remaining is None, "Inventory row should be deleted when qty reaches 0"

    def test_transfers_requires_auth(self, client):
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 5},
        )
        assert resp.status_code == 401
