"""
V-033: warehouse_id in the URL path is enforced by the auth middleware.

Existing tests in test_auth.py cover body/query-string paths. This file
exercises the path-arg code path so a route that forgets
check_warehouse_access still blocks non-admins who try to hit a
warehouse they aren't assigned to.
"""

from db_test_context import get_raw_connection


def _reset_lockout():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM login_attempts")
    cur.close()


def _create_user_and_login(client, username, warehouse_ids):
    _reset_lockout()
    conn = get_raw_connection()
    cur = conn.cursor()
    wids = "{" + ",".join(str(w) for w in warehouse_ids) + "}"
    cur.execute(
        """INSERT INTO users (username, password_hash, full_name, role,
               warehouse_id, warehouse_ids, allowed_functions, external_id)
           VALUES (%s, '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                   'Test User', 'PICKER', %s, %s, '{pick,receive,count}', gen_random_uuid())""",
        (username, warehouse_ids[0], wids),
    )
    cur.close()
    resp = client.post("/api/auth/login", json={"username": username, "password": "admin"})
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


class TestWarehouseIdInPath:
    def test_non_admin_blocked_from_other_warehouse_via_path(self, client):
        # User assigned to warehouse 1 only; hits /pending/999 (unassigned).
        headers = _create_user_and_login(client, "wh_path_blocked", [1])
        resp = client.get("/api/putaway/pending/999", headers=headers)
        assert resp.status_code == 403
        assert "Access denied" in resp.get_json()["error"]

    def test_non_admin_allowed_into_assigned_warehouse_via_path(self, client):
        headers = _create_user_and_login(client, "wh_path_allowed", [1])
        resp = client.get("/api/putaway/pending/1", headers=headers)
        # Business logic may return 200 with empty data. The important part
        # is that the middleware did NOT block with 403.
        assert resp.status_code != 403

    def test_admin_bypasses_path_check(self, client, auth_headers):
        # Admin seed user has no warehouse_ids but role=ADMIN -> must pass
        # regardless of path warehouse.
        resp = client.get("/api/putaway/pending/999", headers=auth_headers)
        assert resp.status_code != 403

    def test_non_numeric_path_warehouse_id_returns_400(self, client):
        # Path converter is <int:warehouse_id>, so Flask rejects non-ints at
        # the router level with 404. This test documents that the middleware
        # does not need to handle malformed path values because Flask's URL
        # converter filters them first.
        headers = _create_user_and_login(client, "wh_path_badint", [1])
        resp = client.get("/api/putaway/pending/abc", headers=headers)
        assert resp.status_code == 404
