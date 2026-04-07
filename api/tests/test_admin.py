import psycopg2
import os
import jwt
from datetime import datetime, timezone, timedelta


def _query_val(sql, params=None):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def _picker_headers(client):
    """Create a PICKER user and return auth headers for role enforcement tests."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    # bcrypt hash of 'picker123'
    import bcrypt
    pw_hash = bcrypt.hashpw(b"picker123", bcrypt.gensalt()).decode("utf-8")
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, warehouse_id) VALUES ('picker1', %s, 'Test Picker', 'PICKER', 1)",
        (pw_hash,),
    )
    cur.close()
    conn.close()

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

    def test_get_warehouse(self, client, auth_headers):
        resp = client.get("/api/admin/warehouses/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["warehouse"]["warehouse_code"] == "APT-LAB"
        assert len(data["zones"]) == 5

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
        assert len(resp.get_json()["zones"]) == 5

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
        assert len(data["bins"]) == 9
        assert "zone_name" in data["bins"][0]

    def test_list_bins_filter_zone(self, client, auth_headers):
        # Zone 2 is STOR with 6 bins
        resp = client.get("/api/admin/bins?zone_id=2", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.get_json()["bins"]) == 6

    def test_get_bin_with_inventory(self, client, auth_headers):
        resp = client.get("/api/admin/bins/2", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["bin"]["bin_code"] == "A-01-01"
        assert len(data["inventory"]) >= 1

    def test_create_bin(self, client, auth_headers):
        resp = client.post("/api/admin/bins", json={
            "zone_id": 2, "warehouse_id": 1, "bin_code": "C-01-01", "bin_barcode": "BIN-C-01-01",
            "bin_type": "STANDARD", "aisle": "C", "row_num": "01", "level_num": "01",
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
        resp = client.put("/api/admin/bins/2", json={"pick_sequence": 999}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["pick_sequence"] == 999


# ── Items ─────────────────────────────────────────────────────────────────────

class TestItems:
    def test_list_items_paginated(self, client, auth_headers):
        resp = client.get("/api/admin/items?per_page=3&page=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["items"]) == 3
        assert data["total"] == 10
        assert data["pages"] == 4
        assert data["page"] == 1

    def test_list_items_filter_category(self, client, auth_headers):
        resp = client.get("/api/admin/items?category=Widgets", headers=auth_headers)
        data = resp.get_json()
        assert data["total"] == 3
        assert all(i["category"] == "Widgets" for i in data["items"])

    def test_get_item_with_inventory(self, client, auth_headers):
        resp = client.get("/api/admin/items/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["item"]["sku"] == "WIDGET-BLU"
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
            "sku": "WIDGET-BLU", "item_name": "Dupe"
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
        # Create an item with no inventory, then delete
        create = client.post("/api/admin/items", json={"sku": "DEL-ME", "item_name": "Delete Me"}, headers=auth_headers)
        item_id = create.get_json()["item_id"]

        resp = client.delete(f"/api/admin/items/{item_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Item deactivated"

        active = _query_val("SELECT is_active FROM items WHERE item_id = %s", (item_id,))
        assert active is False


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
        assert data["purchase_order"]["po_number"] == "PO-001"
        assert len(data["lines"]) == 3

    def test_create_purchase_order(self, client, auth_headers):
        resp = client.post("/api/admin/purchase-orders", json={
            "po_number": "PO-002", "po_barcode": "PO-002", "vendor_name": "Acme",
            "warehouse_id": 1, "lines": [
                {"item_id": 1, "quantity_ordered": 100, "unit_cost": 5.00, "line_number": 1},
                {"item_id": 2, "quantity_ordered": 50, "line_number": 2},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200  # returns via get_purchase_order
        data = resp.get_json()
        assert data["purchase_order"]["po_number"] == "PO-002"
        assert len(data["lines"]) == 2

    def test_create_purchase_order_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/purchase-orders", json={
            "po_number": "PO-001", "warehouse_id": 1, "lines": [{"item_id": 1, "quantity_ordered": 10, "line_number": 1}]
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
        assert data["total"] >= 2

    def test_get_sales_order(self, client, auth_headers):
        resp = client.get("/api/admin/sales-orders/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["so_number"] == "SO-001"
        assert len(data["lines"]) == 2

    def test_create_sales_order(self, client, auth_headers):
        resp = client.post("/api/admin/sales-orders", json={
            "so_number": "SO-003", "customer_name": "New Customer", "warehouse_id": 1,
            "ship_method": "GROUND", "lines": [
                {"item_id": 1, "quantity_ordered": 5, "line_number": 1},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sales_order"]["so_number"] == "SO-003"

    def test_create_sales_order_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/sales-orders", json={
            "so_number": "SO-001", "warehouse_id": 1, "lines": [{"item_id": 1, "quantity_ordered": 1, "line_number": 1}]
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
        # Create batch sets SO-001 to PICKING
        client.post("/api/picking/create-batch", json={"so_identifiers": ["SO-001"], "warehouse_id": 1}, headers=auth_headers)
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "PICKING"

        # Cancel should release allocation
        resp = client.post("/api/admin/sales-orders/1/cancel", headers=auth_headers)
        assert resp.status_code == 200

        # Inventory allocated should be back to 0 for item 1 bin 2
        allocated = _query_val("SELECT quantity_allocated FROM inventory WHERE item_id = 1 AND bin_id = 2")
        assert allocated == 0

    def test_cancel_shipped_fails(self, client, auth_headers):
        # Set SO to SHIPPED status directly
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("UPDATE sales_orders SET status = 'SHIPPED' WHERE so_id = 1")
        cur.close()
        conn.close()

        resp = client.post("/api/admin/sales-orders/1/cancel", headers=auth_headers)
        assert resp.status_code == 400


# ── Users ─────────────────────────────────────────────────────────────────────

class TestUsers:
    def test_list_users(self, client, auth_headers):
        resp = client.get("/api/admin/users", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["users"]) >= 1
        # password_hash should never be present
        for u in data["users"]:
            assert "password_hash" not in u

    def test_create_user(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "newpicker", "password": "testpass", "full_name": "New Picker", "role": "PICKER", "warehouse_id": 1
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["username"] == "newpicker"
        assert "password_hash" not in data

    def test_create_user_duplicate(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "admin", "password": "test", "full_name": "Dupe", "role": "ADMIN"
        }, headers=auth_headers)
        assert resp.status_code == 400
        assert "Duplicate" in resp.get_json()["error"]

    def test_create_user_invalid_role(self, client, auth_headers):
        resp = client.post("/api/admin/users", json={
            "username": "bad", "password": "test", "full_name": "Bad", "role": "SUPERUSER"
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_update_user(self, client, auth_headers):
        resp = client.put("/api/admin/users/1", json={"full_name": "Updated Admin"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["full_name"] == "Updated Admin"

    def test_update_user_password(self, client, auth_headers):
        # Create a user, update password, verify login works
        client.post("/api/admin/users", json={
            "username": "pwtest", "password": "old", "full_name": "PW Test", "role": "PICKER", "warehouse_id": 1
        }, headers=auth_headers)

        user_id = _query_val("SELECT user_id FROM users WHERE username = 'pwtest'")
        client.put(f"/api/admin/users/{user_id}", json={"password": "newpass"}, headers=auth_headers)

        # Login with new password
        resp = client.post("/api/auth/login", json={"username": "pwtest", "password": "newpass"})
        assert resp.status_code == 200

    def test_delete_user(self, client, auth_headers):
        # Create a user then deactivate
        create = client.post("/api/admin/users", json={
            "username": "delme", "password": "test", "full_name": "Del Me", "role": "PICKER"
        }, headers=auth_headers)
        uid = create.get_json()["user_id"]

        resp = client.delete(f"/api/admin/users/{uid}", headers=auth_headers)
        assert resp.status_code == 200

        active = _query_val("SELECT is_active FROM users WHERE user_id = %s", (uid,))
        assert active is False

    def test_cannot_delete_self(self, client, auth_headers):
        resp = client.delete("/api/admin/users/1", headers=auth_headers)
        assert resp.status_code == 400
        assert "yourself" in resp.get_json()["error"]


# ── Audit Log ─────────────────────────────────────────────────────────────────

class TestAuditLog:
    def test_list_audit_log(self, client, auth_headers):
        # Generate an audit entry by doing a transfer
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 1
        }, headers=auth_headers)

        resp = client.get("/api/admin/audit-log", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        assert "pages" in data

    def test_audit_log_filter_action_type(self, client, auth_headers):
        client.post("/api/transfers/move", json={
            "item_id": 1, "from_bin_id": 2, "to_bin_id": 3, "quantity": 1
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
        assert data["total"] == 10
        assert "sku" in data["inventory"][0]
        assert "bin_code" in data["inventory"][0]
        assert "quantity_available" in data["inventory"][0]

    def test_inventory_filter_item(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?item_id=1", headers=auth_headers)
        data = resp.get_json()
        assert data["total"] == 1
        assert data["inventory"][0]["sku"] == "WIDGET-BLU"

    def test_inventory_pagination(self, client, auth_headers):
        resp = client.get("/api/admin/inventory?per_page=3&page=1", headers=auth_headers)
        data = resp.get_json()
        assert len(data["inventory"]) == 3
        assert data["total"] == 10
        assert data["pages"] == 4


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
                {"sku": "WIDGET-BLU", "item_name": "Dupe"},  # duplicate SKU
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
                {"bin_code": "D-01-01", "bin_barcode": "BIN-D-01-01", "bin_type": "STANDARD",
                 "zone_id": 2, "warehouse_id": 1, "pick_sequence": 1100},
            ]
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 1

    def test_import_invalid_entity(self, client, auth_headers):
        resp = client.post("/api/admin/import/invalid", json={"records": []}, headers=auth_headers)
        assert resp.status_code == 400


# ── Dashboard Stats ───────────────────────────────────────────────────────────

class TestDashboard:
    def test_dashboard_stats(self, client, auth_headers):
        resp = client.get("/api/admin/dashboard?warehouse_id=1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["open_pos"] >= 1
        assert data["open_sos"] >= 2
        assert data["total_skus"] == 10
        assert data["total_bins"] == 9
        assert isinstance(data["recent_activity"], list)

    def test_dashboard_without_warehouse_filter(self, client, auth_headers):
        resp = client.get("/api/admin/dashboard", headers=auth_headers)
        assert resp.status_code == 200
        assert "total_skus" in resp.get_json()


# ── Role Enforcement ──────────────────────────────────────────────────────────

class TestRoleEnforcement:
    def test_picker_cannot_create_item(self, client, auth_headers):
        headers = _picker_headers(client)
        resp = client.post("/api/admin/items", json={
            "sku": "BLOCKED", "item_name": "Blocked"
        }, headers=headers)
        assert resp.status_code == 403

    def test_picker_can_read_items(self, client, auth_headers):
        headers = _picker_headers(client)
        resp = client.get("/api/admin/items", headers=headers)
        assert resp.status_code == 200

    def test_picker_cannot_create_user(self, client, auth_headers):
        headers = _picker_headers(client)
        resp = client.post("/api/admin/users", json={
            "username": "x", "password": "x", "full_name": "x", "role": "PICKER"
        }, headers=headers)
        assert resp.status_code == 403


# -- Items default_bin_code --------------------------------------------------

class TestItemsDefaultBin:
    def test_items_list_includes_default_bin_code(self, client, auth_headers):
        resp = client.get("/api/admin/items", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        # Item 1 (WIDGET-BLU) has default_bin_id = 2 (A-01-01)
        item1 = next(i for i in data["items"] if i["sku"] == "WIDGET-BLU")
        assert "default_bin_code" in item1
        assert item1["default_bin_code"] == "A-01-01"

    def test_items_preferred_bin_overrides_default(self, client, auth_headers):
        # Insert preferred bin pointing to bin 3 (A-01-02)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("INSERT INTO preferred_bins (item_id, bin_id, priority) VALUES (1, 3, 1)")
        cur.close()
        conn.close()

        resp = client.get("/api/admin/items", headers=auth_headers)
        data = resp.get_json()
        item1 = next(i for i in data["items"] if i["sku"] == "WIDGET-BLU")
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
            "bin_ids": [2], "warehouse_id": 1,
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
            "item_id": 1, "bin_id": 2, "priority": 1,
        }, headers=auth_headers)
        assert resp.status_code == 201 or resp.status_code == 200

    def test_list_preferred_bins_after_create(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={
            "item_id": 1, "bin_id": 2, "priority": 1,
        }, headers=auth_headers)

        resp = client.get("/api/admin/preferred-bins", headers=auth_headers)
        data = resp.get_json()
        assert len(data["preferred_bins"]) >= 1
        pb = data["preferred_bins"][0]
        assert pb["item_id"] == 1
        assert pb["bin_id"] == 2
        assert pb["priority"] == 1
        assert "sku" in pb
        assert "bin_code" in pb

    def test_list_preferred_bins_filter_item(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={"item_id": 1, "bin_id": 2, "priority": 1}, headers=auth_headers)
        client.post("/api/admin/preferred-bins", json={"item_id": 2, "bin_id": 3, "priority": 1}, headers=auth_headers)

        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        data = resp.get_json()
        assert all(pb["item_id"] == 1 for pb in data["preferred_bins"])

    def test_update_preferred_bin_priority(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={"item_id": 1, "bin_id": 2, "priority": 1}, headers=auth_headers)

        # Get the preferred_bin_id
        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        pb_id = resp.get_json()["preferred_bins"][0]["preferred_bin_id"]

        resp = client.put(f"/api/admin/preferred-bins/{pb_id}", json={"priority": 5}, headers=auth_headers)
        assert resp.status_code == 200

        # Verify updated
        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        assert resp.get_json()["preferred_bins"][0]["priority"] == 5

    def test_delete_preferred_bin(self, client, auth_headers):
        client.post("/api/admin/preferred-bins", json={"item_id": 1, "bin_id": 2, "priority": 1}, headers=auth_headers)

        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        pb_id = resp.get_json()["preferred_bins"][0]["preferred_bin_id"]

        resp = client.delete(f"/api/admin/preferred-bins/{pb_id}", headers=auth_headers)
        assert resp.status_code == 200

        # Verify deleted
        resp = client.get("/api/admin/preferred-bins?item_id=1", headers=auth_headers)
        assert len(resp.get_json()["preferred_bins"]) == 0
