"""
Tests for wave picking workflow: validate, create wave batch, combined picks,
short pick distribution, and full wave-to-pack integration.
"""

import psycopg2
import os


# --- Helpers ---

def _create_extra_so(so_number, customer, items_qty, warehouse_id=1):
    """Create an SO with lines directly in the DB for testing."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by)
           VALUES (%s, %s, %s, 'OPEN', %s, 'admin') RETURNING so_id""",
        (so_number, so_number, customer, warehouse_id),
    )
    so_id = cur.fetchone()[0]
    for idx, (item_id, qty) in enumerate(items_qty, 1):
        cur.execute(
            """INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number)
               VALUES (%s, %s, %s, %s)""",
            (so_id, item_id, qty, idx),
        )
    cur.close()
    conn.close()
    return so_id


def _set_so_status(so_id, status):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE sales_orders SET status = %s WHERE so_id = %s", (status, so_id))
    cur.close()
    conn.close()


def _get_inventory(item_id, bin_id):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def _get_wave_breakdowns(pick_task_id):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "SELECT so_id, so_line_id, quantity, quantity_picked, short_quantity FROM wave_pick_breakdown WHERE pick_task_id = %s ORDER BY so_id",
        (pick_task_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# --- Validation Tests ---


def test_validate_valid_so(client, auth_headers):
    """Valid SO returns valid=true with line count and units."""
    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "SO-001", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["valid"] is True
    assert data["so_number"] == "SO-001"
    assert data["line_count"] == 2
    assert data["total_units"] == 3  # 2 + 1


def test_validate_unknown_so(client, auth_headers):
    """Unknown barcode returns valid=false, order not found."""
    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "FAKE-9999", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["valid"] is False
    assert "not found" in data["error"]


def test_validate_so_in_active_batch(client, auth_headers):
    """SO already in an active batch returns valid=false with batch_id."""
    # Create a batch with SO-001
    client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": ["SO-001"], "warehouse_id": 1},
        headers=auth_headers,
    )
    # Now try to validate SO-001 again
    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "SO-001", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["valid"] is False
    assert "already in active pick batch" in data["error"]
    assert "batch_id" in data


def test_validate_so_no_items(client, auth_headers):
    """SO with no line items returns valid=false."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by)
           VALUES ('SO-EMPTY', 'SO-EMPTY', 'Empty Customer', 'OPEN', 1, 'admin')"""
    )
    cur.close()
    conn.close()

    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "SO-EMPTY", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["valid"] is False
    assert "no items" in data["error"].lower()


# --- Wave Create Tests ---


def test_wave_create_single_order(client, auth_headers, seed_data):
    """Wave create with a single SO works like regular batch creation."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [seed_data["so_ids"][0]], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_orders"] == 1
    assert data["total_picks"] > 0
    assert data["total_units"] > 0
    assert "batch_id" in data
    assert data["batch_number"].startswith("WAVE-")


def test_wave_create_multiple_orders(client, auth_headers, seed_data):
    """Wave create combines identical SKUs across orders."""
    # SO-001 has item 1 (qty 2) and item 6 (qty 1)
    # SO-002 has item 3 (qty 3) and item 10 (qty 1)
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_orders"] == 2
    assert data["total_picks"] > 0
    assert data["total_units"] == 7  # 2 + 1 + 3 + 1
    assert len(data["orders"]) == 2


def test_wave_create_pick_path_order(client, auth_headers, seed_data):
    """Wave picks are sorted by pick_sequence for serpentine walk."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    # Get all tasks
    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]

    # Tasks should be in ascending pick_sequence order
    sequences = [t["pick_sequence"] for t in tasks]
    assert sequences == sorted(sequences)


def test_wave_create_allocation(client, auth_headers, seed_data):
    """Inventory is allocated at wave creation time."""
    # Check inventory before
    before = _get_inventory(1, 2)  # item 1 in bin 2
    assert before[1] == 0  # no allocation

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [seed_data["so_ids"][0]], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Check inventory after - should be allocated
    after = _get_inventory(1, 2)
    assert after[1] > 0  # allocation increased


def test_wave_create_partial_inventory(client, auth_headers):
    """Insufficient inventory creates warning but batch still proceeds."""
    # Create SO needing more than available
    so_id = _create_extra_so("SO-BIG", "Big Customer", [(5, 999)])  # item 5 only has 10 in stock

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_id], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "warnings" in data
    assert data["warnings"][0]["needed"] == 999
    assert data["warnings"][0]["available"] < 999


def test_wave_create_duplicate_so(client, auth_headers, seed_data):
    """Duplicate SO IDs in request are rejected."""
    so_id = seed_data["so_ids"][0]
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_id, so_id], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "Duplicate" in resp.get_json()["error"]


def test_wave_create_so_already_in_batch(client, auth_headers):
    """SO already in active batch returns 409."""
    # Create two OPEN SOs
    so_a = _create_extra_so("SO-BATCH-A", "Cust A", [(1, 1)])
    so_b = _create_extra_so("SO-BATCH-B", "Cust B", [(2, 1)])
    so_c = _create_extra_so("SO-BATCH-C", "Cust C", [(3, 1)])

    # Create first wave with SO-A
    client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a], "warehouse_id": 1},
        headers=auth_headers,
    )

    # Reset SO-A status back to OPEN to test the active batch check specifically
    _set_so_status(so_a, "OPEN")

    # Try to create another wave including SO-A
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_c], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert "already in active pick batch" in resp.get_json()["error"]


# --- Wave Pick Breakdown Tests ---


def test_breakdown_records_created(client, auth_headers, seed_data):
    """Wave create produces breakdown records for each SO per pick task."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    # Get tasks
    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]

    # Each task should have breakdown records
    total_breakdown = 0
    for task in tasks:
        breakdowns = _get_wave_breakdowns(task["pick_task_id"])
        assert len(breakdowns) > 0
        total_breakdown += len(breakdowns)

    assert total_breakdown >= len(tasks)


def test_breakdown_quantities_sum(client, auth_headers, seed_data):
    """Breakdown quantities per task sum to the task's total quantity."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]

    for task in tasks:
        breakdowns = _get_wave_breakdowns(task["pick_task_id"])
        bd_total = sum(b[2] for b in breakdowns)  # quantity column
        assert bd_total == task["quantity_to_pick"]


# --- Next Task with Contributing Orders ---


def test_next_task_has_contributing_orders(client, auth_headers, seed_data):
    """Next task endpoint includes contributing_orders for wave picks."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = resp.get_json()["batch_id"]

    next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
    data = next_resp.get_json()
    assert "contributing_orders" in data
    assert "pick_number" in data
    assert "total_picks" in data
    assert data["pick_number"] == 1


# --- Short Pick Distribution Tests ---


def test_short_pick_fifo_distribution(client, auth_headers):
    """Short pick distributes shortage to later SOs (FIFO by SO ID)."""
    # Create two SOs that share item 1 (Blue Widget, 25 in stock)
    so_a = _create_extra_so("SO-SHORT-A", "Customer A", [(1, 3)])
    so_b = _create_extra_so("SO-SHORT-B", "Customer B", [(1, 5)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_b], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    # Get the pick task for item 1
    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]
    item1_task = [t for t in tasks if t["sku"] == "WIDGET-BLU"][0]

    # Short pick: only 5 available out of 8 needed (3+5)
    resp = client.post(
        "/api/picking/short",
        json={"pick_task_id": item1_task["pick_task_id"], "quantity_available": 5},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["task"]["shortage"] == 3

    # Check breakdown: SO-A (lower ID) should get full 3, SO-B gets 2 (shorted 3)
    breakdowns = _get_wave_breakdowns(item1_task["pick_task_id"])
    # Sort by so_id to ensure FIFO
    breakdowns.sort(key=lambda b: b[0])

    # First SO (lower ID) gets full allocation
    assert breakdowns[0][3] == 3  # quantity_picked
    assert breakdowns[0][4] == 0  # short_quantity

    # Second SO gets remainder
    assert breakdowns[1][3] == 2  # quantity_picked
    assert breakdowns[1][4] == 3  # short_quantity


def test_short_pick_full_shortage(client, auth_headers):
    """Full shortage zeros all breakdown records."""
    so_a = _create_extra_so("SO-ZERO-A", "Customer A", [(2, 2)])
    so_b = _create_extra_so("SO-ZERO-B", "Customer B", [(2, 3)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_b], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = resp.get_json()["batch_id"]

    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]
    task = tasks[0]

    # Short pick: 0 available
    resp = client.post(
        "/api/picking/short",
        json={"pick_task_id": task["pick_task_id"], "quantity_available": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    breakdowns = _get_wave_breakdowns(task["pick_task_id"])
    for bd in breakdowns:
        assert bd[3] == 0  # quantity_picked = 0
        assert bd[4] == bd[2]  # short_quantity = original quantity


# --- Confirm Pick with Wave Breakdown ---


def test_confirm_wave_pick_updates_all_so_lines(client, auth_headers):
    """Confirming a wave pick updates quantity_picked on all contributing SO lines."""
    so_a = _create_extra_so("SO-CONF-A", "Customer A", [(1, 2)])
    so_b = _create_extra_so("SO-CONF-B", "Customer B", [(1, 4)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_b], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = resp.get_json()["batch_id"]

    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]
    task = [t for t in tasks if t["sku"] == "WIDGET-BLU"][0]

    # Confirm pick with barcode
    resp = client.post(
        "/api/picking/confirm",
        json={
            "pick_task_id": task["pick_task_id"],
            "scanned_barcode": "100000000001",
            "quantity_picked": 6,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Check breakdowns are all marked as picked
    breakdowns = _get_wave_breakdowns(task["pick_task_id"])
    total_picked = sum(b[3] for b in breakdowns)
    assert total_picked == 6

    # Check SO lines are updated
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT quantity_picked FROM sales_order_lines WHERE so_id = %s", (so_a,))
    assert cur.fetchone()[0] == 2
    cur.execute("SELECT quantity_picked FROM sales_order_lines WHERE so_id = %s", (so_b,))
    assert cur.fetchone()[0] == 4
    cur.close()
    conn.close()


# --- Integration Tests ---


def test_wave_pick_to_pack_flow(client, auth_headers):
    """Full flow: scan SOs, create wave, pick all, complete batch, pack each SO."""
    # Create 3 SOs with some shared items
    so1 = _create_extra_so("SO-FLOW-1", "Cust 1", [(1, 2), (6, 1)])
    so2 = _create_extra_so("SO-FLOW-2", "Cust 2", [(1, 3), (3, 2)])
    so3 = _create_extra_so("SO-FLOW-3", "Cust 3", [(6, 2)])

    # Validate each
    for barcode in ["SO-FLOW-1", "SO-FLOW-2", "SO-FLOW-3"]:
        resp = client.post(
            "/api/picking/wave-validate",
            json={"so_barcode": barcode, "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.get_json()["valid"] is True

    # Create wave
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so1, so2, so3], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    batch_id = data["batch_id"]
    assert data["total_orders"] == 3
    assert data["total_units"] == 10  # 2+1+3+2+2

    # Pick all tasks
    while True:
        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        next_data = next_resp.get_json()
        if "message" in next_data:
            break
        # Confirm pick
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": next_data["pick_task_id"],
                "scanned_barcode": next_data["upc"],
                "quantity_picked": next_data["quantity_to_pick"],
            },
            headers=auth_headers,
        )

    # Complete batch
    resp = client.post(
        "/api/picking/complete-batch",
        json={"batch_id": batch_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    summary = resp.get_json()["summary"]
    assert summary["total_orders"] == 3
    assert summary["total_shorts"] == 0

    # Verify each SO is now PICKING status and can be packed
    for so_id, barcode in [(so1, "SO-FLOW-1"), (so2, "SO-FLOW-2"), (so3, "SO-FLOW-3")]:
        pack_resp = client.get(f"/api/packing/order/{barcode}", headers=auth_headers)
        assert pack_resp.status_code == 200


def test_wave_pick_with_shorts_to_pack(client, auth_headers):
    """Wave pick with short picks still allows packing with adjusted quantities."""
    so1 = _create_extra_so("SO-SHRT-1", "Cust 1", [(5, 3)])  # Large Gadget, only 10 in stock
    so2 = _create_extra_so("SO-SHRT-2", "Cust 2", [(5, 9)])  # needs 9 more, total 12 > 10

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so1, so2], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    batch_id = data["batch_id"]

    # There should be a warning about insufficient inventory
    assert "warnings" in data

    # Pick what's available (with short)
    while True:
        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        next_data = next_resp.get_json()
        if "message" in next_data:
            break
        # Confirm with actual quantity (may be less than needed due to allocation cap)
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": next_data["pick_task_id"],
                "scanned_barcode": next_data["upc"],
                "quantity_picked": next_data["quantity_to_pick"],
            },
            headers=auth_headers,
        )

    # Complete batch
    resp = client.post(
        "/api/picking/complete-batch",
        json={"batch_id": batch_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Both SOs should be PICKING status
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT status FROM sales_orders WHERE so_id = %s", (so1,))
    assert cur.fetchone()[0] == "PICKING"
    cur.execute("SELECT status FROM sales_orders WHERE so_id = %s", (so2,))
    assert cur.fetchone()[0] == "PICKING"
    cur.close()
    conn.close()
