"""Tests for mobile app API endpoints."""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2


def _db_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    return conn


# ── Warehouse list (public, no auth) ──────────────────────────


def test_warehouse_list_no_auth(client):
    """GET /api/warehouses/list works without JWT."""
    resp = client.get("/api/warehouses/list")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "warehouses" in data
    assert len(data["warehouses"]) >= 1


def test_warehouse_list_fields(client):
    """Response contains only id, name, code."""
    resp = client.get("/api/warehouses/list")
    data = resp.get_json()
    wh = data["warehouses"][0]
    assert set(wh.keys()) == {"id", "name", "code"}
    assert wh["code"] == "APT-LAB"


# ── Auth /me with allowed_functions ───────────────────────────


def test_me_admin_all_functions(client, auth_headers):
    """Admin role always gets all functions."""
    resp = client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["username"] == "admin"
    assert data["role"] == "ADMIN"
    assert set(data["allowed_functions"]) == {"receive", "pick", "pack_ship", "count", "transfer"}


def test_me_picker_role(client, auth_headers):
    """Picker user gets only their assigned functions."""
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO users (username, password_hash, full_name, role, warehouse_id, allowed_functions)
           VALUES ('picker1', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                   'Picker One', 'PICKER', 1, '{pick,count}')"""
    )
    cur.close()
    conn.close()

    # Login as picker1
    resp = client.post("/api/auth/login", json={"username": "picker1", "password": "admin"})
    token = resp.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["role"] == "PICKER"
    assert set(data["allowed_functions"]) == {"pick", "count"}


def test_me_empty_functions(client, auth_headers):
    """User with no assigned functions gets empty array."""
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO users (username, password_hash, full_name, role, warehouse_id, allowed_functions)
           VALUES ('receiver1', '$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK',
                   'Receiver One', 'RECEIVER', 1, '{}')"""
    )
    cur.close()
    conn.close()

    resp = client.post("/api/auth/login", json={"username": "receiver1", "password": "admin"})
    token = resp.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["allowed_functions"] == []


# ── Active batch endpoint ─────────────────────────────────────


def test_active_batch_none(client, auth_headers):
    """No active batch returns active=false."""
    resp = client.get("/api/picking/active-batch", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["active"] is False


def test_active_batch_exists(client, auth_headers):
    """Active batch returns details with pick counts."""
    conn = _db_conn()
    cur = conn.cursor()
    # Create a batch assigned to admin
    cur.execute(
        """INSERT INTO pick_batches (batch_number, warehouse_id, status, assigned_to, total_orders)
           VALUES ('BATCH-ACTIVE-01', 1, 'IN_PROGRESS', 'admin', 2)
           RETURNING batch_id"""
    )
    batch_id = cur.fetchone()[0]
    # Create pick tasks (3 total: 2 picked, 1 pending)
    cur.execute(
        """INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, bin_id, quantity_to_pick, pick_sequence, status)
           VALUES
           (%s, 1, 1, 1, 2, 2, 100, 'PICKED'),
           (%s, 1, 2, 6, 7, 1, 200, 'SHORT'),
           (%s, 2, 3, 3, 4, 3, 300, 'PENDING')""",
        (batch_id, batch_id, batch_id),
    )
    cur.close()
    conn.close()

    resp = client.get("/api/picking/active-batch", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["active"] is True
    assert data["batch_id"] == batch_id
    assert data["total_picks"] == 3
    assert data["completed_picks"] == 2
    assert data["total_orders"] == 2


def test_active_batch_completed(client, auth_headers):
    """Completed batch is not returned as active."""
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO pick_batches (batch_number, warehouse_id, status, assigned_to, total_orders)
           VALUES ('BATCH-DONE-01', 1, 'COMPLETED', 'admin', 1)"""
    )
    cur.close()
    conn.close()

    resp = client.get("/api/picking/active-batch", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["active"] is False


# ── Session settings ──────────────────────────────────────────


def test_session_timeout_default(client):
    """App settings seed includes session_timeout_hours = 8."""
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = 'session_timeout_hours'")
    row = cur.fetchone()
    cur.close()
    conn.close()
    assert row is not None
    assert row[0] == "8"
