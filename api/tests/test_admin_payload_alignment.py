"""Regression lock: admin form payloads match their pydantic schemas (V-017).

v1.4.0's V-017 applies `extra="forbid"` semantics at the validate_body
decorator. If the admin panel ever drifts back to sending fields the
schema does not declare (or omitting fields it requires), the endpoint
starts returning 400 validation_error -- the exact class of bug that
surfaced on every admin create form after v1.4.0 shipped and that
v1.4.2 closed issue-by-issue across #73, #74-81, #82, #83.

This suite exercises each fixed endpoint with the exact shape the admin
frontend now sends after the v1.4.2 fixes. If a future edit to a schema
drops a field, or a frontend change re-introduces an extra key, this
file should fail first.

Scope is alignment only: we assert 2xx status and (where relevant) that
the record is created. We do not re-test the endpoint's business rules;
existing test_admin.py covers those.
"""

import pytest

from db_test_context import get_raw_connection


class TestBinCreatePayload:
    """Issue #74: Bins.jsx saveBin() POST body shape."""

    def test_create_bin_with_aligned_payload(self, client, auth_headers):
        resp = client.post(
            "/api/admin/bins",
            json={
                "bin_code": "ALIGN-BIN-1",
                "bin_barcode": "ALIGN-BIN-1",
                "bin_type": "Pickable",
                "zone_id": 2,
                "aisle": None,
                "pick_sequence": 0,
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["bin_code"] == "ALIGN-BIN-1"

    def test_old_barcode_field_still_rejected(self, client, auth_headers):
        """If the frontend regresses to sending `barcode` instead of
        `bin_barcode`, the schema must keep catching it."""
        resp = client.post(
            "/api/admin/bins",
            json={
                "bin_code": "OLD-BIN",
                "barcode": "OLD-BIN",
                "bin_type": "Pickable",
                "zone_id": 2,
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "validation_error"
        assert any(d["loc"] == ["barcode"] for d in body["details"])


class TestZoneCreatePayload:
    """Issue #75: Zones.jsx save() POST body shape."""

    def test_create_zone_with_aligned_payload(self, client, auth_headers):
        resp = client.post(
            "/api/admin/zones",
            json={
                "zone_code": "ALIGN-Z1",
                "zone_name": "Alignment Zone",
                "zone_type": "STORAGE",
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["zone_code"] == "ALIGN-Z1"

    @pytest.mark.parametrize("zone_type", ["RECEIVING", "STORAGE", "PICKING", "STAGING", "SHIPPING"])
    def test_every_dropdown_zone_type_is_accepted(self, client, auth_headers, zone_type):
        """The admin ZONE_TYPES array must stay a subset of the schema
        validator's VALID_ZONE_TYPES. If someone adds an option that the
        schema rejects, this parametrized test catches it."""
        resp = client.post(
            "/api/admin/zones",
            json={
                "zone_code": f"ALIGN-{zone_type}",
                "zone_name": f"Align {zone_type}",
                "zone_type": zone_type,
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()

    def test_old_is_active_on_create_still_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/zones",
            json={
                "zone_code": "OLD-Z",
                "zone_name": "Old",
                "zone_type": "STORAGE",
                "is_active": True,
                "warehouse_id": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert any(d["loc"] == ["is_active"] for d in resp.get_json()["details"])


class TestZoneEditPayload:
    """Issue #81: Zones.jsx save() PUT body for edit."""

    def test_edit_zone_with_aligned_payload(self, client, auth_headers):
        resp = client.put(
            "/api/admin/zones/1",
            json={
                "zone_code": "RCV-EDIT",
                "zone_name": "Receiving (edited)",
                "zone_type": "RECEIVING",
                "is_active": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["zone_name"] == "Receiving (edited)"


class TestPreferredBinCreatePayload:
    """Issue #76: PreferredBins.jsx saveAdd() POST body shape."""

    def test_create_preferred_bin_with_aligned_payload(self, client, auth_headers):
        resp = client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 5, "bin_id": 2, "priority": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

    def test_null_bin_id_is_rejected(self, client, auth_headers):
        """The #76 bug was sending bin_id: null because the dropdown
        bound to `b.id` (undefined). Lock the schema check here so a
        regression surfaces as a test failure rather than a user
        report."""
        resp = client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 5, "bin_id": None, "priority": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert any(d["loc"] == ["bin_id"] for d in resp.get_json()["details"])


class TestInventoryAdjustmentCreatePayload:
    """Issue #77: Adjustments.jsx handleSubmit() POST body shape."""

    def test_create_adjustment_with_aligned_payload(self, client, auth_headers):
        resp = client.post(
            "/api/admin/adjustments/direct",
            json={
                "warehouse_id": 1,
                "bin_id": 2,
                "item_id": 5,
                "adjustment_type": "add",
                "quantity": 1,
                "reason": "payload alignment test",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()

    def test_empty_reason_still_rejected(self, client, auth_headers):
        """Frontend now refuses to submit with an empty reason; the
        schema still enforces it in case the frontend guard slips."""
        resp = client.post(
            "/api/admin/adjustments/direct",
            json={
                "warehouse_id": 1,
                "bin_id": 2,
                "item_id": 5,
                "adjustment_type": "add",
                "quantity": 1,
                "reason": "",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert any(d["loc"] == ["reason"] for d in resp.get_json()["details"])


class TestInventoryAdjustmentListPayload:
    """#161: GET /admin/adjustments/list must expose the fields the
    Recent Adjustments table reads. Pre-v1.5.1 the endpoint returned
    database-column names (adjusted_at, quantity_change, reason_detail,
    adjusted_by) but did not join items.item_name or users.username, so
    Date / Type / Item / Qty / Reason / User all rendered as '-'.
    """

    def test_list_exposes_every_field_the_recent_table_reads(
        self, client, auth_headers
    ):
        create = client.post(
            "/api/admin/adjustments/direct",
            json={
                "warehouse_id": 1,
                "bin_id": 2,
                "item_id": 5,
                "adjustment_type": "add",
                "quantity": 3,
                "reason": "issue-161 list-payload alignment probe",
            },
            headers=auth_headers,
        )
        assert create.status_code == 201, create.get_json()

        resp = client.get(
            "/api/admin/adjustments/list?warehouse_id=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["adjustments"], "seeded row must come back"
        row = body["adjustments"][0]

        # Every field the admin Recent Adjustments table binds to.
        for key in (
            "adjusted_at",      # Date column
            "quantity_change",  # Qty column + derives Add/Remove tag
            "sku",              # SKU column
            "item_name",        # Item column (JOIN items)
            "bin_code",         # Bin column
            "reason_detail",    # Reason column
            "username",         # User column (JOIN users)
        ):
            assert key in row, f"missing {key!r} for Recent Adjustments table"

        assert row["sku"]
        assert row["item_name"]
        assert row["bin_code"]
        assert row["username"] == "admin"
        assert row["quantity_change"] == 3
        assert row["reason_detail"] == "issue-161 list-payload alignment probe"


class TestInterWarehouseTransferCreatePayload:
    """Issue #78: InterWarehouseTransfers.jsx handleSubmit() POST body shape."""

    def test_transfer_payload_passes_schema(self, client, auth_headers):
        """Send the exact shape the admin UI now sends. This transfer
        expects to hit the `Insufficient inventory` business-rule path
        (the bin is empty); the important assertion is that the payload
        does not trip the schema."""
        resp = client.post(
            "/api/admin/inter-warehouse-transfer",
            json={
                "from_warehouse_id": 1,
                "from_bin_id": 2,
                "to_warehouse_id": 1,
                "to_bin_id": 3,
                "item_id": 5,
                "quantity": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code != 400 or resp.get_json().get("error") != "validation_error", resp.get_json()

    def test_old_source_destination_names_still_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/inter-warehouse-transfer",
            json={
                "source_warehouse_id": 1,
                "source_bin_id": 2,
                "destination_warehouse_id": 1,
                "destination_bin_id": 3,
                "item_id": 5,
                "quantity": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        locs = {tuple(d["loc"]) for d in resp.get_json()["details"]}
        assert ("source_warehouse_id",) in locs
        assert ("source_bin_id",) in locs
        assert ("destination_warehouse_id",) in locs
        assert ("destination_bin_id",) in locs


class TestInterWarehouseTransferListPayload:
    """#162: GET /admin/inter-warehouse-transfers must expose every field
    the Recent Transfers table reads. Pre-fix the backend returned
    from_* / to_* / transferred_at while the frontend was reading
    source_* / destination_* / created_at, so From / To / Status /
    Created all rendered blank.
    """

    def test_list_exposes_every_field_the_recent_table_reads(
        self, client, auth_headers
    ):
        # Seed a row directly; the create endpoint's business rules
        # require matching inventory which isn't worth setting up here.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO bin_transfers (
                item_id, from_bin_id, to_bin_id, warehouse_id,
                quantity, transfer_type, transferred_by, external_id
            ) VALUES (1, 3, 4, 1, 2, 'INTER_WAREHOUSE', 'admin',
                      gen_random_uuid())
            """
        )
        cur.close()

        resp = client.get(
            "/api/admin/inter-warehouse-transfers?limit=10",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["transfers"], "seeded row must come back"
        row = body["transfers"][0]

        # Every field the Recent Transfers table binds to.
        for key in (
            "transfer_id",          # ID column
            "sku",                  # Item column (falls through to item_name/id)
            "item_name",            # Item column fallback
            "quantity",             # Qty column
            "from_warehouse_name",  # From column (primary)
            "from_warehouse_code",  # From column (fallback)
            "from_warehouse_id",    # From column (final fallback)
            "from_bin_code",        # From column (bin side)
            "from_bin_id",
            "to_warehouse_name",
            "to_warehouse_code",
            "to_warehouse_id",
            "to_bin_code",
            "to_bin_id",
            "status",               # Status column tag
            "transferred_at",       # Created column
        ):
            assert key in row, f"missing {key!r} for Recent Transfers table"

        # bin_transfers has no status machine: every row is a completed
        # atomic move, so the server stamps 'completed' for the tag.
        assert row["status"] == "completed"
        assert row["from_warehouse_name"]
        assert row["to_warehouse_name"]
        assert row["from_bin_code"]
        assert row["to_bin_code"]
        assert row["transferred_at"]


class TestManualPOCreatePayload:
    """Issue #79: Settings.jsx createPO() POST body shape."""

    def test_create_po_with_aligned_payload(self, client, auth_headers):
        resp = client.post(
            "/api/admin/purchase-orders",
            json={
                "po_number": "ALIGN-PO-1",
                "warehouse_id": 1,
                "vendor_name": "Align Vendor",
                "notes": None,
                "lines": [{"item_id": 5, "quantity_ordered": 2}],
            },
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201), resp.get_json()

    def test_old_vendor_address_still_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/purchase-orders",
            json={
                "po_number": "ALIGN-PO-2",
                "warehouse_id": 1,
                "vendor_name": "X",
                "vendor_address": "123 Main",
                "lines": [{"item_id": 5, "quantity_ordered": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert any(d["loc"] == ["vendor_address"] for d in resp.get_json()["details"])


class TestManualSOCreatePayload:
    """Issue #80: Settings.jsx createSO() POST body shape."""

    def test_create_so_with_aligned_payload(self, client, auth_headers):
        resp = client.post(
            "/api/admin/sales-orders",
            json={
                "so_number": "ALIGN-SO-1",
                "customer_name": "Align Customer",
                "customer_phone": None,
                "customer_address": "123 Main",
                "ship_address": "123 Main",
                "warehouse_id": 1,
                "lines": [{"item_id": 5, "quantity_ordered": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201), resp.get_json()

    def test_warehouse_id_null_still_rejected(self, client, auth_headers):
        """Guards the #80 `warehouse_id || warehouseId` fallback. If a
        future refactor drops the fallback, a null warehouse_id surfaces
        here before it surfaces for a user."""
        resp = client.post(
            "/api/admin/sales-orders",
            json={
                "so_number": "ALIGN-SO-2",
                "warehouse_id": None,
                "lines": [{"item_id": 5, "quantity_ordered": 1}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert any(d["loc"] == ["warehouse_id"] for d in resp.get_json()["details"])
