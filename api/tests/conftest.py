import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
os.environ.setdefault(
    "SENTRY_PUBSUB_HMAC_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)
# #238: api container's create_app now runs validate_or_die,
# which requires REDIS_URL when the dispatcher is enabled. CI
# sets it to the service-container URL; the test default makes
# local pytest runs self-contained without forcing operators to
# set the var explicitly. The wake module's pubsub publish path
# soft-fails when Redis is unreachable, so a bogus URL here
# does not break tests that don't exercise the publish surface.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_api_dir = os.path.join(_tests_dir, "..")
sys.path.insert(0, os.path.abspath(_api_dir))
sys.path.insert(0, _tests_dir)

import psycopg2
import pytest
from sqlalchemy.orm import sessionmaker

from app import create_app
import models.database as db

import db_test_context

SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed-apartment-lab.sql")
if not os.path.exists(SEED_PATH):
    SEED_PATH = "/db/seed-apartment-lab.sql"

ALL_TABLES = [
    "integration_events",
    "snapshot_scans",
    "wms_tokens",
    "consumer_groups",
    "connectors",
    "sync_state",
    "connector_credentials",
    "login_attempts",
    "preferred_bins",
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

_ORIGINAL_SESSION_LOCAL = db.SessionLocal


def _seed_database():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE " + ", ".join(ALL_TABLES) + " RESTART IDENTITY CASCADE")
    with open(SEED_PATH) as f:
        cur.execute(f.read())
    # The seed SQL inserts the admin user with a placeholder password_hash
    # (see V-069). In production, seed.sh overwrites it with a random
    # password. Tests need a deterministic password, so we install a
    # bcrypt hash of "admin" here. Keep this logic test-only.
    import bcrypt as _bcrypt
    _pw_hash = _bcrypt.hashpw(b"admin", _bcrypt.gensalt()).decode("utf-8")
    cur.execute(
        "UPDATE users SET password_hash = %s WHERE username = 'admin'",
        (_pw_hash,),
    )
    cur.close()
    conn.close()


def _driver_connection(sa_conn):
    if hasattr(sa_conn, "get_driver_connection"):
        return sa_conn.get_driver_connection()
    return sa_conn.connection.dbapi_connection


@pytest.fixture(scope="session")
def _seed_session_database():
    _seed_database()
    yield


@pytest.fixture(scope="session")
def app(_seed_session_database):
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _db_transaction(_seed_session_database):
    # Each test holds an open transaction until teardown. Another process using the same
    # database (e.g. the Flask API container) can block or deadlock with these transactions;
    # run the suite with exclusive DB access (CI does this by default).
    conn = db.engine.connect()
    trans = conn.begin()
    db.SessionLocal = sessionmaker(
        bind=conn,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    db_test_context.set_raw_connection(_driver_connection(conn))
    yield conn
    db_test_context.clear_raw_connection()
    db.SessionLocal = _ORIGINAL_SESSION_LOCAL
    trans.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def _reset_rate_limit_storage():
    """V-041 / #214: the module-level Flask-Limiter shares storage
    across tests. Without a reset, tests that hit the same admin
    endpoint many times bleed into each other and trip 429 in
    unrelated test methods. Reset before every test so each one
    starts with a fresh quota."""
    try:
        from services.rate_limit import limiter
        limiter._storage.reset()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _clear_test_cookies(client):
    # V-045: login now sets HttpOnly + CSRF cookies. The session-scoped test
    # client persists cookies across tests, which would cause tests that
    # expect 401 to accidentally authenticate via a leftover cookie. Clear
    # before every test so each one starts without session state.
    try:
        client._cookies.clear()
    except AttributeError:
        pass
    yield


@pytest.fixture(scope="session")
def auth_headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    data = resp.get_json()
    return {"Authorization": f"Bearer {data['token']}"}


@pytest.fixture()
def seed_data():
    return {
        "warehouse_id": 1,
        "staging_bin_id": 1,
        "storage_bin_ids": [3, 4, 5, 6, 7, 8, 9, 10, 11],
        "outbound_staging_bin_id": 14,
        "shipping_bin_id": 15,
        "item_ids": list(range(1, 21)),
        "po_id": 1,
        "so_ids": [1, 2],
    }
