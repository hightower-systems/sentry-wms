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


def _receive_to_staging(client, auth_headers, item_id=1, quantity=10, bin_id=1):
    """Helper: receive items into staging bin so we can test putaway."""
    client.post(
        "/api/receiving/receive",
        json={"po_id": 1, "items": [{"item_id": item_id, "quantity": quantity, "bin_id": bin_id}]},
        headers=auth_headers,
    )


class TestPendingPutaway:
    def test_pending_items_shows_staging_inventory(self, client, auth_headers):
        _receive_to_staging(client, auth_headers, item_id=1, quantity=10)

        resp = client.get("/api/putaway/pending/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["pending_items"]) >= 1, "Should show items in staging"
        skus = [p["sku"] for p in data["pending_items"]]
        assert "WIDGET-BLU" in skus

    def test_pending_items_empty_when_no_staging(self, client, auth_headers):
        # Fresh seed has no items in staging
        resp = client.get("/api/putaway/pending/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["pending_items"]) == 0


class TestBinSuggestion:
    def test_suggest_returns_default_bin(self, client, auth_headers):
        # Item 1 (WIDGET-BLU) has default_bin_id = 2 (A-01-01)
        resp = client.get("/api/putaway/suggest/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["suggested_bin"] is not None
        assert data["suggested_bin"]["bin_id"] == 2
        assert data["suggested_bin"]["reason"] == "Default bin assignment"

    def test_suggest_returns_existing_stock_bin(self, client, auth_headers):
        # Remove default_bin_id from item 1, so suggestion falls back to existing stock
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("UPDATE items SET default_bin_id = NULL WHERE item_id = 1")
        cur.close()
        conn.close()

        resp = client.get("/api/putaway/suggest/1", headers=auth_headers)
        data = resp.get_json()
        assert data["suggested_bin"] is not None
        assert "Existing stock" in data["suggested_bin"]["reason"]

    def test_suggest_item_not_found(self, client, auth_headers):
        resp = client.get("/api/putaway/suggest/9999", headers=auth_headers)
        assert resp.status_code == 404


class TestConfirmPutaway:
    def test_confirm_putaway_success(self, client, auth_headers):
        _receive_to_staging(client, auth_headers, item_id=1, quantity=10)

        resp = client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 2, "quantity": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["transfer_id"] is not None
        assert data["quantity"] == 10

    def test_confirm_putaway_creates_transfer_record(self, client, auth_headers):
        _receive_to_staging(client, auth_headers, item_id=1, quantity=10)
        client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 2, "quantity": 10},
            headers=auth_headers,
        )

        row = _query_one(
            "SELECT transfer_type FROM bin_transfers WHERE item_id = 1 AND transfer_type = 'PUTAWAY'"
        )
        assert row is not None, "bin_transfers record should exist"
        assert row[0] == "PUTAWAY"

    def test_confirm_putaway_creates_audit_log(self, client, auth_headers):
        _receive_to_staging(client, auth_headers, item_id=1, quantity=10)
        client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 2, "quantity": 10},
            headers=auth_headers,
        )

        row = _query_one("SELECT log_id FROM audit_log WHERE action_type = 'PUTAWAY'")
        assert row is not None

    def test_confirm_insufficient_quantity(self, client, auth_headers):
        _receive_to_staging(client, auth_headers, item_id=1, quantity=5)

        resp = client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 2, "quantity": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Insufficient" in resp.get_json()["error"]

    def test_confirm_same_bin(self, client, auth_headers):
        resp = client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 1, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "different" in resp.get_json()["error"]

    def test_confirm_invalid_item(self, client, auth_headers):
        resp = client.post(
            "/api/putaway/confirm",
            json={"item_id": 9999, "from_bin_id": 1, "to_bin_id": 2, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_putaway_requires_auth(self, client):
        resp = client.get("/api/putaway/pending/1")
        assert resp.status_code == 401
