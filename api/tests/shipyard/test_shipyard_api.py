from shipyard.helpers import create_binding as _create_binding
from shipyard.helpers import create_zone_mapping as _create_zone_mapping
from shipyard.helpers import first_bin as _first_bin
from shipyard.helpers import manifest_bytes as _manifest_bytes
from shipyard.helpers import (
    manifest_bytes_without_drawing as _manifest_bytes_without_drawing,
)
from shipyard.helpers import upload_manifest as _upload_manifest


def test_project_binding_and_zone_mapping_are_listed(client, auth_headers):
    binding = _create_binding(client, auth_headers, "H9101")
    mapping = _create_zone_mapping(client, auth_headers, "H9101")

    bindings = client.get(
        "/api/shipyard/bindings?ship_no=H9101",
        headers=auth_headers,
    )
    assert bindings.status_code == 200
    assert bindings.get_json()["bindings"][0]["binding_id"] == binding["binding_id"]

    mappings = client.get(
        "/api/shipyard/zone-mappings?warehouse_id=1&ship_no=H9101",
        headers=auth_headers,
    )
    assert mappings.status_code == 200
    assert mappings.get_json()["mappings"][0]["mapping_id"] == mapping["mapping_id"]
    assert mappings.get_json()["mappings"][0]["is_primary"] is True


def test_preview_warns_when_ship_has_no_primary_zone(client, auth_headers):
    _create_binding(client, auth_headers, "H9102")
    _create_binding(client, auth_headers, "H9102", drawing="D-002")
    response = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes("H9102"),
        manifest_no="IN-H9102-001",
    )

    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["manifest"]["status"] == "PARSED"
    assert data["validation"]["can_confirm"] is False
    assert data["validation"]["missing_zone_ship_nos"] == ["H9102"]
    assert data["lines"][0]["binding"]["status"] == "MATCHED"
    assert data["lines"][0]["suggested_bin"] is None

    confirm = client.post(
        f"/api/shipyard/inbound/manifests/{data['manifest']['manifest_id']}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "zone_configuration_required"


def test_preview_confirm_creates_one_expected_ticket_per_source_row(
    client, auth_headers
):
    _create_binding(client, auth_headers, "H9103")
    _create_binding(client, auth_headers, "H9103", drawing="D-002")
    mapping = _create_zone_mapping(client, auth_headers, "H9103")
    preview = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes("H9103"),
        manifest_no="IN-H9103-001",
    )
    assert preview.status_code == 201, preview.get_json()
    preview_data = preview.get_json()
    assert preview_data["validation"]["can_confirm"] is True, preview_data["validation"]
    assert [line["source_row_no"] for line in preview_data["lines"]] == ["1", "2"]
    assert preview_data["lines"][0]["suggested_bin"]["bin_id"] == mapping["bin_id"]
    assert preview_data["tickets"] == []

    confirm = client.post(
        f"/api/shipyard/inbound/manifests/{preview_data['manifest']['manifest_id']}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirm.status_code == 200, confirm.get_json()
    confirmed = confirm.get_json()
    assert confirmed["manifest"]["status"] == "CONFIRMED"
    assert len(confirmed["tickets"]) == 2
    assert len(confirmed["confirmed_tickets"]) == 2
    assert {ticket["status"] for ticket in confirmed["tickets"]} == {"EXPECTED"}
    assert {ticket["location_path_snapshot"] for ticket in confirmed["tickets"]} == {
        mapping["location_path"]
    }

    repeated = client.post(
        f"/api/shipyard/inbound/manifests/{preview_data['manifest']['manifest_id']}/confirm",
        json={},
        headers=auth_headers,
    )
    assert repeated.status_code == 409
    assert repeated.get_json()["error"] == "manifest_state_conflict"


def test_duplicate_manifest_file_is_rejected(client, auth_headers):
    content = _manifest_bytes("H9104")
    first = _upload_manifest(
        client,
        auth_headers,
        content,
        manifest_no="IN-H9104-001",
    )
    assert first.status_code == 201

    duplicate = _upload_manifest(
        client,
        auth_headers,
        content,
        manifest_no="IN-H9104-002",
    )
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"] == "duplicate_manifest_file"
    assert (
        duplicate.get_json()["manifest_id"]
        == first.get_json()["manifest"]["manifest_id"]
    )


def test_missing_binding_blocks_manifest_confirmation(client, auth_headers):
    _create_zone_mapping(client, auth_headers, "H9106")
    preview = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes("H9106"),
        manifest_no="IN-H9106-001",
    )
    data = preview.get_json()
    assert preview.status_code == 201
    assert data["validation"]["can_confirm"] is False
    assert data["validation"]["binding_error_count"] == 2

    confirm = client.post(
        f"/api/shipyard/inbound/manifests/{data['manifest']['manifest_id']}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "binding_configuration_required"


def test_mismatched_drawing_binding_blocks_confirmation(client, auth_headers):
    _create_binding(client, auth_headers, "H9107", drawing="D-001")
    _create_zone_mapping(client, auth_headers, "H9107")
    preview = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes("H9107"),
        manifest_no="IN-H9107-001",
    )
    data = preview.get_json()
    assert data["validation"]["can_confirm"] is False
    assert [line["binding"]["status"] for line in data["lines"]] == [
        "MATCHED",
        "MISMATCH",
    ]
    assert data["lines"][1]["binding"]["mismatched_fields"] == ["production_drawing_no"]
    assert "制作图号" in data["lines"][1]["binding"]["message"]


def test_adjacent_future_primary_zone_windows_are_schedulable(client, auth_headers):
    bin_id, _bin_code, _zone_code = _first_bin()
    first = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": "H9108",
            "effective_from": "2030-01-01T00:00:00Z",
            "effective_to": "2030-02-01T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert first.status_code == 201, first.get_json()

    second = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": "H9108",
            "effective_from": "2030-02-01T00:00:00Z",
            "effective_to": "2030-03-01T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert second.status_code == 201, second.get_json()


def test_overlapping_future_zone_window_returns_409(client, auth_headers):
    bin_id, _bin_code, _zone_code = _first_bin()
    first = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": "H9109",
            "effective_from": "2030-01-01T00:00:00Z",
            "effective_to": "2030-03-01T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert first.status_code == 201

    overlap = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": "H9109",
            "effective_from": "2030-02-01T00:00:00Z",
            "effective_to": "2030-04-01T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert overlap.status_code == 409
    assert overlap.get_json()["constraint"] == ("shipyard_zone_ship_primary_no_overlap")


def test_naive_datetime_update_returns_422(client, auth_headers):
    binding = _create_binding(client, auth_headers, "H9110")
    response = client.put(
        f"/api/shipyard/bindings/{binding['binding_id']}",
        json={"effective_to": "2030-01-01T00:00:00"},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "validation_error"


def test_binding_update_conflict_returns_409(client, auth_headers):
    first = _create_binding(client, auth_headers, "H9111", drawing="D-A")
    second = _create_binding(client, auth_headers, "H9111", drawing="D-B")
    response = client.put(
        f"/api/shipyard/bindings/{second['binding_id']}",
        json={
            "production_drawing_no": first["production_drawing_no"],
        },
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["constraint"] == ("shipyard_project_binding_no_overlap")


def test_duplicate_binding_create_returns_409(client, auth_headers):
    first = _create_binding(client, auth_headers, "H9113")
    duplicate = client.post(
        "/api/shipyard/bindings",
        json={
            "ship_no": first["ship_no"],
            "engineering_no": first["engineering_no"],
            "production_drawing_no": first["production_drawing_no"],
            "manufacturer_name": first["manufacturer_name"],
        },
        headers=auth_headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"] == "shipyard_config_conflict"
    assert duplicate.get_json()["constraint"] == ("shipyard_project_binding_no_overlap")


def test_binding_update_success_nochange_and_missing(client, auth_headers):
    binding = _create_binding(client, auth_headers, "H9114")
    changed = client.put(
        f"/api/shipyard/bindings/{binding['binding_id']}",
        json={"manufacturer_name": "调整后的厂家"},
        headers=auth_headers,
    )
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()["binding"]["manufacturer_name"] == "调整后的厂家"

    no_change = client.put(
        f"/api/shipyard/bindings/{binding['binding_id']}",
        json={},
        headers=auth_headers,
    )
    assert no_change.status_code == 200
    assert no_change.get_json()["binding"]["binding_id"] == binding["binding_id"]

    missing = client.put(
        "/api/shipyard/bindings/99999999",
        json={},
        headers=auth_headers,
    )
    assert missing.status_code == 404
    assert missing.get_json()["error"] == "shipyard_not_found"


def test_binding_update_rejects_null_start_and_invalid_window(client, auth_headers):
    binding = _create_binding(client, auth_headers, "H9115")
    null_start = client.put(
        f"/api/shipyard/bindings/{binding['binding_id']}",
        json={"effective_from": None},
        headers=auth_headers,
    )
    assert null_start.status_code == 400
    assert null_start.get_json()["error"] == "shipyard_validation_error"

    invalid_window = client.put(
        f"/api/shipyard/bindings/{binding['binding_id']}",
        json={"effective_to": "2000-01-01T00:00:00Z"},
        headers=auth_headers,
    )
    assert invalid_window.status_code == 400
    assert invalid_window.get_json()["error"] == "shipyard_validation_error"


def test_blank_ship_no_is_rejected(client, auth_headers):
    binding = client.post(
        "/api/shipyard/bindings",
        json={"ship_no": "   "},
        headers=auth_headers,
    )
    assert binding.status_code == 400
    assert binding.get_json()["error"] == "validation_error"

    bin_id, _bin_code, _zone_code = _first_bin()
    mapping = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": "   ",
        },
        headers=auth_headers,
    )
    assert mapping.status_code == 400
    assert mapping.get_json()["error"] == "validation_error"


def test_ambiguous_binding_blocks_confirmation(client, auth_headers):
    _create_binding(client, auth_headers, "H9116", drawing="D-A")
    _create_binding(client, auth_headers, "H9116", drawing="D-B")
    _create_zone_mapping(client, auth_headers, "H9116")
    preview = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes_without_drawing("H9116"),
        manifest_no="IN-H9116-001",
    )
    assert preview.status_code == 201, preview.get_json()
    data = preview.get_json()
    assert data["validation"]["can_confirm"] is False
    assert data["validation"]["binding_error_count"] == 1
    assert data["lines"][0]["binding"]["status"] == "AMBIGUOUS"

    confirm = client.post(
        f"/api/shipyard/inbound/manifests/{data['manifest']['manifest_id']}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "binding_configuration_required"


def test_zone_mapping_create_rejects_invalid_bin_and_window(client, auth_headers):
    invalid_bin = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": 99999999,
            "ship_no": "H9117",
        },
        headers=auth_headers,
    )
    assert invalid_bin.status_code == 400
    assert invalid_bin.get_json()["error"] == "shipyard_validation_error"

    bin_id, _bin_code, _zone_code = _first_bin()
    invalid_window = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": "H9117",
            "effective_from": "2030-03-01T00:00:00Z",
            "effective_to": "2030-02-01T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert invalid_window.status_code == 400
    assert invalid_window.get_json()["error"] == "shipyard_validation_error"


def test_zone_mapping_update_success_nochange_and_missing(client, auth_headers):
    mapping = _create_zone_mapping(client, auth_headers, "H9118")
    changed = client.put(
        f"/api/shipyard/zone-mappings/{mapping['mapping_id']}",
        json={"is_primary": False},
        headers=auth_headers,
    )
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()["mapping"]["is_primary"] is False

    no_change = client.put(
        f"/api/shipyard/zone-mappings/{mapping['mapping_id']}",
        json={},
        headers=auth_headers,
    )
    assert no_change.status_code == 200
    assert no_change.get_json()["mapping"]["mapping_id"] == mapping["mapping_id"]

    missing = client.put(
        "/api/shipyard/zone-mappings/99999999",
        json={},
        headers=auth_headers,
    )
    assert missing.status_code == 404
    assert missing.get_json()["error"] == "shipyard_not_found"


def test_zone_mapping_update_rejects_invalid_bin_and_window(client, auth_headers):
    mapping = _create_zone_mapping(client, auth_headers, "H9119")
    invalid_bin = client.put(
        f"/api/shipyard/zone-mappings/{mapping['mapping_id']}",
        json={"bin_id": 99999999},
        headers=auth_headers,
    )
    assert invalid_bin.status_code == 400

    invalid_window = client.put(
        f"/api/shipyard/zone-mappings/{mapping['mapping_id']}",
        json={"effective_to": "2000-01-01T00:00:00Z"},
        headers=auth_headers,
    )
    assert invalid_window.status_code == 400


def test_original_file_is_retained_downloadable_and_void_allows_reimport(
    client, auth_headers
):
    content = _manifest_bytes("H9112")
    preview = _upload_manifest(
        client,
        auth_headers,
        content,
        manifest_no="IN-H9112-001",
    )
    assert preview.status_code == 201
    manifest_id = preview.get_json()["manifest"]["manifest_id"]

    download = client.get(
        f"/api/shipyard/inbound/manifests/{manifest_id}/file",
        headers=auth_headers,
    )
    assert download.status_code == 200
    assert download.data == content
    assert download.headers["Content-Disposition"].startswith("attachment;")

    voided = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/void",
        json={"reason": "误导入，需要重新选择厂家"},
        headers=auth_headers,
    )
    assert voided.status_code == 200, voided.get_json()
    assert voided.get_json()["manifest"]["status"] == "VOID"
    assert voided.get_json()["manifest"]["metadata"]["void_reason"] == (
        "误导入，需要重新选择厂家"
    )

    reimported = _upload_manifest(
        client,
        auth_headers,
        content,
        manifest_no="IN-H9112-002",
    )
    assert reimported.status_code == 201, reimported.get_json()
    assert reimported.get_json()["manifest"]["manifest_id"] != manifest_id


def test_blank_void_reason_is_rejected(client, auth_headers):
    preview = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes("H9120"),
        manifest_no="IN-H9120-001",
    )
    manifest_id = preview.get_json()["manifest"]["manifest_id"]
    response = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/void",
        json={"reason": "   "},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "validation_error"


def test_manifest_list_and_detail_expose_validation_summary(client, auth_headers):
    _create_binding(client, auth_headers, "H9105")
    _create_binding(client, auth_headers, "H9105", drawing="D-002")
    _create_zone_mapping(client, auth_headers, "H9105")
    preview = _upload_manifest(
        client,
        auth_headers,
        _manifest_bytes("H9105"),
        manifest_no="IN-H9105-001",
    )
    manifest_id = preview.get_json()["manifest"]["manifest_id"]

    listing = client.get(
        "/api/shipyard/inbound/manifests?warehouse_id=1",
        headers=auth_headers,
    )
    assert listing.status_code == 200
    assert listing.get_json()["manifests"][0]["line_count"] == 2
    assert listing.get_json()["manifests"][0]["total_quantity"] == "20.000"

    detail = client.get(
        f"/api/shipyard/inbound/manifests/{manifest_id}",
        headers=auth_headers,
    )
    assert detail.status_code == 200
    assert detail.get_json()["validation"]["summary"]["line_count"] == 2
    assert detail.get_json()["validation"]["summary"]["total_quantity"] == "20.000"
