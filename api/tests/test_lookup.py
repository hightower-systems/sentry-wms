class TestItemLookup:
    def test_lookup_item_by_upc(self, client, auth_headers):
        resp = client.get("/api/lookup/item/100000000001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["item"]["sku"] == "TST-001"
        assert data["item"]["upc"] == "100000000001"
        assert len(data["locations"]) >= 1, "Item should have at least one location"

    def test_lookup_item_not_found(self, client, auth_headers):
        resp = client.get("/api/lookup/item/999999999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_lookup_item_returns_location_details(self, client, auth_headers):
        resp = client.get("/api/lookup/item/100000000001", headers=auth_headers)
        data = resp.get_json()
        loc = data["locations"][0]
        assert "bin_id" in loc
        assert "bin_code" in loc
        assert "bin_type" in loc
        assert "quantity_on_hand" in loc
        assert "quantity_available" in loc
        assert "lot_number" in loc
        assert loc["quantity_on_hand"] == 50, "TST-001 should have 50 in bin A-01-01"
        assert loc["bin_type"] == "Pickable"

    def test_lookup_requires_auth(self, client):
        resp = client.get("/api/lookup/item/100000000001")
        assert resp.status_code == 401


class TestBinLookup:
    def test_lookup_bin_by_barcode(self, client, auth_headers):
        resp = client.get("/api/lookup/bin/A-01-01", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["bin"]["bin_code"] == "A-01-01"
        assert data["bin"]["bin_type"] == "Pickable"
        assert len(data["items"]) >= 1, "Bin A-01-01 should contain items"

    def test_lookup_bin_not_found(self, client, auth_headers):
        resp = client.get("/api/lookup/bin/BIN-FAKE-99", headers=auth_headers)
        assert resp.status_code == 404

    def test_lookup_bin_with_multiple_items(self, client, auth_headers):
        # Bin A-01-01 (id=3) has item 1 (TST-001, qty 50) and item 11 (TST-011, qty 12)
        resp = client.get("/api/lookup/bin/A-01-01", headers=auth_headers)
        data = resp.get_json()
        assert len(data["items"]) == 2, "Bin A-01-01 should have 2 items"
        skus = {i["sku"] for i in data["items"]}
        assert "TST-001" in skus
        assert "TST-011" in skus


class TestItemSearch:
    def test_search_items_by_sku(self, client, auth_headers):
        resp = client.get("/api/lookup/item/search?q=TST-00", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 9, "Should find 9 TST-00x items"

    def test_search_items_by_name(self, client, auth_headers):
        resp = client.get("/api/lookup/item/search?q=nymph", headers=auth_headers)
        data = resp.get_json()
        assert len(data) >= 2, "Case-insensitive search should find nymph flies"

    def test_search_items_no_results(self, client, auth_headers):
        resp = client.get("/api/lookup/item/search?q=zzzznonexistent", headers=auth_headers)
        data = resp.get_json()
        assert data == []

    def test_search_items_empty_query(self, client, auth_headers):
        resp = client.get("/api/lookup/item/search?q=", headers=auth_headers)
        data = resp.get_json()
        assert data == []


class TestBinSearch:
    def test_search_bins_by_code(self, client, auth_headers):
        resp = client.get("/api/lookup/bin/search?q=A-01", headers=auth_headers)
        data = resp.get_json()
        assert len(data) == 3, "Should find 3 bins A-01-01, A-01-02, A-01-03"

    def test_search_bins_no_results(self, client, auth_headers):
        resp = client.get("/api/lookup/bin/search?q=ZZZZZ", headers=auth_headers)
        data = resp.get_json()
        assert data == []
