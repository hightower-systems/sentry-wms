"""Per-handler emission integration tests (v1.5.0 outbox).

One class per emit site (receipt.completed, adjustment.applied,
transfer.completed, pick.confirmed, pack.confirmed, ship.confirmed,
cycle_count.adjusted). Each drives the real handler via the existing
cookie-auth + SQLAlchemy-savepoint fixture and asserts that the
``integration_events`` row lands with the right envelope shape.

``visible_at`` commit-ordering and concurrency coverage live in
``test_events_migration.py`` (schema-level) and ``test_event_fifo.py``
(to be added in #120). The fixture used here wraps each test in a
rollback'd outer transaction, so the deferred ``visible_at`` trigger
does not fire inside the test window. The tests instead assert the
row lands with ``visible_at IS NULL`` pre-commit; the trigger's
post-commit behaviour is already proven in ``test_events_migration``.
"""

import json
import uuid

from db_test_context import get_raw_connection


def _query_event_rows(source_txn_id: str):
    """Return every integration_events row emitted during this test's
    wrapping transaction with the given source_txn_id.

    Uses the raw test connection (same savepoint-owning connection the
    handler ran against) so visibility inside the outer transaction
    matches what the handler wrote.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT event_id, event_type, event_version, aggregate_type,
               aggregate_id, aggregate_external_id::text, warehouse_id,
               source_txn_id::text, visible_at, payload
          FROM integration_events
         WHERE source_txn_id = %s
         ORDER BY event_id
        """,
        (source_txn_id,),
    )
    cols = [c.name for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    for row in rows:
        # psycopg2 decodes JSONB to dict already; fall back for str case.
        if isinstance(row["payload"], str):
            row["payload"] = json.loads(row["payload"])
    return rows


class TestReceiptCompletedEmission:
    def test_single_item_receive_emits_one_event(self, client, auth_headers, seed_data):
        # Stable X-Request-ID so the test can filter integration_events by
        # source_txn_id rather than scanning every row in the table.
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {
                        "item_id": 1,
                        "quantity": 3,
                        "bin_id": seed_data["staging_bin_id"],
                        "lot_number": "LOT-TEST-112",
                        "serial_number": None,
                    }
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 1, f"expected exactly one receipt.completed row, got {len(rows)}"
        row = rows[0]
        assert row["event_type"] == "receipt.completed"
        assert row["event_version"] == 1
        assert row["aggregate_type"] == "item_receipt"
        assert row["warehouse_id"] == seed_data["warehouse_id"]
        # Aggregate external_id is a UUID string, not the internal receipt_id.
        assert uuid.UUID(row["aggregate_external_id"])
        # Deferred trigger does not fire inside the test fixture's outer
        # transaction, so visible_at stays NULL here. The trigger's
        # COMMIT-time behaviour is proven in test_events_migration.py.
        assert row["visible_at"] is None

    def test_receipt_payload_matches_schema(self, client, auth_headers, seed_data):
        request_id = str(uuid.uuid4())
        client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {
                        "item_id": 1,
                        "quantity": 5,
                        "bin_id": seed_data["staging_bin_id"],
                        "lot_number": "LOT-TEST-112-B",
                        "serial_number": "SN-42",
                    }
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        payload = rows[0]["payload"]

        # Field-by-field checks against docs/events/receipt.completed/1.json.
        assert uuid.UUID(payload["receipt_external_id"])
        assert uuid.UUID(payload["po_external_id"])
        assert uuid.UUID(payload["completed_by_user_external_id"])
        assert payload["completed_at"].endswith("Z") or "+" in payload["completed_at"]

        lines = payload["lines"]
        assert len(lines) == 1  # v1.5.0 ships single-line per receipt row
        line = lines[0]
        assert uuid.UUID(line["item_external_id"])
        assert line["quantity_received"] == 5
        assert line["lot_number"] == "LOT-TEST-112-B"
        assert line["serial_number"] == "SN-42"

    def test_multi_item_receive_emits_one_event_per_receipt_row(
        self, client, auth_headers, seed_data
    ):
        """Sentry creates one item_receipts row per item in the call, so
        the emit site fires one receipt.completed per row. Confirmed by
        counting events after a 3-item receive."""
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {"item_id": 1, "quantity": 2, "bin_id": seed_data["staging_bin_id"]},
                    {"item_id": 2, "quantity": 3, "bin_id": seed_data["staging_bin_id"]},
                    {"item_id": 3, "quantity": 4, "bin_id": seed_data["staging_bin_id"]},
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200
        rows = _query_event_rows(request_id)
        assert len(rows) == 3
        quantities = sorted(r["payload"]["lines"][0]["quantity_received"] for r in rows)
        assert quantities == [2, 3, 4]

    def test_source_txn_id_prefers_x_request_id_header(
        self, client, auth_headers, seed_data
    ):
        """Plan 1.5: X-Request-ID wins over a generated id when it parses
        as a UUID. The handler's emit call must therefore carry the
        header UUID as source_txn_id on the resulting row."""
        request_id = "7c9f4a2a-6fac-4e3b-8c0a-8d2f9d7ab012"
        client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {"item_id": 1, "quantity": 1, "bin_id": seed_data["staging_bin_id"]},
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        assert rows[0]["source_txn_id"] == request_id


def _insert_pending_adjustment(item_id, bin_id, warehouse_id, quantity_change,
                               reason_code="CORRECTION", submitted_by="admin",
                               cycle_count_id=None):
    """Insert a PENDING inventory_adjustments row directly for test setup."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO inventory_adjustments
            (item_id, bin_id, warehouse_id, quantity_change, reason_code,
             status, adjusted_by, cycle_count_id, external_id)
        VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s, gen_random_uuid())
        RETURNING adjustment_id
        """,
        (item_id, bin_id, warehouse_id, quantity_change, reason_code,
         submitted_by, cycle_count_id),
    )
    adj_id = cur.fetchone()[0]
    cur.close()
    return adj_id


class TestAdjustmentAppliedEmission:
    def test_approval_emits_adjustment_applied_with_approver_as_applier(
        self, client, auth_headers, seed_data
    ):
        """Non-cycle-count adjustment: approval emits adjustment.applied
        naming the APPROVER (g.current_user) in applied_by_user_external_id,
        not the submitter."""
        adj_id = _insert_pending_adjustment(
            item_id=1,
            bin_id=seed_data["staging_bin_id"],
            warehouse_id=seed_data["warehouse_id"],
            quantity_change=-3,
            reason_code="DAMAGE",
            submitted_by="some_other_user",  # NOT the approver
        )
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj_id, "action": "approve"}]},
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["approved"] == 1

        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "adjustment.applied"
        assert row["aggregate_type"] == "inventory_adjustment"
        payload = row["payload"]
        assert payload["quantity_delta"] == -3
        assert payload["reason_code"] == "DAMAGE"

        # The applier is the approver (admin), not "some_other_user" (submitter).
        admin_ext = _query_external_id("users", "username", "admin")
        assert payload["applied_by_user_external_id"] == admin_ext

    def test_reject_emits_zero_events(self, client, auth_headers, seed_data):
        """Rejected adjustments must not appear on the outbox."""
        adj_id = _insert_pending_adjustment(
            item_id=1,
            bin_id=seed_data["staging_bin_id"],
            warehouse_id=seed_data["warehouse_id"],
            quantity_change=10,
        )
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj_id, "action": "reject"}]},
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200
        assert resp.get_json()["rejected"] == 1
        assert _query_event_rows(request_id) == []


class TestDirectAdjustmentEmission:
    def test_add_direct_adjustment_emits_adjustment_applied(
        self, client, auth_headers, seed_data
    ):
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/admin/adjustments/direct",
            json={
                "warehouse_id": 1,
                "bin_id": 2,
                "item_id": 5,
                "adjustment_type": "add",
                "quantity": 2,
                "reason": "direct-add emission test",
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 201, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "adjustment.applied"
        assert row["aggregate_type"] == "inventory_adjustment"
        payload = row["payload"]
        assert payload["quantity_delta"] == 2
        assert payload["reason_code"] == "DIRECT_ADJUSTMENT"
        # Admin is both submitter and approver here.
        admin_ext = _query_external_id("users", "username", "admin")
        assert payload["applied_by_user_external_id"] == admin_ext
        assert uuid.UUID(payload["adjustment_external_id"])
        assert uuid.UUID(payload["item_external_id"])
        assert uuid.UUID(payload["bin_external_id"])

    def test_remove_direct_adjustment_emits_negative_delta(
        self, client, auth_headers, seed_data
    ):
        # Seed inventory so the REMOVE branch has enough to subtract from.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) "
            "VALUES (5, 2, 1, 20) ON CONFLICT (item_id, bin_id, lot_number) "
            "DO UPDATE SET quantity_on_hand = EXCLUDED.quantity_on_hand"
        )
        cur.close()

        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/admin/adjustments/direct",
            json={
                "warehouse_id": 1,
                "bin_id": 2,
                "item_id": 5,
                "adjustment_type": "remove",
                "quantity": 3,
                "reason": "direct-remove emission test",
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 201, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "adjustment.applied"
        assert row["payload"]["quantity_delta"] == -3
        assert row["payload"]["reason_code"] == "DIRECT_ADJUSTMENT"


class TestCycleCountAdjustedEmission:
    def _create_variance(self, client, auth_headers):
        """Replicated pattern from test_inventory.TestAdjustmentSelfApproval:
        create a cycle count, submit a count with variance, return the
        pending adjustment_id."""
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        detail_resp = client.get(
            f"/api/inventory/cycle-count/{count_id}", headers=auth_headers
        )
        lines = detail_resp.get_json()["lines"]

        submit_lines = [
            {"count_line_id": lines[0]["count_line_id"],
             "counted_quantity": lines[0]["expected_quantity"] + 5}
        ]
        submit_resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        adj = submit_resp.get_json()["summary"]["adjustments"][0]
        return adj["adjustment_id"]

    def test_approval_emits_cycle_count_adjusted(self, client, auth_headers):
        adj_id = self._create_variance(client, auth_headers)
        # First turn off separation so self-approval goes through in the
        # test fixture without juggling users.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('require_count_approval_separation', 'false') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )
        cur.close()

        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj_id, "action": "approve"}]},
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200, resp.get_json()
        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "cycle_count.adjusted"
        assert row["aggregate_type"] == "inventory_adjustment"
        payload = row["payload"]
        # Payload must carry both the cycle_count external_id (from the
        # cycle_counts join) and the adjusted quantity_delta.
        assert uuid.UUID(payload["cycle_count_external_id"])
        assert uuid.UUID(payload["item_external_id"])
        assert uuid.UUID(payload["bin_external_id"])
        assert payload["counted_quantity"] == payload["system_quantity"] + 5
        assert payload["quantity_delta"] == 5
        assert uuid.UUID(payload["counted_by_user_external_id"])
        assert payload["counted_at"]


def _query_external_id(table, key_column, key_value):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT external_id::text FROM {table} WHERE {key_column} = %s",
        (key_value,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


class TestPickConfirmedEmission:
    def _pick_all_tasks(self, client, auth_headers, batch_id):
        """Replicated from test_picking.TestCompleteBatch: pick or short every
        pending task in the batch. Called before complete_batch so the batch
        has no pending tasks left."""
        while True:
            next_resp = client.get(
                f"/api/picking/batch/{batch_id}/next", headers=auth_headers
            )
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

    def test_complete_batch_emits_one_pick_confirmed_per_so(
        self, client, auth_headers, seed_data
    ):
        """A 2-SO batch produces exactly two pick.confirmed events, each
        with a distinct aggregate_id but all sharing the one source_txn_id
        of the complete-batch call. Same pattern the outbox already uses
        for multi-item receive (see TestReceiptCompletedEmission)."""
        create_resp = client.post(
            "/api/picking/create-batch",
            json={
                "so_identifiers": ["SO-2026-001", "SO-2026-002"],
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 2
        assert all(r["event_type"] == "pick.confirmed" for r in rows)
        assert all(r["aggregate_type"] == "sales_order" for r in rows)
        assert {r["aggregate_id"] for r in rows} == {1, 2}
        assert {r["source_txn_id"] for r in rows} == {request_id}

    def test_pick_confirmed_payload_matches_schema(
        self, client, auth_headers, seed_data
    ):
        create_resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-2026-001"], "warehouse_id": 1},
            headers=auth_headers,
        )
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        request_id = str(uuid.uuid4())
        client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert uuid.UUID(payload["sales_order_external_id"])
        assert uuid.UUID(payload["completed_by_user_external_id"])
        assert payload["completed_at"].endswith("Z") or "+" in payload["completed_at"]

        assert isinstance(payload["lines"], list)
        assert len(payload["lines"]) >= 1
        for line in payload["lines"]:
            assert uuid.UUID(line["item_external_id"])
            assert isinstance(line["quantity_picked"], int)
            # sales_order_lines does not carry lot / serial today.
            assert line["lot_number"] is None
            assert line["serial_number"] is None


class TestTransferCompletedEmission:
    def _seed_source_inventory(self, item_id, bin_id, warehouse_id, quantity):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (item_id, bin_id, lot_number) "
            "DO UPDATE SET quantity_on_hand = EXCLUDED.quantity_on_hand",
            (item_id, bin_id, warehouse_id, quantity),
        )
        cur.close()

    def test_move_emits_transfer_completed(self, client, auth_headers, seed_data):
        # Seed from-bin so the move has inventory to pull from.
        self._seed_source_inventory(
            item_id=1,
            bin_id=seed_data["staging_bin_id"],
            warehouse_id=seed_data["warehouse_id"],
            quantity=15,
        )
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/transfers/move",
            json={
                "item_id": 1,
                "from_bin_id": seed_data["staging_bin_id"],
                "to_bin_id": 3,
                "quantity": 5,
                "reason": "transfer emission test",
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "transfer.completed"
        assert row["aggregate_type"] == "inventory_transfer"
        payload = row["payload"]
        assert uuid.UUID(payload["transfer_external_id"])
        assert payload["from_warehouse_id"] == seed_data["warehouse_id"]
        assert payload["to_warehouse_id"] == seed_data["warehouse_id"]

        # lines[] must be array-shaped even though Sentry is single-line.
        assert isinstance(payload["lines"], list)
        assert len(payload["lines"]) == 1
        line = payload["lines"][0]
        assert uuid.UUID(line["item_external_id"])
        assert line["quantity"] == 5

    def test_putaway_confirm_emits_nothing(self, client, auth_headers, seed_data):
        """Decision K: putaway.confirm is an internal warehouse movement,
        not a connector-visible transfer. The bin_transfers row it writes
        is an audit record; the outbox must stay empty."""
        # Receive 10 into the staging bin first so putaway has something
        # to move.
        receive_request_id = str(uuid.uuid4())
        client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {"item_id": 1, "quantity": 10, "bin_id": seed_data["staging_bin_id"]},
                ],
            },
            headers={**auth_headers, "X-Request-ID": receive_request_id},
        )

        putaway_request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/putaway/confirm",
            json={
                "item_id": 1,
                "from_bin_id": seed_data["staging_bin_id"],
                "to_bin_id": 3,
                "quantity": 10,
            },
            headers={**auth_headers, "X-Request-ID": putaway_request_id},
        )
        assert resp.status_code == 200, resp.get_json()

        # The receive call emits receipt.completed (different source_txn_id);
        # the putaway call emits nothing.
        assert _query_event_rows(putaway_request_id) == []
