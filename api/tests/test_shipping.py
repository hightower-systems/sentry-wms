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


def _set_setting(key, value):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = %s",
        (key, value, value),
    )
    cur.close()


def _advance_so_to_picked(client, auth_headers, so_number="SO-2026-001"):
    """Advance an SO through picking to PICKED status (no packing)."""
    create_resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": [so_number], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = create_resp.get_json()["batch_id"]

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

    client.post("/api/picking/complete-batch", json={"batch_id": batch_id}, headers=auth_headers)
    return _query_val("SELECT so_id FROM sales_orders WHERE so_number = %s", (so_number,))


def _advance_so_to_packed(client, auth_headers, so_number="SO-2026-001"):
    """Advance an SO through picking and packing to PACKED status."""
    so_id = _advance_so_to_picked(client, auth_headers, so_number)

    # Pack - verify all items
    order_resp = client.get(f"/api/packing/order/{so_number}", headers=auth_headers)
    for line in order_resp.get_json()["lines"]:
        remaining = line["quantity_picked"] - line["quantity_packed"]
        if remaining > 0:
            client.post(
                "/api/packing/verify",
                json={"so_id": so_id, "scanned_barcode": line["upc"], "quantity": remaining},
                headers=auth_headers,
            )

    client.post("/api/packing/complete", json={"so_id": so_id}, headers=auth_headers)
    return so_id


# ── Shipping Order Lookup ────────────────────────────────────────────────────


class TestShippingOrderLookup:
    def test_lookup_packed_order(self, client, auth_headers):
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        resp = client.get("/api/shipping/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["so_number"] == "SO-2026-001"
        assert data["sales_order"]["status"] == "PACKED"
        assert data["total_items"] > 0
        assert data["total_lines"] > 0
        assert len(data["lines"]) > 0

    def test_lookup_not_found(self, client, auth_headers):
        resp = client.get("/api/shipping/order/SO-FAKE", headers=auth_headers)
        assert resp.status_code == 404

    def test_lookup_wrong_status_packing_required(self, client, auth_headers):
        _set_setting("require_packing_before_shipping", "true")
        # SO-2026-001 is OPEN, not PACKED
        resp = client.get("/api/shipping/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 400

    def test_lookup_picked_when_packing_not_required(self, client, auth_headers):
        _set_setting("require_packing_before_shipping", "false")
        _advance_so_to_picked(client, auth_headers, "SO-2026-001")

        resp = client.get("/api/shipping/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["status"] == "PICKED"

    def test_lookup_picked_rejected_when_packing_required(self, client, auth_headers):
        _set_setting("require_packing_before_shipping", "true")
        _advance_so_to_picked(client, auth_headers, "SO-2026-001")

        resp = client.get("/api/shipping/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 400
        assert "packed" in resp.get_json()["error"].lower()

    def test_lookup_requires_auth(self, client):
        resp = client.get("/api/shipping/order/SO-2026-001")
        assert resp.status_code == 401


# ── Fulfill ──────────────────────────────────────────────────────────────────


class TestFulfill:
    def test_fulfill_success(self, client, auth_headers):
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        resp = client.post(
            "/api/shipping/fulfill",
            json={
                "so_id": so_id,
                "tracking_number": "1Z999AA10123456784",
                "carrier": "UPS",
                "ship_method": "GROUND",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["fulfillment_id"] is not None
        assert data["tracking_number"] == "1Z999AA10123456784"
        assert data["carrier"] == "UPS"

        # Verify SO status
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = %s", (so_id,))
        assert status == "SHIPPED"

    def test_fulfill_stores_carrier_tracking_on_so(self, client, auth_headers):
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "1ZTRACK123", "carrier": "FedEx"},
            headers=auth_headers,
        )

        row = _query_one(
            "SELECT carrier, tracking_number FROM sales_orders WHERE so_id = %s", (so_id,)
        )
        assert row[0] == "FedEx"
        assert row[1] == "1ZTRACK123"

    def test_fulfill_creates_fulfillment_lines(self, client, auth_headers):
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "TRACK123", "carrier": "FedEx"},
            headers=auth_headers,
        )
        fid = resp.get_json()["fulfillment_id"]

        count = _query_val(
            "SELECT COUNT(*) FROM item_fulfillment_lines WHERE fulfillment_id = %s", (fid,)
        )
        assert count > 0, "Fulfillment lines should be created"

    def test_fulfill_not_packed(self, client, auth_headers):
        _set_setting("require_packing_before_shipping", "true")
        # SO-2026-001 is still OPEN
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "TRACK", "carrier": "UPS"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_fulfill_missing_tracking(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "carrier": "UPS"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "tracking" in resp.get_json()["error"].lower()

    def test_fulfill_missing_carrier(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "TRACK"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "carrier" in resp.get_json()["error"].lower()

    def test_fulfill_creates_audit_log(self, client, auth_headers):
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "AUDIT-TRACK", "carrier": "USPS"},
            headers=auth_headers,
        )

        row = _query_one("SELECT log_id FROM audit_log WHERE action_type = 'SHIP'")
        assert row is not None, "Audit log should record shipment"

    def test_fulfill_updates_so_line_quantities(self, client, auth_headers):
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "QTY-TRACK", "carrier": "UPS"},
            headers=auth_headers,
        )

        status = _query_val(
            "SELECT status FROM sales_order_lines WHERE so_id = %s LIMIT 1", (so_id,)
        )
        assert status == "SHIPPED"

    def test_fulfill_not_found(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 9999, "tracking_number": "TRACK", "carrier": "UPS"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_shipping_requires_auth(self, client):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "T", "carrier": "UPS"},
        )
        assert resp.status_code == 401

    def test_fulfill_carrier_too_long(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "T", "carrier": "X" * 101},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "carrier" in resp.get_json()["error"].lower()
        assert "100" in resp.get_json()["error"]

    def test_fulfill_tracking_too_long(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "T" * 256, "carrier": "UPS"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "tracking" in resp.get_json()["error"].lower()
        assert "255" in resp.get_json()["error"]

    def test_fulfill_whitespace_only_carrier(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "T", "carrier": "   "},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "carrier" in resp.get_json()["error"].lower()

    def test_fulfill_non_string_carrier(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "T", "carrier": 123},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "string" in resp.get_json()["error"].lower()


# ── Order Lookup Validation ──────────────────────────────────────────────────


class TestOrderLookupValidation:
    def test_lookup_barcode_too_long(self, client, auth_headers):
        resp = client.get(f"/api/shipping/order/{'X' * 101}", headers=auth_headers)
        assert resp.status_code == 400
        assert "100" in resp.get_json()["error"]


# ── Packing Toggle ───────────────────────────────────────────────────────────


class TestPackingToggle:
    def test_fulfill_picked_order_when_packing_not_required(self, client, auth_headers):
        """When packing toggle is OFF, PICKED orders can be shipped directly."""
        _set_setting("require_packing_before_shipping", "false")
        so_id = _advance_so_to_picked(client, auth_headers, "SO-2026-001")

        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "DIRECT-SHIP", "carrier": "UPS"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["tracking_number"] == "DIRECT-SHIP"

        status = _query_val("SELECT status FROM sales_orders WHERE so_id = %s", (so_id,))
        assert status == "SHIPPED"

    def test_fulfill_picked_order_rejected_when_packing_required(self, client, auth_headers):
        """When packing toggle is ON, PICKED orders cannot be shipped."""
        _set_setting("require_packing_before_shipping", "true")
        so_id = _advance_so_to_picked(client, auth_headers, "SO-2026-001")

        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "BLOCKED", "carrier": "UPS"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_fulfill_packed_order_works_regardless_of_toggle(self, client, auth_headers):
        """PACKED orders can always be shipped, regardless of toggle state."""
        _set_setting("require_packing_before_shipping", "false")
        so_id = _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": so_id, "tracking_number": "PACKED-SHIP", "carrier": "FedEx"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
