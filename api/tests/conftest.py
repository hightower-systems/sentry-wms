import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from app import create_app

SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed-apartment-lab.sql")

ALL_TABLES = [
    "app_settings",
    "audit_log",
    "inventory_adjustments",
    "cycle_count_lines",
    "cycle_counts",
    "item_fulfillment_lines",
    "item_fulfillments",
    "wave_pick_breakdown",
    "wave_pick_orders",
    "pick_tasks",
    "pick_batch_orders",
    "pick_batches",
    "bin_transfers",
    "item_receipts",
    "sales_order_lines",
    "sales_orders",
    "purchase_order_lines",
    "purchase_orders",
    "inventory",
    "items",
    "bins",
    "zones",
    "users",
    "warehouses",
]


def _reset_database():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE " + ", ".join(ALL_TABLES) + " RESTART IDENTITY CASCADE")
    with open(SEED_PATH) as f:
        cur.execute(f.read())
    cur.close()
    conn.close()


@pytest.fixture(scope="session")
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def reset_db():
    _reset_database()
    yield


@pytest.fixture(scope="session")
def auth_headers(client):
    _reset_database()
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    data = resp.get_json()
    return {"Authorization": f"Bearer {data['token']}"}


@pytest.fixture()
def seed_data():
    return {
        "warehouse_id": 1,
        "staging_bin_id": 1,
        "storage_bin_ids": [2, 3, 4, 5, 6, 7],
        "outbound_staging_bin_id": 8,
        "shipping_bin_id": 9,
        "item_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "po_id": 1,
        "so_ids": [1, 2],
    }
