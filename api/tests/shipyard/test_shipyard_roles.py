import uuid

from shipyard.helpers import dispatch_line_payload


def _login(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.get_json()
    return {
        "Authorization": f"Bearer {response.get_json()['token']}",
    }


def _create_role_user(client, admin_headers, *, role, page_keys):
    username = f"role-{uuid.uuid4().hex[:12]}"
    password = "RoleDemo2026!"
    response = client.post(
        "/api/admin/users",
        json={
            "username": username,
            "password": password,
            "full_name": f"{role} demo",
            "role": role,
            "warehouse_ids": [1],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.get_json()
    user_id = response.get_json()["user_id"]
    permissions = client.put(
        f"/api/admin/users/{user_id}/permissions",
        json={"page_keys": page_keys},
        headers=admin_headers,
    )
    assert permissions.status_code == 200, permissions.get_json()
    return _login(client, username, password)


def test_role_templates_define_shipyard_access(client, auth_headers):
    auth_headers = _login(client, "admin", "admin")
    response = client.get("/api/admin/users/role-templates", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()
    assert data["roles"] == [
        "ADMIN",
        "USER",
        "SHIPYARD_MANAGER",
        "RECEIVER",
        "OUTBOUND",
        "VIEWER",
    ]
    assert "shipyard-settings" in data["templates"]["SHIPYARD_MANAGER"]["page_keys"]
    assert data["templates"]["RECEIVER"]["functions"] == ["shipyard_receive"]
    assert data["templates"]["OUTBOUND"]["functions"] == ["shipyard_outbound"]
    assert data["templates"]["VIEWER"]["functions"] == []


def test_receiver_is_role_scoped_and_cannot_edit_documents(client, auth_headers):
    auth_headers = _login(client, "admin", "admin")
    receiver_headers = _create_role_user(
        client,
        auth_headers,
        role="RECEIVER",
        page_keys=["dashboard", "inbound"],
    )

    config = client.get("/api/shipyard/bindings", headers=receiver_headers)
    assert config.status_code == 403

    import_attempt = client.post(
        "/api/shipyard/inbound/manifests/preview",
        headers=receiver_headers,
    )
    assert import_attempt.status_code == 403
    assert import_attempt.get_json()["error"] == "role_forbidden"

    receipt_attempt = client.post(
        f"/api/shipyard/inbound/tickets/{uuid.uuid4()}/scan",
        json={"qr_payload": "not-a-real-ticket"},
        headers=receiver_headers,
    )
    assert receipt_attempt.status_code != 403


def test_outbound_role_can_read_but_cannot_import_or_change_config(
    client, auth_headers
):
    auth_headers = _login(client, "admin", "admin")
    outbound_headers = _create_role_user(
        client,
        auth_headers,
        role="OUTBOUND",
        page_keys=["dashboard", "inventory", "shipyard-outbound"],
    )

    listing = client.get(
        "/api/shipyard/outbound/dispatches?warehouse_id=1",
        headers=outbound_headers,
    )
    assert listing.status_code == 200

    create_dispatch = client.post(
        "/api/shipyard/outbound/dispatches",
        json={
            "warehouse_id": 1,
            "dispatch_no": "PT-ROLE-DENIED",
            "dispatch_date": "2026-10-27",
            "idempotency_key": f"dispatch-{uuid.uuid4()}",
            "lines": [dispatch_line_payload()],
        },
        headers=outbound_headers,
    )
    assert create_dispatch.status_code == 403
    assert create_dispatch.get_json()["error"] == "role_forbidden"

    config = client.get("/api/shipyard/bindings", headers=outbound_headers)
    assert config.status_code == 403


def test_outbound_mobile_requires_shipyard_outbound_page_permission(
    client,
    auth_headers,
):
    auth_headers = _login(client, "admin", "admin")
    outbound_headers = _create_role_user(
        client,
        auth_headers,
        role="OUTBOUND",
        page_keys=["dashboard"],
    )

    listing = client.get(
        "/api/shipyard/outbound/mobile/orders?warehouse_id=1",
        headers=outbound_headers,
    )
    assert listing.status_code == 403

    recognition = client.post(
        f"/api/shipyard/outbound/tasks/{uuid.uuid4()}/label-recognition",
        headers=outbound_headers,
    )
    assert recognition.status_code == 403


def test_viewer_is_read_only(client, auth_headers):
    auth_headers = _login(client, "admin", "admin")
    viewer_headers = _create_role_user(
        client,
        auth_headers,
        role="VIEWER",
        page_keys=[
            "dashboard",
            "inventory",
            "inbound",
            "shipyard-outbound",
            "audit-log",
        ],
    )

    listing = client.get(
        "/api/shipyard/outbound/delivery-notes?warehouse_id=1",
        headers=viewer_headers,
    )
    assert listing.status_code == 200

    manifest = client.post(
        "/api/shipyard/inbound/manifests/preview",
        headers=viewer_headers,
    )
    assert manifest.status_code == 403
    assert manifest.get_json()["error"] == "role_forbidden"
