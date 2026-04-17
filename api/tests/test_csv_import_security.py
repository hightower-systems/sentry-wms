"""V-015 regression tests for the /api/admin/import/<type> endpoint.

Before the fix, CSV rows bypassed pydantic entirely: formula-injection
prefixes were stored as-is, non-numeric values passed to int() crashed
the whole import with a 500, and nothing enforced per-field length or
type. The fix runs each row through a per-entity pydantic schema and
rejects formula-injection prefixes on every text field.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


class TestCsvImportPydantic:
    def test_formula_prefix_rejected_on_sku(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/items",
            json={"records": [{"sku": "=CMD|'/c calc'!A1", "name": "X"}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 0
        assert data["skipped"] == 1
        assert "formula" in data["errors"][0]["error"].lower()

    def test_formula_prefix_rejected_on_item_name(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/items",
            json={"records": [{"sku": "TEST-FORMULA-1", "item_name": "+attack"}]},
            headers=auth_headers,
        )
        data = resp.get_json()
        assert data["imported"] == 0
        assert data["skipped"] == 1

    def test_formula_prefix_rejected_on_customer(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/sales-orders",
            json={
                "warehouse_id": 1,
                "records": [{
                    "so_number": "SO-V015-1", "sku": "TST-001",
                    "quantity": 1, "customer": "-bad-start"
                }],
            },
            headers=auth_headers,
        )
        data = resp.get_json()
        assert data["imported"] == 0
        assert data["skipped"] == 1

    def test_non_numeric_quantity_skips_row_not_500(self, client, auth_headers):
        """Previously `int("abc")` bubbled up and crashed the entire
        import. Now pydantic rejects the row and the import continues."""
        resp = client.post(
            "/api/admin/import/items",
            json={
                "records": [
                    {"sku": "V015-GOOD", "name": "Good", "quantity": "abc"},
                    {"sku": "V015-OK", "name": "Ok"},
                ]
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["skipped"] == 1
        # The second row must have imported despite the first being bad.
        assert data["imported"] == 1

    def test_valid_rows_still_import(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/items",
            json={
                "records": [
                    {"sku": "V015-VALID-1", "name": "Legitimate item"},
                    {"sku": "V015-VALID-2", "name": "Another legit"},
                ]
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 2
        assert data["skipped"] == 0

    def test_unsupported_entity_type(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/accounts",
            json={"records": [{"foo": "bar"}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_over_5000_records_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/items",
            json={"records": [{"sku": f"X{i}", "name": "x"} for i in range(5001)]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "5000" in resp.get_json()["error"]

    def test_missing_records_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/import/items", json={}, headers=auth_headers
        )
        assert resp.status_code == 400

    def test_tab_prefix_rejected(self, client, auth_headers):
        # Tab is a legacy formula-injection vector in some spreadsheets.
        resp = client.post(
            "/api/admin/import/items",
            json={"records": [{"sku": "\tTAB-LEADER", "name": "X"}]},
            headers=auth_headers,
        )
        data = resp.get_json()
        assert data["imported"] == 0
        assert data["skipped"] == 1
