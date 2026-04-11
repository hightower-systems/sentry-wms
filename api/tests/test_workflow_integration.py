from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


class TestFullReceiveToShipWorkflow:
    def test_full_receive_to_ship_workflow(self, client, auth_headers, seed_data):
        staging_bin = seed_data["staging_bin_id"]

        # 1. Look up PO
        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        po = resp.get_json()
        assert po["purchase_order"]["status"] == "OPEN"

        # 2. Receive all 10 PO-2026-001 lines into staging
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {"item_id": 1, "quantity": 100, "bin_id": staging_bin},
                    {"item_id": 2, "quantity": 100, "bin_id": staging_bin},
                    {"item_id": 3, "quantity": 100, "bin_id": staging_bin},
                    {"item_id": 4, "quantity": 100, "bin_id": staging_bin},
                    {"item_id": 5, "quantity": 50, "bin_id": staging_bin},
                    {"item_id": 6, "quantity": 20, "bin_id": staging_bin},
                    {"item_id": 7, "quantity": 200, "bin_id": staging_bin},
                    {"item_id": 8, "quantity": 30, "bin_id": staging_bin},
                    {"item_id": 9, "quantity": 40, "bin_id": staging_bin},
                    {"item_id": 10, "quantity": 60, "bin_id": staging_bin},
                ],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["po_status"] == "RECEIVED"

        # 3. Check pending put-away
        resp = client.get("/api/putaway/pending/1", headers=auth_headers)
        assert resp.status_code == 200
        pending = resp.get_json()["pending_items"]
        assert len(pending) >= 10

        # 4. Get bin suggestion for item 1
        resp = client.get("/api/putaway/suggest/1", headers=auth_headers)
        assert resp.status_code == 200
        suggestion = resp.get_json()
        to_bin = suggestion["suggested_bin"]["bin_id"]

        # 5. Confirm put-away for item 1
        resp = client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": staging_bin, "to_bin_id": to_bin, "quantity": 100},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Verify inventory moved - item 1 should now have 50 (original) + 100 = 150 in bin 3
        inv_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = %s", (to_bin,)
        )
        assert inv_qty == 150

        # 6. Create pick batch for SO-2026-001
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-2026-001"], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        batch_id = resp.get_json()["batch_id"]

        # 7. Confirm all picks
        while True:
            resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
            data = resp.get_json()
            if "message" in data:
                break
            resp = client.post(
                "/api/picking/confirm",
                json={
                    "pick_task_id": data["pick_task_id"],
                    "scanned_barcode": data["upc"],
                    "quantity_picked": data["quantity_to_pick"],
                },
                headers=auth_headers,
            )
            assert resp.status_code == 200

        # 8. Complete batch
        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # 9. Load order for packing
        resp = client.get("/api/packing/order/SO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        packing_data = resp.get_json()

        # 10. Verify all items
        for line in packing_data["lines"]:
            remaining = line["quantity_picked"] - line["quantity_packed"]
            if remaining > 0:
                resp = client.post(
                    "/api/packing/verify",
                    json={"so_id": 1, "scanned_barcode": line["upc"], "quantity": remaining},
                    headers=auth_headers,
                )
                assert resp.status_code == 200

        # 11. Complete packing
        resp = client.post(
            "/api/packing/complete",
            json={"so_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "PACKED"

        # 12. Ship with tracking number
        resp = client.post(
            "/api/shipping/fulfill",
            json={
                "so_id": 1,
                "tracking_number": "1Z999AA10123456784",
                "carrier": "UPS",
                "ship_method": "GROUND",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # 13. Verify SO is SHIPPED
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "SHIPPED"

        # 14. Verify inventory levels
        # Item 1 started at 150 (after putaway), picked 2 for SO-2026-001 -> 148
        item1_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = %s", (to_bin,)
        )
        assert item1_qty == 148


class TestCycleCountCorrectsInventory:
    def test_cycle_count_corrects_inventory(self, client, auth_headers):
        # 1. Check current inventory for bin 3 (A-01-01)
        original_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = 3"
        )
        assert original_qty == 50

        # 2. Create cycle count
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        count_id = resp.get_json()["counts"][0]["count_id"]

        # Get count details
        resp = client.get(f"/api/inventory/cycle-count/{count_id}", headers=auth_headers)
        lines = resp.get_json()["lines"]

        # 3. Submit count with variance: item 1 should be 55 instead of 50
        item1_line = next(l for l in lines if l["item_id"] == 1)
        submit_lines = [
            {"count_line_id": item1_line["count_line_id"], "counted_quantity": 55}
        ]
        # Submit other lines with exact counts
        for l in lines:
            if l["item_id"] != 1:
                submit_lines.append(
                    {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
                )

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "VARIANCE"

        # 4. Verify inventory was NOT changed (pending audit  -  requires admin approval)
        new_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = 3"
        )
        assert new_qty == 50

        # 5. Verify pending adjustment record exists
        adj = _query_val(
            "SELECT quantity_change FROM inventory_adjustments WHERE item_id = 1 AND bin_id = 3"
        )
        assert adj == 5

        adj_status = _query_val(
            "SELECT status FROM inventory_adjustments WHERE item_id = 1 AND bin_id = 3"
        )
        assert adj_status == "PENDING"


class TestTransferAndPickFromNewLocation:
    def test_transfer_and_pick_from_new_location(self, client, auth_headers):
        # 1. Transfer item 3 (TST-003) from bin 5 (A-01-03) to bin 3 (A-01-01)
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 3, "from_bin_id": 5, "to_bin_id": 3, "quantity": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # 2. Verify item is in new bin
        new_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 3 AND bin_id = 3"
        )
        assert new_qty == 50

        # Original bin should be empty (had 50, moved all 50)
        old_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 3 AND bin_id = 5"
        )
        assert old_qty is None, "Original bin should have no inventory row"

        # 3. Create a pick batch for SO-2026-006 (which needs item 3)
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-2026-006"], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        batch_id = resp.get_json()["batch_id"]
        tasks = resp.get_json()["tasks"]

        # 4. Verify pick task references the new bin (bin 3, A-01-01)
        item3_tasks = [t for t in tasks if t["sku"] == "TST-003"]
        assert len(item3_tasks) > 0
        assert item3_tasks[0]["bin_code"] == "A-01-01", "Should pick from new location"
