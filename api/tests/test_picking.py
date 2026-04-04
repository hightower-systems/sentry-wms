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


def _query_val(sql, params=None):
    row = _query_one(sql, params)
    return row[0] if row else None


def _create_batch(client, auth_headers, so_ids=None):
    """Create a pick batch for the given SOs (default: SO-001 and SO-002)."""
    identifiers = so_ids or ["SO-001", "SO-002"]
    resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": identifiers, "warehouse_id": 1},
        headers=auth_headers,
    )
    return resp


class TestCreateBatch:
    def test_create_batch_success(self, client, auth_headers):
        resp = _create_batch(client, auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["batch_id"] is not None
        assert data["batch_number"].startswith("BATCH-")
        assert data["total_orders"] == 2
        assert len(data["tasks"]) > 0

    def test_create_batch_pick_path_order(self, client, auth_headers):
        resp = _create_batch(client, auth_headers)
        data = resp.get_json()
        sequences = [t["pick_sequence"] for t in data["tasks"]]
        assert sequences == sorted(sequences), "Tasks should be sorted by pick_sequence"

    def test_create_batch_allocates_inventory(self, client, auth_headers):
        _create_batch(client, auth_headers)
        # Item 1 (WIDGET-BLU) in bin 2 should have quantity_allocated > 0
        row = _query_one(
            "SELECT quantity_allocated FROM inventory WHERE item_id = 1 AND bin_id = 2"
        )
        assert row[0] > 0, "Inventory should be allocated after batch creation"

    def test_create_batch_updates_so_status(self, client, auth_headers):
        _create_batch(client, auth_headers)
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "ALLOCATED"

    def test_create_batch_assigns_totes(self, client, auth_headers):
        resp = _create_batch(client, auth_headers)
        data = resp.get_json()
        totes = [o["tote_number"] for o in data["orders"]]
        assert "TOTE-1" in totes
        assert "TOTE-2" in totes

    def test_create_batch_invalid_so(self, client, auth_headers):
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-FAKE"], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_batch_already_allocated_so(self, client, auth_headers):
        # First batch allocates the SOs
        _create_batch(client, auth_headers)
        # Second attempt should fail because SOs are now ALLOCATED, not OPEN
        resp = _create_batch(client, auth_headers)
        assert resp.status_code == 400


class TestGetBatch:
    def test_get_batch_returns_tasks_in_order(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        sequences = [t["pick_sequence"] for t in data["tasks"]]
        assert sequences == sorted(sequences)

    def test_get_batch_not_found(self, client, auth_headers):
        resp = client.get("/api/picking/batch/9999", headers=auth_headers)
        assert resp.status_code == 404


class TestNextTask:
    def test_get_next_task(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "pick_task_id" in data
        assert data["status"] == "PENDING"


class TestConfirmPick:
    def test_confirm_pick_success(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["task"]["status"] == "PICKED"

    def test_confirm_pick_wrong_barcode(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": "WRONG-BARCODE",
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Wrong item" in resp.get_json()["error"]

    def test_confirm_pick_already_picked(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        # Pick it once
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )

        # Try to pick again
        resp = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "already" in resp.get_json()["error"].lower()

    def test_confirm_pick_updates_so_line(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )

        # Check so_line quantity_picked increased
        so_line_id = _query_val(
            "SELECT so_line_id FROM pick_tasks WHERE pick_task_id = %s",
            (task["pick_task_id"],),
        )
        qty_picked = _query_val(
            "SELECT quantity_picked FROM sales_order_lines WHERE so_line_id = %s",
            (so_line_id,),
        )
        assert qty_picked > 0


class TestShortPick:
    def test_short_pick_success(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/short",
            json={"pick_task_id": task["pick_task_id"], "quantity_available": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["task"]["status"] == "SHORT"
        assert data["task"]["shortage"] > 0

    def test_short_pick_zero_available(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/short",
            json={"pick_task_id": task["pick_task_id"], "quantity_available": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["task"]["quantity_picked"] == 0


class TestCompleteBatch:
    def _pick_all_tasks(self, client, auth_headers, batch_id):
        """Pick or short all pending tasks in a batch."""
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

    def test_complete_batch_success(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["batch_number"] is not None

        # Check SOs moved to PICKING
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "PICKING"

    def test_complete_batch_with_pending_tasks(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        # Don't pick any tasks, try to complete
        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "pending" in resp.get_json()["error"].lower()

    def test_complete_batch_not_found(self, client, auth_headers):
        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": 9999},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_get_next_task_all_complete(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        assert resp.status_code == 200
        assert "message" in resp.get_json()
        assert "complete" in resp.get_json()["message"].lower()

    def test_picking_requires_auth(self, client):
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-001"], "warehouse_id": 1},
        )
        assert resp.status_code == 401


# --- Zone/Aisle Conditional Display Tests ---


def test_next_pick_with_zone_and_aisle(client, auth_headers):
    """Bin with zone and aisle returns both in response."""
    batch = _create_batch(client, auth_headers).get_json()
    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] is not None
    # Seed bins have zones (Storage Shelves) and aisles (A, B)
    assert data["zone"] is not None
    assert data["aisle"] is not None


def test_next_pick_with_zone_no_aisle(client, auth_headers):
    """Bin with zone but no aisle returns zone and aisle=null."""
    # The staging bin (RCV-01) has zone (Receiving Area) but no aisle
    # Create an SO that needs an item in a staging-like location
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    # Put some item 1 inventory in the staging bin (bin_id=1, zone=Receiving, no aisle)
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES (1, 1, 1, 50)"
    )
    # Remove item 1 from all non-staging bins so it must pick from staging
    cur.execute("DELETE FROM inventory WHERE item_id = 1 AND bin_id != 1")
    # Change staging bin type so it's pickable
    cur.execute("UPDATE bins SET bin_type = 'STANDARD' WHERE bin_id = 1")
    cur.close()
    conn.close()

    # Create SO for item 1
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by)
           VALUES ('SO-NOAISLE', 'SO-NOAISLE', 'Cust', 'OPEN', 1, 'admin') RETURNING so_id"""
    )
    so_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (%s, 1, 1, 1)",
        (so_id,),
    )
    cur.close()
    conn.close()

    batch_resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": ["SO-NOAISLE"], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch = batch_resp.get_json()

    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] == "RCV-01"
    assert data["zone"] == "Receiving Area"
    assert data["aisle"] is None


def test_next_pick_no_zone(client, auth_headers):
    """Bin with no zone returns zone=null and aisle=null."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    # Create a bin with no zone (zone_id references are NOT NULL in schema,
    # so we create a bin in an existing zone then set zone_id via direct update)
    # Actually zone_id is NOT NULL, so we need to create the bin properly
    # and then check. The schema requires zone_id, so a "no zone" bin
    # would need a schema change. Instead, test that zone_name=null
    # when the zone record is somehow missing. We can simulate by
    # creating a bin with a zone that has no name... or we can check
    # that the LEFT JOIN handles it.
    # Since zone_id is NOT NULL in schema, we test the null-coercion
    # on empty strings: create a zone with empty name to verify `or None`.
    cur.execute(
        """INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type)
           VALUES (1, 'NONAME', '', 'STORAGE') RETURNING zone_id"""
    )
    zone_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, pick_sequence, putaway_sequence)
           VALUES (%s, 1, 'NOZONE-01', 'BIN-NOZONE-01', 'STANDARD', 50, 50) RETURNING bin_id""",
        (zone_id,),
    )
    bin_id = cur.fetchone()[0]
    # Put inventory in this bin
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES (1, %s, 1, 100)",
        (bin_id,),
    )
    # Remove item 1 from all other bins
    cur.execute("DELETE FROM inventory WHERE item_id = 1 AND bin_id != %s", (bin_id,))
    cur.close()
    conn.close()

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by)
           VALUES ('SO-NOZONE', 'SO-NOZONE', 'Cust', 'OPEN', 1, 'admin') RETURNING so_id"""
    )
    so_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (%s, 1, 1, 1)",
        (so_id,),
    )
    cur.close()
    conn.close()

    batch_resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": ["SO-NOZONE"], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch = batch_resp.get_json()

    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] == "NOZONE-01"
    # Empty string zone_name should be coerced to null
    assert data["zone"] is None
    assert data["aisle"] is None


def test_next_pick_bin_code_always_present(client, auth_headers):
    """bin_code is never null regardless of zone/aisle state."""
    batch = _create_batch(client, auth_headers).get_json()
    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] is not None
    assert isinstance(data["bin_code"], str)
    assert len(data["bin_code"]) > 0
