import re
import uuid

from shipyard.helpers import create_zone_mapping


def _login(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


def _create_role_user(
    client,
    admin_headers,
    *,
    role,
    page_keys,
    warehouse_ids=None,
):
    username = f"matrix-{uuid.uuid4().hex[:12]}"
    password = "RoleDemo2026!"
    response = client.post(
        "/api/admin/users",
        json={
            "username": username,
            "password": password,
            "full_name": f"{role} matrix",
            "role": role,
            "warehouse_ids": warehouse_ids or [1],
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


def _route_path(rule):
    path = re.sub(
        r"<uuid:[^>]+>",
        "00000000-0000-0000-0000-000000000001",
        rule,
    )
    return re.sub(r"<int:[^>]+>", "1", path)


def _shipyard_routes(app):
    return sorted(
        (
            rule.rule,
            sorted(rule.methods - {"HEAD", "OPTIONS"}),
        )
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/shipyard/")
    )


def _request_route(client, route, method, headers):
    return client.open(
        _route_path(route),
        method=method,
        headers=headers,
    )


def test_every_shipyard_route_requires_a_page_permission(client, auth_headers):
    admin_headers = _login(client, "admin", "admin")
    manager_without_shipyard_page = _create_role_user(
        client,
        admin_headers,
        role="SHIPYARD_MANAGER",
        page_keys=["dashboard"],
    )
    routes = _shipyard_routes(client.application)

    assert len(routes) == 49
    for route, methods in routes:
        for method in methods:
            response = _request_route(
                client,
                route,
                method,
                manager_without_shipyard_page,
            )
            assert response.status_code == 403, (
                route,
                method,
                response.status_code,
                response.get_json(),
            )


def test_every_shipyard_write_route_requires_a_business_role(
    client,
    auth_headers,
):
    admin_headers = _login(client, "admin", "admin")
    viewer_with_all_shipyard_pages = _create_role_user(
        client,
        admin_headers,
        role="VIEWER",
        page_keys=[
            "dashboard",
            "inbound",
            "shipyard-outbound",
            "shipyard-settings",
        ],
    )
    routes = _shipyard_routes(client.application)

    write_routes = [
        (route, method)
        for route, methods in routes
        for method in methods
        if method != "GET"
    ]
    assert len(write_routes) == 26
    for route, method in write_routes:
        response = _request_route(
            client,
            route,
            method,
            viewer_with_all_shipyard_pages,
        )
        assert response.status_code == 403, (
            route,
            method,
            response.status_code,
            response.get_json(),
        )
        assert response.get_json().get("error") == "role_forbidden"


def test_zone_mapping_update_is_scoped_to_the_mapping_warehouse(
    client,
    auth_headers,
):
    admin_headers = _login(client, "admin", "admin")
    mapping = create_zone_mapping(
        client,
        admin_headers,
        f"HMATRIX-{uuid.uuid4().hex[:8]}",
    )
    warehouse_two_manager = _create_role_user(
        client,
        admin_headers,
        role="SHIPYARD_MANAGER",
        page_keys=["dashboard", "shipyard-settings"],
        warehouse_ids=[2],
    )

    list_response = client.get(
        "/api/shipyard/zone-mappings?warehouse_id=1",
        headers=warehouse_two_manager,
    )
    assert list_response.status_code == 403

    update_response = client.put(
        f"/api/shipyard/zone-mappings/{mapping['mapping_id']}",
        json={"is_primary": False},
        headers=warehouse_two_manager,
    )
    assert update_response.status_code == 403
