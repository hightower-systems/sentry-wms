import jwt
import pytest
from datetime import datetime, timezone, timedelta

from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _picker_headers(client):
    """Create a PICKER user and return auth headers for role enforcement tests."""
    conn = get_raw_connection()
    cur = conn.cursor()
    # bcrypt hash of 'picker123'
    import bcrypt
    pw_hash = bcrypt.hashpw(b"picker123", bcrypt.gensalt()).decode("utf-8")
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, warehouse_id) VALUES ('picker1', %s, 'Test Picker', 'PICKER', 1)",
        (pw_hash,),
    )
    cur.close()

    resp = client.post("/api/auth/login", json={"username": "picker1", "password": "picker123"})
    token = resp.get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ── Warehouses ────────────────────────────────────────────────────────────────

class TestWarehouses:
    def test_list_warehouses(self, client, auth_headers):
        resp = client.get("/api/admin/warehouses", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["warehouses"]) >= 1
        assert data["warehouses"][0]["warehouse_code"] == "APT-LAB"
        assert "total" in data
        assert "page" in data
        assert "per_page" in data
        assert "pages" in data

    def test_get_warehouse(self, client, auth_headers):
        resp = client.get("/api/admin/warehouses/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["warehouse"]["warehouse_code"] == "APT-LAB"
        assert len(data["zones"]) == 6

    def test_get_warehouse_not_found(self, client, auth_headers):
        resp = client.get("/api/admin/warehouses/9999", headers=auth_headers)
        assert resp.status_code == 404

    def test_create_warehouse(self, client, auth_headers):
        resp = client.post("/api/admin/warehouses", json={
            "warehouse_code": "WH-02", "warehouse_name": "Second Warehouse", "address": "456 Oak St"
        }, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.get_json()["warehouse_code"] == "WH-02"

    def test_create_warehouse_duplicate_code(self, client, auth_headers):
        resp = client.post("/api/admin/warehouses", json={
            "warehouse_code": "APT-LAB", "warehouse_name": "Dupe"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "Duplicate" in resp.get_json()["error"]

    def test_update_warehouse(self, client, auth_headers):
        resp = client.put("/api/admin/warehouses/1", json={
            "warehouse_name": "Updated Lab"
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["warehouse_name"] == "Updated Lab"


# ── Zones ─────────────────────────────────────────────────────────────────────

class TestZones:
    def test_list_zones(self, client, auth_headers):
        resp = client.get("/api/admin/zones?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["zones"]) == 6
        assert "total" in data

    def test_list_zones_pagination(self, client, auth_headers):
        resp = client.get("/api/admin/zones?warehouse_id=1&per_page=2&page=1", headers=auth_headers)
        data = resp.get_json()
        assert len(data["zones"]) == 2
        assert data["total"] == 6
        assert data["pages"] == 3

    def test_create_zone(self, client, auth_headers):
        resp = client.post("/api/admin/zones", json={
            "warehouse_id": 1, "zone_code": "TEST", "zone_name": "Test Zone", "zone_type": "STORAGE"
        }, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.get_json()["zone_code"] == "TEST"

    def test_create_zone_invalid_type(self, client, auth_headers):
        resp = client.post("/api/admin/zones", json={
            "warehouse_id": 1, "zone_code": "BAD", "zone_name": "Bad", "zone_type": "INVALID"
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_create_zone_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/zones", json={
            "warehouse_id": 1, "zone_code": "RCV", "zone_name": "Dupe", "zone_type": "RECEIVING"
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_update_zone(self, client, auth_headers):
        resp = client.put("/api/admin/zones/1", json={"zone_name": "Updated Receiving"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["zone_name"] == "Updated Receiving"


# ── Bins ──────────────────────────────────────────────────────────────────────

class TestBins:
    def test_list_bins(self, client, auth_headers):
        resp = client.get("/api/admin/bins?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["bins"]) == 16
        assert "zone_name" in data["bins"][0]
        assert "total" in data

    def test_list_bins_pagination(self, client, auth_headers):
        resp = client.get("/api/admin/bins?warehouse_id=1&per_page=3&page=1", headers=auth_headers)
        data = resp.get_json()
        assert len(data["bins"]) == 3
        assert data["total"] == 16
        assert data["page"] == 1

    def test_list_bins_filter_zone(self, client, auth_headers):
        # Zone 2 is PICK with 9 bins
        resp = client.get("/api/admin/bins?zone_id=2", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.get_json()["bins"]) == 9

    def test_get_bin_with_inventory(self, client, auth_headers):
        resp = client.get("/api/admin/bins/3", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["bin"]["bin_code"] == "A-01-01"
        assert len(data["inventory"]) >= 1

    def test_create_bin(self, client, auth_headers):
        resp = client.post("/api/admin/bins", json={
            "zone_id": 2, "warehouse_id": 1, "bin_code": "C-01-01", "bin_barcode": "BIN-C-01-01",
            "bin_type": "Pickable", "aisle": "C", "row_num": "01", "level_num": "01",
            "pick_sequence": 1000,
        }, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.get_json()["bin_code"] == "C-01-01"

    def test_create_bin_invalid_type(self, client, auth_headers):
        resp = client.post("/api/admin/bins", json={
            "zone_id": 2, "warehouse_id": 1, "bin_code": "X", "bin_barcode": "X", "bin_type": "BAD"
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_update_bin_pick_sequence(self, client, auth_headers):
        resp = client.put("/api/admin/bins/3", json={"pick_sequence": 999}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["pick_sequence"] == 999


# ── Items ─────────────────────────────────────────────────────────────────────

class TestItems:
    def test_list_items_paginated(self, client, auth_headers):
        resp = client.get("/api/admin/items?per_page=3&page=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["items"]) == 3
        assert data["total"] == 20
        assert data["pages"] == 7
        assert data["page"] == 1

    def test_list_items_filter_category(self, client, auth_headers):
        resp = client.get("/api/admin/items?category=Flies", headers=auth_headers)
        data = resp.get_json()
        assert data["total"] == 9
        assert all(i["category"] == "Flies" for i in data["items"])

    def test_get_item_with_inventory(self, client, auth_headers):
        resp = client.get("/api/admin/items/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["item"]["sku"] == "TST-001"
        assert len(data["inventory"]) >= 1

    def test_get_item_not_found(self, client, auth_headers):
        resp = client.get("/api/admin/items/9999", headers=auth_headers)
        assert resp.status_code == 404

    def test_create_item(self, client, auth_headers):
        resp = client.post("/api/admin/items", json={
            "sku": "NEW-ITEM", "item_name": "New Item", "upc": "999000000001", "category": "Test", "weight_lbs": 1.5
        }, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.get_json()["sku"] == "NEW-ITEM"

    def test_create_item_duplicate_sku(self, client, auth_headers):
        resp = client.post("/api/admin/items", json={
            "sku": "TST-001", "item_name": "Dupe"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "Duplicate SKU" in resp.get_json()["error"]

    def test_create_item_duplicate_upc(self, client, auth_headers):
        resp = client.post("/api/admin/items", json={
            "sku": "UNIQUE-SKU", "item_name": "Dupe UPC", "upc": "100000000001"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "Duplicate UPC" in resp.get_json()["error"]

    def test_update_item(self, client, auth_headers):
        resp = client.put("/api/admin/items/1", json={"item_name": "Updated Widget"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["item_name"] == "Updated Widget"

    def test_delete_item_with_inventory(self, client, auth_headers):
        # Item 1 has inventory, should fail
        resp = client.delete("/api/admin/items/1", headers=auth_headers)
        assert resp.status_code == 400
        assert "existing inventory" in resp.get_json()["error"]

    def test_delete_item_without_inventory(self, client, auth_headers):
        # Create an item with no inventory, then hard delete
        create = client.post("/api/admin/items", json={"sku": "DEL-ME", "item_name": "Delete Me"}, headers=auth_headers)
        item_id = create.get_json()["item_id"]

        resp = client.delete(f"/api/admin/items/{item_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Item deleted"

        exists = _query_val("SELECT 1 FROM items WHERE item_id = %s", (item_id,))
        assert exists is None


# ── Purchase Orders ───────────────────────────────────────────────────────────

class TestPurchaseOrders:
    def test_list_purchase_orders(self, client, auth_headers):
        resp = client.get("/api/admin/purchase-orders", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        assert "pages" in data

    def test_list_purchase_orders_filter_status(self, client, auth_headers):
        resp = client.get("/api/admin/purchase-orders?status=OPEN", headers=auth_headers)
        data = resp.get_json()
        assert all(po["status"] == "OPEN" for po in data["purchase_orders"])

    def test_get_purchase_order(self, client, auth_headers):
        resp = client.get("/api/admin/purchase-orders/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_number"] == "PO-2026-001"
        assert len(data["lines"]) == 10

    def test_create_purchase_order(self, client, auth_headers):
        resp = client.post("/api/admin/purchase-orders", json={
            "po_number": "PO-2026-006", "po_barcode": "PO-2026-006", "vendor_name": "Acme",
            "warehouse_id": 1, "lines": [
                {"item_id": 1, "quantity_ordered": 100, "unit_cost": 5.00, "line_number": 1},
                {"item_id": 2, "quantity_ordered": 50, "line_number": 2},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200  # returns via get_purchase_order
        data = resp.get_json()
        assert data["purchase_order"]["po_number"] == "PO-2026-006"
        assert len(data["lines"]) == 2

    def test_create_purchase_order_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/purchase-orders", json={
            "po_number": "PO-2026-001", "warehouse_id": 1, "lines": [{"item_id": 1, "quantity_ordered": 10, "line_number": 1}]
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_update_purchase_order(self, client, auth_headers):
        resp = client.put("/api/admin/purchase-orders/1", json={"vendor_name": "Updated Vendor"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["vendor_name"] == "Updated Vendor"

    def test_update_purchase_order_not_open(self, client, auth_headers):
        # Close the PO first
        client.post("/api/admin/purchase-orders/1/close", headers=auth_headers)
        resp = client.put("/api/admin/purchase-orders/1", json={"vendor_name": "Fail"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_close_purchase_order(self, client, auth_headers):
        resp = client.post("/api/admin/purchase-orders/1/close", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Purchase order closed"


# ── Sales Orders ──────────────────────────────────────────────────────────────

class TestSalesOrders:
    def test_list_sales_orders(self, client, auth_headers):
        resp = client.get("/api/admin/sales-orders", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 20

    def test_list_sales_orders_includes_shipping_fields(self, client, auth_headers):
        resp = client.get("/api/admin/sales-orders", headers=auth_headers)
        assert resp.status_code == 200
        so = resp.get_json()["sales_orders"][0]
        assert "carrier" in so
        assert "tracking_number" in so
        assert "shipped_at" in so

    def test_get_sales_order(self, client, auth_headers):
        resp = client.get("/api/admin/sales-orders/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["so_number"] == "SO-2026-001"
        assert len(data["lines"]) == 1

    def test_create_sales_order(self, client, auth_headers):
        resp = client.post("/api/admin/sales-orders", json={
            "so_number": "SO-2026-021", "customer_name": "New Customer", "warehouse_id": 1,
            "ship_method": "GROUND", "lines": [
                {"item_id": 1, "quantity_ordered": 5, "line_number": 1},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["so_number"] == "SO-2026-021"

    def test_create_sales_order_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/sales-orders", json={
            "so_number": "SO-2026-001", "warehouse_id": 1, "lines": [{"item_id": 1, "quantity_ordered": 1, "line_number": 1}]
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_update_sales_order(self, client, auth_headers):
        resp = client.put("/api/admin/sales-orders/1", json={"customer_name": "Updated Customer"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["customer_name"] == "Updated Customer"

    def test_cancel_open_sales_order(self, client, auth_headers):
        resp = client.post("/api/admin/sales-orders/1/cancel", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Sales order cancelled"
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "CANCELLED"

    def test_cancel_picking_releases_inventory(self, client, auth_headers):
        # Create batch sets SO-2026-001 to PICKING
        client.post("/api/picking/create-batch", json={"so_identifiers": ["SO-2026-001"], "warehouse_id": 1}, headers=auth_headers)
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "PICKING"

        # Cancel should release allocation
        resp = client.post("/api/admin/sales-orders/1/cancel", headers=auth_headers)
        assert resp.status_code == 200

        # Inventory allocated should be back to 0 for item 1 bin 3
        allocated = _query_val("SELECT quantity_allocated FROM inventory WHERE item_id = 1 AND bin_id = 3")
        assert allocated == 0

    def test_cancel_shipped_fails(self, client, auth_headers):
        # Set SO to SHIPPED status directly
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("UPDATE sales_orders SET status = 'SHIPPED' WHERE so_id = 1")
        cur.close()

        resp = client.post("/api/admin/sales-orders/1/cancel", headers=auth_headers)
        assert resp.status_code == 400


# ── Users ─────────────────────────────────────────────────────────────────────

class TestUsers:
    def test_list_users(self, client, auth_headers):
        resp = client.get("/api/admin/users", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["users"]) >= 1
        assert "total" in data
        assert "page" in data
        # password_hash should never be present
        for u in data["users"]:
            assert "password_hash" not in u

    def test_create_user(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "newpicker", "password": "testpass1", "full_name": "New Picker", "role": "USER", "warehouse_id": 1
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["username"] == "newpicker"
        assert "password_hash" not in data

    def test_create_user_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "admin", "password": "testpass1234", "full_name": "Dupe", "role": "ADMIN"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "Duplicate" in resp.get_json()["error"]

    def test_create_user_invalid_role(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "bad", "password": "testpass1234", "full_name": "Bad", "role": "SUPERUSER"
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_create_user_short_password(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "short", "password": "abc1", "full_name": "Short", "role": "USER"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "8 characters" in resp.get_json()["error"]

    def test_create_user_no_digit_password(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "nodigit", "password": "abcdefgh", "full_name": "No Digit", "role": "USER"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "digit" in resp.get_json()["error"]

    def test_create_user_no_letter_password(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "noletter", "password": "12345678", "full_name": "No Letter", "role": "USER"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "letter" in resp.get_json()["error"]

    def test_update_user(self, client, auth_headers):
        resp = client.put("/api/admin/users/1", json={"full_name": "Updated Admin"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["full_name"] == "Updated Admin"

    def test_update_user_password(self, client, auth_headers):
        # Create a user, update password, verify login works
        client.post("/api/admin/users", json={
            "username": "pwtest", "password": "oldpass1234", "full_name": "PW Test", "role": "USER", "warehouse_id": 1
        }, headers=auth_headers)

        user_id = _query_val("SELECT user_id FROM users WHERE username = 'pwtest'")
        client.put(f"/api/admin/users/{user_id}", json={"password": "newpass1234"}, headers=auth_headers)

        # Login with new password
        resp = client.post("/api/auth/login", json={"username": "pwtest", "password": "newpass1234"})
        assert resp.status_code == 200

    def test_update_user_accepts_full_admin_panel_payload(self, client, auth_headers):
        # Regression guard for #63: the admin panel's Users edit form
        # POSTs the whole form shape (full_name, role, warehouse_ids,
        # allowed_functions, password). Before the fix it also posted
        # `username`, which UpdateUserRequest does not declare and V-017
        # extras=forbid rejected with validation_error. This test pins
        # the post-fix payload shape so a future UI change that sneaks
        # an extra field back in trips here before landing in prod.
        client.post("/api/admin/users", json={
            "username": "panel_edit_fixture",
            "password": "orig_pw_1234",
            "full_name": "Panel Fixture",
            "role": "USER",
            "warehouse_id": 1,
            "allowed_functions": ["pick", "pack"],
        }, headers=auth_headers)
        user_id = _query_val(
            "SELECT user_id FROM users WHERE username = 'panel_edit_fixture'"
        )

        # Exactly what admin/src/pages/Users.jsx::save sends on the
        # editId branch (with a password set).
        edit_body = {
            "full_name": "Panel Fixture Renamed",
            "role": "USER",
            "warehouse_ids": [1],
            "allowed_functions": ["pick", "pack", "count"],
            "password": "new_pw_1234",
        }
        resp = client.put(
            f"/api/admin/users/{user_id}", json=edit_body, headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["full_name"] == "Panel Fixture Renamed"
        assert data["allowed_functions"] == ["pick", "pack", "count"]

        # Password actually took effect.
        login = client.post(
            "/api/auth/login",
            json={"username": "panel_edit_fixture", "password": "new_pw_1234"},
        )
        assert login.status_code == 200

    def test_update_user_rejects_username_field(self, client, auth_headers):
        # Make the invariant explicit: username is intentionally NOT in
        # UpdateUserRequest (the user_id is in the URL path; renaming a
        # user is a separate operation). The v1.3 V-017 extras=forbid
        # gate catches it. This test pins that rejection so a future
        # "let's add username for convenience" PR has to also think
        # about the admin panel's PUT payload.
        resp = client.put(
            "/api/admin/users/1",
            json={"username": "renamed_admin", "full_name": "X"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        details = resp.get_json()["details"]
        assert any(d.get("loc") == ["username"] for d in details)

    def test_delete_user(self, client, auth_headers):
        # Create a user then hard-delete
        create = client.post("/api/admin/users", json={
            "username": "delme", "password": "testpass1234", "full_name": "Del Me", "role": "USER"
        }, headers=auth_headers)
        uid = create.get_json()["user_id"]

        resp = client.delete(f"/api/admin/users/{uid}", headers=auth_headers)
        assert resp.status_code == 200

        exists = _query_val("SELECT 1 FROM users WHERE user_id = %s", (uid,))
        assert exists is None

    def test_cannot_delete_self(self, client, auth_headers):
        resp = client.delete("/api/admin/users/1", headers=auth_headers)
        assert resp.status_code == 400
        assert "yourself" in resp.get_json()["error"]


# ── Audit Log ─────────────────────────────────────────────────────────────────

class TestAuditLog:
    def test_list_audit_log(self, client, auth_headers):
        # Generate an audit entry by doing a transfer
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 3, "to_bin_id": 4, "quantity": 1
        }, headers=auth_headers)

        resp = client.get("/api/admin/audit-log", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        assert "pages" in data

    def test_audit_log_filter_action_type(self, client, auth_headers):
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 3, "to_bin_id": 4, "quantity": 1
        }, headers=auth_headers)

        resp = client.get("/api/admin/audit-log?action_type=TRANSFER", headers=auth_headers)
        data = resp.get_json()
        assert all(e["action_type"] == "TRANSFER" for e in data["entries"])


# ── Inventory Overview ────────────────────────────────────────────────────────

class TestInventoryOverview:
    def test_list_inventory(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 20
        assert "sku" in data["inventory"][0]
        assert "bin_code" in data["inventory"][0]
        assert "quantity_available" in data["inventory"][0]

    def test_inventory_filter_item(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?item_id=1", headers=auth_headers)
        data = resp.get_json()
        assert data["total"] == 1
        assert data["inventory"][0]["sku"] == "TST-001"

    def test_inventory_pagination(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?per_page=3&page=1", headers=auth_headers)
        data = resp.get_json()
        assert len(data["inventory"]) == 3
        assert data["total"] == 20
        assert data["pages"] == 7


# ── CSV Import ────────────────────────────────────────────────────────────────

class TestCsvImport:
    def test_import_items_success(self, client, auth_headers):
        resp = client.post("/api/admin/import/items", json={
            "records": [
                {"sku": "IMP-001", "item_name": "Import 1", "upc": "900000000001", "category": "Test", "weight_lbs": 1.0},
                {"sku": "IMP-002", "item_name": "Import 2", "upc": "900000000002", "category": "Test", "weight_lbs": 2.0},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 2
        assert data["imported"] == 2
        assert data["skipped"] == 0

    def test_import_items_with_errors(self, client, auth_headers):
        resp = client.post("/api/admin/import/items", json={
            "records": [
                {"sku": "TST-001", "item_name": "Dupe"},  # duplicate SKU
                {"sku": "IMP-OK", "item_name": "Good Item"},
                {"item_name": "No SKU"},  # missing sku
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 1
        assert data["skipped"] == 2
        assert len(data["errors"]) == 2

    def test_import_bins_success(self, client, auth_headers):
        resp = client.post("/api/admin/import/bins", json={
            "records": [
                {"bin_code": "D-01-01", "bin_barcode": "BIN-D-01-01", "bin_type": "Pickable",
                 "zone_id": 2, "warehouse_id": 1, "pick_sequence": 1100},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 1

    def test_import_invalid_entity(self, client, auth_headers):
        resp = client.post("/api/admin/import/invalid", json={"records": []}, headers=auth_headers)
        assert resp.status_code == 400

    def test_import_rejects_over_5000_records(self, client, auth_headers):
        records = [{"sku": f"BULK-{i}", "item_name": f"Item {i}"} for i in range(5001)]
        resp = client.post("/api/admin/import/items", json={"records": records}, headers=auth_headers)
        assert resp.status_code == 400
        assert "5000" in resp.get_json()["error"]


# ── Dashboard Stats ───────────────────────────────────────────────────────────

class TestDashboard:
    def test_dashboard_stats(self, client, auth_headers):
        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["open_pos"] >= 1
        assert data["open_sos"] >= 20
        assert data["total_skus"] == 20
        assert data["total_bins"] == 16
        assert isinstance(data["recent_activity"], list)
        # Toggle-aware fields (packing ON by default)
        assert "require_packing" in data
        assert data["require_packing"] is True
        assert "ready_to_pack" in data
        assert "orders_packed" in data
        assert "ready_to_ship" in data

    def test_dashboard_without_warehouse_filter(self, client, auth_headers):
        resp = client.get("/api/admin/dashboard", headers=auth_headers)
        assert resp.status_code == 200
        assert "total_skus" in resp.get_json()

    def test_dashboard_packed_count(self, client, auth_headers):
        """orders_packed should reflect PACKED orders."""
        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        initial_packed = resp.get_json()["orders_packed"]
        assert initial_packed == 0

        # Advance SO-2026-001 through picking
        create_resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-2026-001"], "warehouse_id": 1},
            headers=auth_headers,
        )
        batch_id = create_resp.get_json()["batch_id"]
        while True:
            next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
            data = next_resp.get_json()
            if "message" in data:
                break
            client.post("/api/picking/confirm", json={
                "pick_task_id": data["pick_task_id"],
                "scanned_barcode": data["upc"],
                "quantity_picked": data["quantity_to_pick"],
            }, headers=auth_headers)
        client.post("/api/picking/complete-batch", json={"batch_id": batch_id}, headers=auth_headers)

        # Pack all items
        order_resp = client.get("/api/packing/order/SO-2026-001", headers=auth_headers)
        for line in order_resp.get_json()["lines"]:
            remaining = line["quantity_picked"] - line["quantity_packed"]
            if remaining > 0:
                client.post("/api/packing/verify", json={
                    "so_id": 1, "scanned_barcode": line["upc"], "quantity": remaining,
                }, headers=auth_headers)
        client.post("/api/packing/complete", json={"so_id": 1}, headers=auth_headers)

        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        assert resp.get_json()["orders_packed"] == 1

    def test_dashboard_packing_off_hides_pack_fields(self, client, auth_headers):
        """When packing is OFF, dashboard omits ready_to_pack and orders_packed."""
        from db_test_context import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('require_packing_before_shipping', 'false') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )
        cur.close()

        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        data = resp.get_json()
        assert data["require_packing"] is False
        assert "ready_to_pack" not in data
        assert "orders_packed" not in data
        assert "ready_to_ship" in data

    def test_dashboard_packing_off_ready_to_ship_includes_picked(self, client, auth_headers):
        """When packing is OFF, ready_to_ship includes PICKED orders."""
        from test_shipping import _advance_so_to_picked
        from db_test_context import get_raw_connection
        _advance_so_to_picked(client, auth_headers, "SO-2026-001")

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('require_packing_before_shipping', 'false') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )
        cur.close()

        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        assert resp.get_json()["ready_to_ship"] >= 1


# ── Role Enforcement ──────────────────────────────────────────────────────────

class TestRoleEnforcement:
    def test_picker_cannot_create_item(self, client, auth_headers):
        headers = _picker_headers(client)
        resp = client.post("/api/admin/items", json={
            "sku": "BLOCKED", "item_name": "Blocked"
        }, headers=headers)
        assert resp.status_code == 403

    def test_picker_cannot_read_admin_items(self, client, auth_headers):
        headers = _picker_headers(client)
        resp = client.get("/api/admin/items", headers=headers)
        assert resp.status_code == 403

    def test_picker_cannot_create_user(self, client, auth_headers):
        headers = _picker_headers(client)
        resp = client.post("/api/admin/users", json={
            "username": "x", "password": "x", "full_name": "x", "role": "USER"
        }, headers=headers)
        assert resp.status_code == 403


# -- Items default_bin_code --------------------------------------------------

class TestItemsDefaultBin:
    def test_items_list_includes_default_bin_code(self, client, auth_headers):
        resp = client.get("/api/admin/items", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        # Item 1 (TST-001) has default_bin_id = 3 (A-01-01)
        item1 = next(i for i in data["items"] if i["sku"] == "TST-001")
        assert "default_bin_code" in item1
        assert item1["default_bin_code"] == "A-01-01"

    def test_items_preferred_bin_overrides_default(self, client, auth_headers):
        # Insert preferred bin pointing to bin 4 (A-01-02)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO preferred_bins (item_id, bin_id, priority) VALUES (1, 4, 1)")
        cur.close()

        resp = client.get("/api/admin/items", headers=auth_headers)
        data = resp.get_json()
        item1 = next(i for i in data["items"] if i["sku"] == "TST-001")
        assert item1["default_bin_code"] == "A-01-02"


# -- Settings ----------------------------------------------------------------

class TestSettings:
    def test_get_settings(self, client, auth_headers):
        resp = client.get("/api/admin/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "settings" in data

    def test_update_settings(self, client, auth_headers):
        resp = client.put("/api/admin/settings", json={
            "settings": {"count_show_expected": "false"}
        }, headers=auth_headers)
        assert resp.status_code == 200

        # Verify it was saved
        resp = client.get("/api/admin/settings", headers=auth_headers)
        settings = {s["key"]: s["value"] for s in resp.get_json()["settings"]}
        assert settings.get("count_show_expected") == "false"

    def test_update_packing_toggle(self, client, auth_headers):
        resp = client.put("/api/admin/settings", json={
            "settings": {"require_packing_before_shipping": "false"}
        }, headers=auth_headers)
        assert resp.status_code == 200

        resp = client.get("/api/admin/settings/require_packing_before_shipping", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["value"] == "false"

    def test_toggle_off_rejected_when_packed_orders_exist(self, client, auth_headers):
        """Cannot disable packing when PACKED orders exist."""
        # Advance SO-2026-001 to PACKED
        from test_shipping import _advance_so_to_packed
        _advance_so_to_packed(client, auth_headers, "SO-2026-001")

        resp = client.put("/api/admin/settings", json={
            "settings": {"require_packing_before_shipping": "false"}
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "PACKED" in resp.get_json()["error"]

    def test_toggle_on_always_allowed(self, client, auth_headers):
        """Enabling packing is always allowed."""
        resp = client.put("/api/admin/settings", json={
            "settings": {"require_packing_before_shipping": "true"}
        }, headers=auth_headers)
        assert resp.status_code == 200

    def test_update_settings_missing_body(self, client, auth_headers):
        resp = client.put("/api/admin/settings", json={}, headers=auth_headers)
        assert resp.status_code == 400


# -- Cycle Counts ------------------------------------------------------------

class TestCycleCounts:
    def test_list_cycle_counts_empty(self, client, auth_headers):
        resp = client.get("/api/admin/cycle-counts", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "cycle_counts" in data
        assert isinstance(data["cycle_counts"], list)

    def test_list_cycle_counts_after_creation(self, client, auth_headers):
        # Create a cycle count via the inventory endpoint
        client.post("/api/inventory/cycle-count/create", json={
            "bin_ids": [3], "warehouse_id": 1,
        }, headers=auth_headers)

        resp = client.get("/api/admin/cycle-counts", headers=auth_headers)
        data = resp.get_json()
        assert len(data["cycle_counts"]) >= 1
        cc = data["cycle_counts"][0]
        assert "count_id" in cc
        assert "bin_code" in cc
        assert "status" in cc


# -- Preferred Bins CRUD ----------------------------------------------------

class TestPreferredBinsCRUD:
    def test_list_preferred_bins_empty(self, client, auth_headers):
        resp = client.get("/api/admin/preferred-bins", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["preferred_bins"] == []

    def test_create_preferred_bin(self, client, auth_headers):
        resp = client.post("/api/admin/preferred-bins", json={
            "item_id": 1, "bin_id": 3, "priority": 1,
        }, headers=auth_headers)
        assert resp.status_code == 201 or resp.status_code == 200

    def test_list_preferred_bins_after_create(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={
            "item_id": 1, "bin_id": 3, "priority": 1,
        }, headers=auth_headers)

        resp = client.get("/api/admin/preferred-bins", headers=auth_headers)
        data = resp.get_json()
        assert len(data["preferred_bins"]) >= 1
        pb = data["preferred_bins"][0]
        assert pb["item_id"] == 1
        assert pb["bin_id"] == 3
        assert pb["priority"] == 1
        assert "sku" in pb
        assert "bin_code" in pb

    def test_list_preferred_bins_filter_item(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={"item_id": 1, "bin_id": 3, "priority": 1}, headers=auth_headers)
        client.post("/api/admin/preferred-bins", json={"item_id": 2, "bin_id": 4, "priority": 1}, headers=auth_headers)

        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        data = resp.get_json()
        assert all(pb["item_id"] == 1 for pb in data["preferred_bins"])

    def test_update_preferred_bin_priority(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={"item_id": 1, "bin_id": 3, "priority": 1}, headers=auth_headers)

        # Get the preferred_bin_id
        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        pb_id = resp.get_json()["preferred_bins"][0]["preferred_bin_id"]

        resp = client.put(f"/api/admin/preferred-bins/{pb_id}", json={"priority": 5}, headers=auth_headers)
        assert resp.status_code == 200

        # Verify updated
        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        assert resp.get_json()["preferred_bins"][0]["priority"] == 5

    def test_delete_preferred_bin(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={"item_id": 1, "bin_id": 3, "priority": 1}, headers=auth_headers)

        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        pb_id = resp.get_json()["preferred_bins"][0]["preferred_bin_id"]

        resp = client.delete(f"/api/admin/preferred-bins/{pb_id}", headers=auth_headers)
        assert resp.status_code == 200

        # Verify deleted
        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        assert len(resp.get_json()["preferred_bins"]) == 0


# ══════════════════════════════════════════════════════════════════════════════
# REPEAT OFFENDER TESTS  -  v0.9.7
# Each bug was reported in v0.9.5, "fixed" in v0.9.6, but still broken.
# These tests MUST pass before marking any of them done.
# ══════════════════════════════════════════════════════════════════════════════


class TestRepeatOffender12_LoginUsername:
    """Bug #12: Admin login  -  username must NOT clear on bad password."""

    def test_bad_password_returns_401_not_redirect(self, client):
        """The /auth/login endpoint must return 401 JSON, not trigger a redirect."""
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-password"
        })
        assert resp.status_code == 401
        data = resp.get_json()
        assert "error" in data or "message" in data

    def test_correct_login_still_works(self, client):
        """Ensure valid credentials still return 200 with token."""
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin"
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "token" in data


class TestRepeatOffender19_ItemWeight:
    """Bug #19: Item weight must persist through create and display."""

    def test_create_item_with_weight(self, client, auth_headers):
        """Creating an item with weight_lbs must store and return it."""
        resp = client.post("/api/admin/items", json={
            "sku": "WEIGHT-TEST-001",
            "item_name": "Weight Test Item",
            "weight_lbs": 2.5,
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["weight_lbs"] == 2.5

    def test_list_items_shows_weight(self, client, auth_headers):
        """The items list endpoint must include weight_lbs."""
        client.post("/api/admin/items", json={
            "sku": "WEIGHT-TEST-002",
            "item_name": "Weight Test Item 2",
            "weight_lbs": 3.75,
        }, headers=auth_headers)

        resp = client.get("/api/admin/items?q=WEIGHT-TEST-002", headers=auth_headers)
        assert resp.status_code == 200
        items = resp.get_json()["items"]
        match = [i for i in items if i["sku"] == "WEIGHT-TEST-002"]
        assert len(match) == 1
        assert match[0]["weight_lbs"] == 3.75

    def test_update_item_weight(self, client, auth_headers):
        """Updating weight_lbs must persist."""
        resp = client.post("/api/admin/items", json={
            "sku": "WEIGHT-TEST-003",
            "item_name": "Weight Update Test",
            "weight_lbs": 1.0,
        }, headers=auth_headers)
        item_id = resp.get_json()["item_id"]

        resp = client.put(f"/api/admin/items/{item_id}", json={
            "weight_lbs": 5.5,
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["weight_lbs"] == 5.5

        resp = client.get(f"/api/admin/items/{item_id}", headers=auth_headers)
        assert resp.get_json()["item"]["weight_lbs"] == 5.5


class TestRepeatOffender20_AuditLogNames:
    """Bug #20: Audit log must show human-readable names, not internal IDs."""

    def test_audit_log_has_entity_name(self, client, auth_headers):
        """entity_name must be resolved (SKU, bin_code, etc.)."""
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 3, "to_bin_id": 4, "quantity": 1,
        }, headers=auth_headers)

        resp = client.get("/api/admin/audit-log?action_type=TRANSFER", headers=auth_headers)
        assert resp.status_code == 200
        entries = resp.get_json()["entries"]
        assert len(entries) >= 1
        for e in entries:
            if e["entity_type"] in ("ITEM", "BIN", "SO", "PO"):
                assert e["entity_name"] is not None, f"entity_name is None for {e['entity_type']} id={e['entity_id']}"

    def test_audit_log_has_username_not_id(self, client, auth_headers):
        """username field must be a string name, not a numeric ID."""
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 3, "to_bin_id": 4, "quantity": 1,
        }, headers=auth_headers)

        resp = client.get("/api/admin/audit-log?action_type=TRANSFER", headers=auth_headers)
        entries = resp.get_json()["entries"]
        assert len(entries) >= 1
        for e in entries:
            assert isinstance(e["username"], str), f"username is not a string: {e['username']}"
            assert not e["username"].isdigit(), f"username looks like an ID: {e['username']}"

    def test_audit_log_details_resolved(self, client, auth_headers):
        """Details JSON should resolve IDs to names where possible."""
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 3, "to_bin_id": 4, "quantity": 1,
        }, headers=auth_headers)

        resp = client.get("/api/admin/audit-log?action_type=TRANSFER", headers=auth_headers)
        entries = resp.get_json()["entries"]
        transfer_entry = entries[0]
        details = transfer_entry.get("details", {})
        for key in details:
            assert key not in ("bin_id", "item_id", "from_bin_id", "to_bin_id"), \
                f"Details still contains raw ID key: {key}={details[key]}"


class TestRepeatOffender21_ReceivingBinFilter:
    """Bug #21: /admin/bins endpoint must support bin_type filter."""

    def test_bins_filter_by_type(self, client, auth_headers):
        """Requesting bin_type=Staging must return only Staging bins."""
        resp = client.get("/api/admin/bins?warehouse_id=1&bin_type=Staging", headers=auth_headers)
        assert resp.status_code == 200
        bins = resp.get_json()["bins"]
        assert len(bins) >= 1, "Expected at least one Staging bin"
        for b in bins:
            assert b["bin_type"] == "Staging", f"Got bin_type={b['bin_type']} for bin {b['bin_code']}"

    def test_bins_filter_pickable(self, client, auth_headers):
        """bin_type=Pickable should return only Pickable bins."""
        resp = client.get("/api/admin/bins?warehouse_id=1&bin_type=Pickable", headers=auth_headers)
        assert resp.status_code == 200
        bins = resp.get_json()["bins"]
        for b in bins:
            assert b["bin_type"] == "Pickable", f"Got bin_type={b['bin_type']}"

    def test_bins_no_filter_returns_all(self, client, auth_headers):
        """Without bin_type filter, all bin types should appear."""
        resp = client.get("/api/admin/bins?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        bins = resp.get_json()["bins"]
        types = set(b["bin_type"] for b in bins)
        assert len(types) > 1, f"Expected multiple bin types, got {types}"


class TestRepeatOffender23_WarehouseHardDelete:
    """Bug #23: DELETE warehouse must hard-delete, not just set inactive."""

    def test_delete_warehouse_hard_deletes(self, client, auth_headers):
        """After DELETE, the warehouse should not exist at all."""
        resp = client.post("/api/admin/warehouses", json={
            "warehouse_code": "DEL-TEST", "warehouse_name": "Delete Test WH",
        }, headers=auth_headers)
        assert resp.status_code == 201
        wh_id = resp.get_json()["warehouse_id"]

        resp = client.delete(f"/api/admin/warehouses/{wh_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert "deleted" in resp.get_json()["message"].lower()

        resp = client.get(f"/api/admin/warehouses/{wh_id}", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_warehouse_with_bins_blocked(self, client, auth_headers):
        """Cannot hard-delete a warehouse that still has bins."""
        resp = client.delete("/api/admin/warehouses/1", headers=auth_headers)
        assert resp.status_code == 400

    def test_inactive_toggle_via_put(self, client, auth_headers):
        """Setting is_active=false via PUT should soft-toggle, not delete."""
        resp = client.post("/api/admin/warehouses", json={
            "warehouse_code": "SOFT-TEST", "warehouse_name": "Soft Toggle Test",
        }, headers=auth_headers)
        wh_id = resp.get_json()["warehouse_id"]

        resp = client.put(f"/api/admin/warehouses/{wh_id}", json={"is_active": False}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["is_active"] is False

        resp = client.get(f"/api/admin/warehouses/{wh_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["warehouse"]["is_active"] is False


class TestInterWarehouseTransfersList:
    """Bug #65: GET /api/admin/inter-warehouse-transfers 500s on nonexistent bt.notes column."""

    def test_list_returns_200(self, client, auth_headers):
        """Endpoint must return 200 even when no transfers exist (fresh seed)."""
        resp = client.get("/api/admin/inter-warehouse-transfers?limit=50", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "transfers" in data
        assert isinstance(data["transfers"], list)


class TestZoneDelete:
    """Issue #86: DELETE /api/admin/zones/{id} must 409 when bins remain."""

    def test_delete_empty_zone_succeeds(self, client, auth_headers):
        resp = client.post(
            "/api/admin/zones",
            json={
                "zone_code": "ZDELTEST",
                "zone_name": "Zone Delete Test",
                "zone_type": "STORAGE",
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        zone_id = resp.get_json()["zone_id"]

        resp = client.delete(f"/api/admin/zones/{zone_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert "deleted" in resp.get_json()["message"].lower()

    def test_delete_zone_with_bins_returns_409(self, client, auth_headers):
        """Zone 1 in the demo seed is the Receiving zone with at least
        one bin attached."""
        resp = client.delete("/api/admin/zones/1", headers=auth_headers)
        assert resp.status_code == 409
        body = resp.get_json()
        assert "bin(s) are assigned" in body["error"]
        assert "Reassign or delete the bins first" in body["error"]

    def test_delete_missing_zone_returns_404(self, client, auth_headers):
        resp = client.delete("/api/admin/zones/99999", headers=auth_headers)
        assert resp.status_code == 404


class TestBinDelete:
    """Issue #85 follow-up: DELETE /api/admin/bins/{id} with confirmation
    dialog on the frontend and a 409 guard when the bin still has
    inventory or is referenced by preferred-bin mappings."""

    def test_delete_empty_bin_succeeds(self, client, auth_headers):
        resp = client.post(
            "/api/admin/bins",
            json={
                "bin_code": "DELTEST-1",
                "bin_barcode": "DELTEST-1",
                "bin_type": "Pickable",
                "zone_id": 2,
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        bin_id = resp.get_json()["bin_id"]

        resp = client.delete(f"/api/admin/bins/{bin_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert "deleted" in resp.get_json()["message"].lower()

        resp = client.get(f"/api/admin/bins/{bin_id}", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_bin_with_inventory_returns_409(self, client, auth_headers):
        """Bin 3 in the demo seed has inventory rows with quantity_on_hand > 0."""
        resp = client.delete("/api/admin/bins/3", headers=auth_headers)
        assert resp.status_code == 409
        body = resp.get_json()
        assert "inventory record" in body["error"].lower()
        assert "quantity on hand" in body["error"].lower()

    def test_delete_bin_with_preferred_mapping_returns_409(self, client, auth_headers):
        resp = client.post(
            "/api/admin/bins",
            json={
                "bin_code": "DELTEST-PREF",
                "bin_barcode": "DELTEST-PREF",
                "bin_type": "Pickable",
                "zone_id": 2,
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        bin_id = resp.get_json()["bin_id"]

        client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 5, "bin_id": bin_id, "priority": 1},
            headers=auth_headers,
        )

        resp = client.delete(f"/api/admin/bins/{bin_id}", headers=auth_headers)
        assert resp.status_code == 409
        assert "preferred-bin mapping" in resp.get_json()["error"].lower()

    def test_delete_missing_bin_returns_404(self, client, auth_headers):
        resp = client.delete("/api/admin/bins/99999", headers=auth_headers)
        assert resp.status_code == 404


class TestInventorySearchQ:
    """Issue #82: /admin/inventory must honour the `q` query parameter
    so the admin panel's SKU / item-name search actually filters rows."""

    def test_q_matches_sku(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?warehouse_id=1&q=TST-005", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        assert all(r["sku"] == "TST-005" for r in data["inventory"])

    def test_q_matches_item_name_case_insensitive(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?warehouse_id=1&q=fly%20line", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        assert all("fly line" in r["item_name"].lower() for r in data["inventory"])

    def test_q_with_no_matches_returns_empty(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?warehouse_id=1&q=no-such-item-zzz", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 0
        assert data["inventory"] == []

    def test_q_whitespace_only_ignored(self, client, auth_headers):
        """A user who types spaces and hits Enter should get the full
        list, not an empty filter. strip() on the server defends this."""
        baseline = client.get("/api/admin/inventory?warehouse_id=1", headers=auth_headers).get_json()["total"]
        resp = client.get("/api/admin/inventory?warehouse_id=1&q=%20%20%20", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["total"] == baseline
