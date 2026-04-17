"""Tests for pydantic validation schemas and the @validate_body decorator.

Covers: valid input, missing required fields, invalid types, boundary
conditions, and integration tests verifying 400 on invalid / 200 on valid.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pydantic import ValidationError

from schemas.auth import LoginRequest, ChangePasswordRequest
from schemas.receiving import ReceiveItemsRequest, ReceiveItemEntry, CancelReceivingRequest
from schemas.putaway import ConfirmPutawayRequest, UpdatePreferredRequest
from schemas.pick_walks import (
    CreateBatchRequest, WaveValidateRequest, ConfirmPickRequest,
    ShortPickRequest, CompleteBatchRequest, CancelBatchRequest,
)
from schemas.pack_verification import VerifyPackItemRequest, CompletePackingRequest
from schemas.shipping import FulfillRequest
from schemas.cycle_count import CreateCycleCountRequest, SubmitCycleCountRequest, CycleCountLineEntry
from schemas.bin_transfer import MoveRequest
from schemas.items import CreateItemRequest, UpdateItemRequest, CreatePreferredBinRequest
from schemas.purchase_orders import CreatePurchaseOrderRequest, POLineEntry
from schemas.sales_orders import CreateSalesOrderRequest, SOLineEntry
from schemas.users import CreateUserRequest, UpdateUserRequest
from schemas.warehouses import CreateWarehouseRequest, InterWarehouseTransferRequest
from schemas.zones import CreateZoneRequest
from schemas.bins import CreateBinRequest
from schemas.settings import UpdateSettingsRequest
from schemas.inventory_adjustments import DirectAdjustmentRequest, ReviewAdjustmentsRequest, AdjustmentDecision

from db_test_context import get_raw_connection


def _reset_lockout():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM login_attempts")
    cur.close()


# ---------------------------------------------------------------------------
# Unit tests - schema validation without hitting the server
# ---------------------------------------------------------------------------


class TestAuthSchemas:
    def test_login_valid(self):
        req = LoginRequest(username="admin", password="secret")
        assert req.username == "admin"

    def test_login_missing_username(self):
        with pytest.raises(ValidationError):
            LoginRequest(password="secret")

    def test_login_missing_password(self):
        with pytest.raises(ValidationError):
            LoginRequest(username="admin")

    def test_login_empty_username(self):
        with pytest.raises(ValidationError):
            LoginRequest(username="", password="secret")

    def test_login_strips_username(self):
        req = LoginRequest(username="  admin  ", password="secret")
        assert req.username == "admin"

    def test_change_password_valid(self):
        req = ChangePasswordRequest(current_password="old", new_password="new")
        assert req.new_password == "new"

    def test_change_password_missing_fields(self):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="old")


class TestReceivingSchemas:
    def test_receive_valid(self):
        req = ReceiveItemsRequest(
            po_id=1,
            items=[{"item_id": 1, "quantity": 5, "bin_id": 1}],
        )
        assert req.po_id == 1
        assert len(req.items) == 1

    def test_receive_missing_po_id(self):
        with pytest.raises(ValidationError):
            ReceiveItemsRequest(items=[{"item_id": 1, "quantity": 5, "bin_id": 1}])

    def test_receive_empty_items(self):
        with pytest.raises(ValidationError):
            ReceiveItemsRequest(po_id=1, items=[])

    def test_receive_zero_quantity(self):
        with pytest.raises(ValidationError):
            ReceiveItemsRequest(po_id=1, items=[{"item_id": 1, "quantity": 0, "bin_id": 1}])

    def test_receive_negative_po_id(self):
        with pytest.raises(ValidationError):
            ReceiveItemsRequest(po_id=-1, items=[{"item_id": 1, "quantity": 1, "bin_id": 1}])

    def test_cancel_defaults(self):
        req = CancelReceivingRequest()
        assert req.receipt_ids == []
        assert req.po_id is None


class TestPutawaySchemas:
    def test_confirm_valid(self):
        req = ConfirmPutawayRequest(item_id=1, from_bin_id=1, to_bin_id=2, quantity=10)
        assert req.quantity == 10

    def test_confirm_same_bins_rejected(self):
        with pytest.raises(ValidationError):
            ConfirmPutawayRequest(item_id=1, from_bin_id=1, to_bin_id=1, quantity=10)

    def test_confirm_zero_quantity(self):
        with pytest.raises(ValidationError):
            ConfirmPutawayRequest(item_id=1, from_bin_id=1, to_bin_id=2, quantity=0)

    def test_update_preferred_defaults(self):
        req = UpdatePreferredRequest(item_id=1, bin_id=2)
        assert req.set_as_primary is True


class TestPickingSchemas:
    def test_create_batch_valid(self):
        req = CreateBatchRequest(so_identifiers=["SO-001"], warehouse_id=1)
        assert len(req.so_identifiers) == 1

    def test_create_batch_empty_identifiers(self):
        with pytest.raises(ValidationError):
            CreateBatchRequest(so_identifiers=[], warehouse_id=1)

    def test_confirm_pick_valid(self):
        req = ConfirmPickRequest(pick_task_id=1, scanned_barcode="ABC", quantity_picked=5)
        assert req.quantity_picked == 5

    def test_confirm_pick_zero_quantity(self):
        with pytest.raises(ValidationError):
            ConfirmPickRequest(pick_task_id=1, scanned_barcode="ABC", quantity_picked=0)

    def test_short_pick_defaults(self):
        req = ShortPickRequest(pick_task_id=1)
        assert req.quantity_available == 0

    def test_short_pick_negative(self):
        with pytest.raises(ValidationError):
            ShortPickRequest(pick_task_id=1, quantity_available=-1)


class TestPackingSchemas:
    def test_verify_valid(self):
        req = VerifyPackItemRequest(so_id=1, scanned_barcode="ABC")
        assert req.quantity == 1  # default

    def test_verify_zero_quantity(self):
        with pytest.raises(ValidationError):
            VerifyPackItemRequest(so_id=1, scanned_barcode="ABC", quantity=0)


class TestShippingSchemas:
    def test_fulfill_valid(self):
        req = FulfillRequest(so_id=1, tracking_number="TRACK123", carrier="UPS")
        assert req.carrier == "UPS"

    def test_fulfill_strips_whitespace(self):
        req = FulfillRequest(so_id=1, tracking_number="  TRACK  ", carrier="  UPS  ")
        assert req.tracking_number == "TRACK"
        assert req.carrier == "UPS"

    def test_fulfill_blank_carrier(self):
        with pytest.raises(ValidationError):
            FulfillRequest(so_id=1, tracking_number="TRACK", carrier="   ")

    def test_fulfill_too_long_carrier(self):
        with pytest.raises(ValidationError):
            FulfillRequest(so_id=1, tracking_number="T", carrier="X" * 101)


class TestCycleCountSchemas:
    def test_create_valid(self):
        req = CreateCycleCountRequest(warehouse_id=1, bin_ids=[1, 2, 3])
        assert len(req.bin_ids) == 3

    def test_create_empty_bins(self):
        with pytest.raises(ValidationError):
            CreateCycleCountRequest(warehouse_id=1, bin_ids=[])

    def test_submit_valid(self):
        req = SubmitCycleCountRequest(
            count_id=1,
            lines=[{"count_line_id": 1, "counted_quantity": 5}],
        )
        assert req.lines[0].counted_quantity == 5

    def test_submit_negative_count(self):
        with pytest.raises(ValidationError):
            SubmitCycleCountRequest(
                count_id=1,
                lines=[{"count_line_id": 1, "counted_quantity": -1}],
            )


class TestTransferSchemas:
    def test_move_valid(self):
        req = MoveRequest(item_id=1, from_bin_id=1, to_bin_id=2, quantity=10)
        assert req.quantity == 10

    def test_move_same_bins(self):
        with pytest.raises(ValidationError):
            MoveRequest(item_id=1, from_bin_id=1, to_bin_id=1, quantity=10)


class TestItemSchemas:
    def test_create_valid(self):
        req = CreateItemRequest(sku="TEST-001", item_name="Test Item")
        assert req.sku == "TEST-001"

    def test_create_missing_sku(self):
        with pytest.raises(ValidationError):
            CreateItemRequest(item_name="Test Item")

    def test_update_excludes_unset(self):
        req = UpdateItemRequest(sku="NEW-SKU")
        dumped = req.model_dump(exclude_unset=True)
        assert "sku" in dumped
        assert "item_name" not in dumped

    def test_create_preferred_bin_valid(self):
        req = CreatePreferredBinRequest(item_id=1, bin_id=2)
        assert req.priority == 1


class TestOrderSchemas:
    def test_create_po_valid(self):
        req = CreatePurchaseOrderRequest(
            po_number="PO-001",
            warehouse_id=1,
            lines=[{"item_id": 1, "quantity_ordered": 10}],
        )
        assert len(req.lines) == 1

    def test_create_po_zero_quantity(self):
        with pytest.raises(ValidationError):
            CreatePurchaseOrderRequest(
                po_number="PO-001",
                warehouse_id=1,
                lines=[{"item_id": 1, "quantity_ordered": 0}],
            )

    def test_create_so_valid(self):
        req = CreateSalesOrderRequest(
            so_number="SO-001",
            warehouse_id=1,
            lines=[{"item_id": 1, "quantity_ordered": 5}],
        )
        assert req.so_number == "SO-001"


class TestUserSchemas:
    def test_create_valid(self):
        req = CreateUserRequest(
            username="testuser", password="testpass", full_name="Test User", role="USER",
        )
        assert req.role == "USER"

    def test_create_invalid_role(self):
        with pytest.raises(ValidationError):
            CreateUserRequest(
                username="testuser", password="testpass", full_name="Test", role="SUPERADMIN",
            )

    def test_update_partial(self):
        req = UpdateUserRequest(full_name="New Name")
        dumped = req.model_dump(exclude_unset=True)
        assert dumped == {"full_name": "New Name"}


class TestWarehouseSchemas:
    def test_create_valid(self):
        req = CreateWarehouseRequest(warehouse_code="WH1", warehouse_name="Main")
        assert req.warehouse_code == "WH1"

    def test_create_missing_code(self):
        with pytest.raises(ValidationError):
            CreateWarehouseRequest(warehouse_name="Main")

    def test_inter_warehouse_valid(self):
        req = InterWarehouseTransferRequest(
            item_id=1, from_bin_id=1, from_warehouse_id=1,
            to_bin_id=2, to_warehouse_id=2, quantity=5,
        )
        assert req.quantity == 5


class TestZoneSchemas:
    def test_create_valid(self):
        req = CreateZoneRequest(
            warehouse_id=1, zone_code="Z1", zone_name="Zone 1", zone_type="STORAGE",
        )
        assert req.zone_type == "STORAGE"

    def test_create_invalid_type(self):
        with pytest.raises(ValidationError):
            CreateZoneRequest(
                warehouse_id=1, zone_code="Z1", zone_name="Zone 1", zone_type="INVALID",
            )


class TestBinSchemas:
    def test_create_valid(self):
        req = CreateBinRequest(
            zone_id=1, warehouse_id=1, bin_code="A-01-01",
            bin_barcode="A-01-01", bin_type="Pickable",
        )
        assert req.bin_type == "Pickable"

    def test_create_invalid_type(self):
        with pytest.raises(ValidationError):
            CreateBinRequest(
                zone_id=1, warehouse_id=1, bin_code="A-01-01",
                bin_barcode="A-01-01", bin_type="InvalidType",
            )


class TestSettingsSchemas:
    def test_valid(self):
        req = UpdateSettingsRequest(settings={"require_packing_before_shipping": "true"})
        assert req.settings["require_packing_before_shipping"] == "true"

    def test_empty_settings(self):
        with pytest.raises(ValidationError):
            UpdateSettingsRequest(settings={})


class TestAdjustmentSchemas:
    def test_direct_valid(self):
        req = DirectAdjustmentRequest(
            item_id=1, bin_id=1, warehouse_id=1,
            adjustment_type="ADD", quantity=10, reason="Stock correction",
        )
        assert req.adjustment_type == "ADD"

    def test_direct_normalizes_case(self):
        req = DirectAdjustmentRequest(
            item_id=1, bin_id=1, warehouse_id=1,
            adjustment_type="add", quantity=10, reason="test",
        )
        assert req.adjustment_type == "ADD"

    def test_direct_invalid_type(self):
        with pytest.raises(ValidationError):
            DirectAdjustmentRequest(
                item_id=1, bin_id=1, warehouse_id=1,
                adjustment_type="DESTROY", quantity=10, reason="test",
            )

    def test_review_valid(self):
        req = ReviewAdjustmentsRequest(
            decisions=[{"adjustment_id": 1, "action": "approve"}],
        )
        assert len(req.decisions) == 1

    def test_review_invalid_action(self):
        with pytest.raises(ValidationError):
            ReviewAdjustmentsRequest(
                decisions=[{"adjustment_id": 1, "action": "maybe"}],
            )


# ---------------------------------------------------------------------------
# Integration tests - decorator returns 400 on invalid, passes on valid
# ---------------------------------------------------------------------------


class TestValidationIntegration:
    """Verify the @validate_body decorator returns proper 400 responses."""

    def test_login_invalid_body_returns_400(self, client):
        _reset_lockout()
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"] == "validation_error"
        assert "details" in data
        assert isinstance(data["details"], list)
        assert len(data["details"]) > 0

    def test_login_valid_body_passes_through(self, client):
        _reset_lockout()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200
        assert "token" in resp.get_json()

    def test_login_wrong_type_returns_400(self, client):
        resp = client.post("/api/auth/login", json={"username": 123, "password": "admin"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_receive_invalid_body_returns_400(self, client, auth_headers):
        resp = client.post("/api/receiving/receive", json={"po_id": -1}, headers=auth_headers)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_putaway_same_bins_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/putaway/confirm",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 1, "quantity": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_create_batch_empty_identifiers_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": [], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_fulfill_missing_carrier_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/shipping/fulfill",
            json={"so_id": 1, "tracking_number": "T123"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_transfer_same_bins_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/transfers/move",
            json={"item_id": 1, "from_bin_id": 1, "to_bin_id": 1, "quantity": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_create_item_missing_sku_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/items",
            json={"item_name": "Test"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_create_user_invalid_role_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/users",
            json={"username": "x", "password": "y", "full_name": "z", "role": "SUPERADMIN"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_create_zone_invalid_type_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/zones",
            json={"warehouse_id": 1, "zone_code": "Z9", "zone_name": "Bad", "zone_type": "INVALID"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_create_bin_invalid_type_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/admin/bins",
            json={
                "zone_id": 1, "warehouse_id": 1, "bin_code": "X-99",
                "bin_barcode": "X-99", "bin_type": "BadType",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_cycle_count_empty_bins_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_settings_empty_returns_400(self, client, auth_headers):
        resp = client.put(
            "/api/admin/settings",
            json={"settings": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_no_json_body_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/picking/complete-batch",
            data="not json",
            content_type="text/plain",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_validation_error_detail_structure(self, client):
        """Verify the details array contains the expected pydantic error fields."""
        _reset_lockout()
        resp = client.post("/api/auth/login", json={"username": ""})
        assert resp.status_code == 400
        details = resp.get_json()["details"]
        assert len(details) > 0
        detail = details[0]
        assert "type" in detail
        assert "loc" in detail
        assert "msg" in detail
