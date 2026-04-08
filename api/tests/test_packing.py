from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _advance_so_to_picking(client, auth_headers, so_identifiers=None):
    """Run through picking flow to get SOs to PICKING status."""
    idents = so_identifiers or ["SO-2026-001"]
    create_resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": idents, "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = create_resp.get_json()["batch_id"]

    # Pick all tasks
    while True:
        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        data = next_resp.get_json()
        if "message" in data:
            break
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": data["pick_task_id"],
                "scanned_barcode": data["upc"],
                "quantity_picked": data["quantity_to_pick"],
            },
            headers=auth_headers,
        )

    # Complete batch
    client.post(
        "/api/picking/complete-batch",
        json={"batch_id": batch_id},
        headers=auth_headers,
    )
    return batch_id


class TestLoadOrder:
    def test_load_order_for_packing(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])

        resp = client.get("/api/packing/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["so_number"] == "SO-2026-001"
        assert len(data["lines"]) >= 1
        assert "calculated_weight_lbs" in data

    def test_load_order_not_found(self, client, auth_headers):
        resp = client.get("/api/packing/order/SO-FAKE", headers=auth_headers)
        assert resp.status_code == 404

    def test_load_order_wrong_status(self, client, auth_headers):
        # SO-2026-001 is still OPEN (not picked yet)
        resp = client.get("/api/packing/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 400
        assert "not ready" in resp.get_json()["error"].lower()


class TestVerifyItem:
    def test_verify_item_success(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])

        # SO-2026-001 line 1: item 1 (TST-001), UPC 100000000001
        resp = client.post(
            "/api/packing/verify",
            json={"so_id": 1, "scanned_barcode": "100000000001", "quantity": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["item"]["sku"] == "TST-001"
        assert "order_progress" in data

    def test_verify_wrong_barcode(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])

        resp = client.post(
            "/api/packing/verify",
            json={"so_id": 1, "scanned_barcode": "WRONG-UPC", "quantity": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_verify_over_pack(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])

        # SO-2026-001 line 1 has quantity_picked = 2. Try to pack 100
        resp = client.post(
            "/api/packing/verify",
            json={"so_id": 1, "scanned_barcode": "100000000001", "quantity": 100},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "remaining" in resp.get_json()["error"].lower()

    def test_verify_item_not_on_order(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])

        # Item 3 (TST-003, UPC 100000000003) is not on SO-2026-001
        resp = client.post(
            "/api/packing/verify",
            json={"so_id": 1, "scanned_barcode": "100000000003", "quantity": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestCompletePacking:
    def _verify_all_items(self, client, auth_headers, so_id, so_number="SO-2026-001"):
        """Verify all items on an order."""
        order_resp = client.get(f"/api/packing/order/{so_number}", headers=auth_headers)
        for line in order_resp.get_json()["lines"]:
            remaining = line["quantity_picked"] - line["quantity_packed"]
            if remaining > 0:
                client.post(
                    "/api/packing/verify",
                    json={"so_id": so_id, "scanned_barcode": line["upc"], "quantity": remaining},
                    headers=auth_headers,
                )

    def test_complete_packing_success(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])
        self._verify_all_items(client, auth_headers, 1)

        resp = client.post(
            "/api/packing/complete",
            json={"so_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "PACKED"

    def test_complete_packing_items_not_verified(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])
        # Don't verify any items

        resp = client.post(
            "/api/packing/complete",
            json={"so_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "not yet verified" in resp.get_json()["error"].lower()

    def test_calculated_weight_correct(self, client, auth_headers):
        _advance_so_to_picking(client, auth_headers, ["SO-2026-001"])

        resp = client.get("/api/packing/order/SO-2026-001", headers=auth_headers)
        data = resp.get_json()
        # Calculate expected weight from picked items
        expected_weight = sum(
            l["weight_lbs"] * l["quantity_picked"] for l in data["lines"]
        )
        assert abs(data["calculated_weight_lbs"] - expected_weight) < 0.01

    def test_packing_requires_auth(self, client):
        resp = client.get("/api/packing/order/SO-2026-001")
        assert resp.status_code == 401
