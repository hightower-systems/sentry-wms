"""
V-017: validate_body rejects unknown top-level fields.

Exercises a handful of real endpoints to confirm the decorator-level
extras check is wired up regardless of which route the request lands
on. Also confirms the framework-allowlisted `warehouse_id` still flows
through because the auth middleware reads it from the body.
"""


class TestExtraFieldsRejected:
    def test_login_rejects_unknown_field(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin",
            "is_admin": True,  # mass-assignment attempt
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "validation_error"
        assert any(
            d.get("type") == "extra_forbidden" and d.get("loc") == ["is_admin"]
            for d in body.get("details", [])
        )

    def test_login_without_extras_still_succeeds(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin",
        })
        assert resp.status_code == 200

    def test_receive_with_warehouse_id_still_works(self, client, auth_headers, seed_data):
        # warehouse_id is framework-allowlisted (middleware reads it); its
        # presence must not trip the extras gate even though the schema
        # does not declare it.
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": seed_data["po_id"],
                "items": [{
                    "item_id": seed_data["item_ids"][0],
                    "quantity": 1,
                    "bin_id": seed_data["staging_bin_id"],
                }],
                "warehouse_id": seed_data["warehouse_id"],
            },
            headers=auth_headers,
        )
        assert resp.status_code != 400 or (
            resp.get_json() or {}
        ).get("error") != "validation_error"

    def test_receive_rejects_unknown_field(self, client, auth_headers, seed_data):
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": seed_data["po_id"],
                "items": [{"item_id": seed_data["item_ids"][0], "quantity": 1,
                           "bin_id": seed_data["staging_bin_id"]}],
                "warehouse_id": seed_data["warehouse_id"],
                "backdoor_flag": "yes",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "validation_error"
        assert any(d.get("loc") == ["backdoor_flag"] for d in body["details"])

    def test_unknown_field_error_names_the_field(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin",
            "role_override": "ADMIN",
        })
        assert resp.status_code == 400
        body = resp.get_json()
        details = body["details"]
        # The error message should surface the offending field name so
        # developers can fix the caller without guessing.
        assert any("role_override" in str(d.get("msg", "")) for d in details)
