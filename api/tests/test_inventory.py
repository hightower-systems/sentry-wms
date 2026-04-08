from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _query_one(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


class TestCreateCycleCount:
    def test_create_cycle_count(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["counts"]) == 1
        assert data["counts"][0]["bin_code"] == "A-01-01"
        assert data["counts"][0]["lines"] >= 1, "Should have lines for items in bin"

    def test_create_count_multiple_bins(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3, 4, 5]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["counts"]) == 3

    def test_create_count_empty_bin(self, client, auth_headers):
        # Bin 16 (QC-01) has no inventory in seed data
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [16]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["counts"][0]["lines"] == 0

    def test_create_count_invalid_warehouse(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 9999, "bin_ids": [3]},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestGetCycleCount:
    def test_get_count_details(self, client, auth_headers):
        # Create a count first
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        resp = client.get(f"/api/inventory/cycle-count/{count_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cycle_count"]["status"] == "PENDING"
        assert len(data["lines"]) >= 1
        # Bin 3 (A-01-01) has item 1 (qty 50) and item 11 (qty 12)
        for line in data["lines"]:
            assert line["expected_quantity"] > 0

    def test_get_count_not_found(self, client, auth_headers):
        resp = client.get("/api/inventory/cycle-count/9999", headers=auth_headers)
        assert resp.status_code == 404


class TestSubmitCycleCount:
    def _create_count_for_bin(self, client, auth_headers, bin_id=3):
        """Create a cycle count and return count_id and lines."""
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [bin_id]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        detail_resp = client.get(
            f"/api/inventory/cycle-count/{count_id}", headers=auth_headers
        )
        return count_id, detail_resp.get_json()["lines"]

    def test_submit_count_no_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        # Submit exact expected quantities
        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "COMPLETED"
        assert data["summary"]["lines_with_variance"] == 0

    def test_submit_count_with_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        # Submit different quantities
        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"] + 5}
            for l in lines
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "VARIANCE"
        assert data["summary"]["lines_with_variance"] > 0
        assert len(data["summary"]["adjustments"]) > 0

    def test_submit_count_negative_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        line = lines[0]
        original_qty = line["expected_quantity"]

        submit_lines = [
            {"count_line_id": line["count_line_id"], "counted_quantity": original_qty - 3}
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        data = resp.get_json()
        adj = data["summary"]["adjustments"][0]
        assert adj["variance"] == -3

        # Verify inventory was decremented
        new_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = 3",
            (line["item_id"],),
        )
        assert new_qty == original_qty - 3

    def test_submit_count_positive_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        line = lines[0]
        original_qty = line["expected_quantity"]

        submit_lines = [
            {"count_line_id": line["count_line_id"], "counted_quantity": original_qty + 10}
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        data = resp.get_json()
        adj = data["summary"]["adjustments"][0]
        assert adj["variance"] == 10

        new_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = 3",
            (line["item_id"],),
        )
        assert new_qty == original_qty + 10

    def test_submit_count_updates_last_counted_at(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        line = lines[0]

        submit_lines = [
            {"count_line_id": line["count_line_id"], "counted_quantity": line["expected_quantity"]}
        ]
        client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )

        last_counted = _query_val(
            "SELECT last_counted_at FROM inventory WHERE item_id = %s AND bin_id = 3",
            (line["item_id"],),
        )
        assert last_counted is not None, "last_counted_at should be set"

    def test_submit_count_creates_audit_log(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ]
        client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )

        row = _query_one("SELECT log_id FROM audit_log WHERE action_type = 'COUNT'")
        assert row is not None

    def test_submit_count_already_completed(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ]

        # Submit once
        client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )

        # Submit again
        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_inventory_requires_auth(self, client):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
        )
        assert resp.status_code == 401
