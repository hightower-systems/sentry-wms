"""Regression tests for V-026: existence-oracle IDORs on lookup endpoints.

Before the fix, every lookup endpoint returned three distinct responses --
404 for missing, 403 for wrong-warehouse, and 400 for wrong-status. This
let an attacker enumerate PO/SO/bin IDs across tenants by probing
barcodes and watching response codes. The fix moves the warehouse filter
into the SQL SELECT so a record in another warehouse is indistinguishable
from a record that does not exist (both produce 404).

These tests create a second warehouse with distinct POs, SOs, and bins,
then verify a user assigned only to warehouse 1 cannot tell the
difference between "does not exist" and "exists in warehouse 2".
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from db_test_context import get_raw_connection


def _login_as(client, username, warehouse_ids):
    """Create a non-admin PICKER user assigned only to ``warehouse_ids``
    and return an auth headers dict."""
    conn = get_raw_connection()
    cur = conn.cursor()
    wids = "{" + ",".join(str(w) for w in warehouse_ids) + "}"
    cur.execute(
        """INSERT INTO users (username, password_hash, full_name, role,
               warehouse_id, warehouse_ids, allowed_functions)
           VALUES (%s, '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                   'Scope Test User', 'PICKER', %s, %s,
                   '{pick,receive,count,pack,ship}')""",
        (username, warehouse_ids[0], wids),
    )
    cur.close()
    resp = client.post("/api/auth/login", json={"username": username, "password": "admin"})
    token = resp.get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def warehouse_2_setup():
    """Create warehouse 2 with one PO, one SO, and one bin -- each with a
    distinctive barcode so tests can probe for them."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO warehouses (warehouse_code, warehouse_name) VALUES ('WH-2', 'Second') RETURNING warehouse_id"
    )
    wh2 = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) VALUES (%s, 'W2Z', 'Zone', 'STORAGE') RETURNING zone_id",
        (wh2,),
    )
    z2 = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type) "
        "VALUES (%s, %s, 'W2-BIN-01', 'W2-BIN-01-BC', 'Pickable') RETURNING bin_id",
        (z2, wh2),
    )
    bin2 = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, status, warehouse_id) "
        "VALUES ('PO-W2-UNIQUE', 'PO-W2-UNIQUE', 'V', 'OPEN', %s) RETURNING po_id",
        (wh2,),
    )
    po2 = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, order_date) "
        "VALUES ('SO-W2-UNIQUE', 'SO-W2-UNIQUE', 'C', 'OPEN', %s, NOW()) RETURNING so_id",
        (wh2,),
    )
    so2 = cur.fetchone()[0]
    cur.close()
    return {"warehouse_id": wh2, "bin_id": bin2, "bin_barcode": "W2-BIN-01-BC",
            "po_id": po2, "po_barcode": "PO-W2-UNIQUE",
            "so_id": so2, "so_barcode": "SO-W2-UNIQUE"}


class TestReceivingNoExistenceOracle:
    """lookup_po must not distinguish 'wrong warehouse' from 'not found'."""

    def test_wrong_warehouse_returns_404_not_403(self, client, warehouse_2_setup):
        headers = _login_as(client, "wh1_recv_user", [1])
        resp = client.get(
            f"/api/receiving/po/{warehouse_2_setup['po_barcode']}",
            headers=headers,
        )
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    def test_missing_barcode_returns_same_404(self, client):
        headers = _login_as(client, "wh1_recv_missing", [1])
        resp = client.get(
            "/api/receiving/po/THIS-BARCODE-DOES-NOT-EXIST",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_admin_still_sees_cross_warehouse(self, client, auth_headers, warehouse_2_setup):
        # Admin is not scoped; they retain the ability to look up any PO.
        resp = client.get(
            f"/api/receiving/po/{warehouse_2_setup['po_barcode']}",
            headers=auth_headers,
        )
        assert resp.status_code == 200


class TestPackingNoExistenceOracle:
    def test_wrong_warehouse_returns_404(self, client, warehouse_2_setup):
        headers = _login_as(client, "wh1_pack_user", [1])
        resp = client.get(
            f"/api/packing/order/{warehouse_2_setup['so_barcode']}",
            headers=headers,
        )
        assert resp.status_code == 404


class TestShippingNoExistenceOracle:
    def test_wrong_warehouse_returns_404(self, client, warehouse_2_setup):
        headers = _login_as(client, "wh1_ship_user", [1])
        resp = client.get(
            f"/api/shipping/order/{warehouse_2_setup['so_barcode']}",
            headers=headers,
        )
        assert resp.status_code == 404


class TestLookupNoExistenceOracle:
    def test_bin_in_other_warehouse_returns_404(self, client, warehouse_2_setup):
        headers = _login_as(client, "wh1_bin_user", [1])
        resp = client.get(
            f"/api/lookup/bin/{warehouse_2_setup['bin_barcode']}",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_so_in_other_warehouse_returns_404(self, client, warehouse_2_setup):
        headers = _login_as(client, "wh1_so_user", [1])
        resp = client.get(
            f"/api/lookup/so/{warehouse_2_setup['so_barcode']}",
            headers=headers,
        )
        assert resp.status_code == 404
