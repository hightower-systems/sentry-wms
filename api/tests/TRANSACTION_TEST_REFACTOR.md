# Transaction-based test infrastructure refactor

This document summarizes the changes that replaced **per-test `TRUNCATE` + full re-seed** with **session-scoped seeding** and **per-test SQLAlchemy transactions that roll back** (using savepoints for `session.commit()`).

## Goals

- Seed the database **once** per pytest session.
- Wrap each test in a transaction that **rolls back** after the test.
- Remove the autouse pattern that ran `TRUNCATE`, re-ran the seed SQL, and called `engine.dispose()` on every test.
- Align **raw SQL** in tests with the same connection/transaction as the Flask app under test.

## New files

### `api/tests/db_test_context.py`

- Thread-local storage for the **raw DB-API connection** (psycopg2) that matches the SQLAlchemy connection used for the current test’s outer transaction.
- API:
  - `set_raw_connection(conn)` — set by `conftest` at the start of each test transaction.
  - `get_raw_connection()` — used by tests/helpers instead of `psycopg2.connect(...)`.
  - `clear_raw_connection()` — cleared in `conftest` teardown.

## Modified files

### `api/tests/conftest.py`

- **`sys.path`**: Inserts the `tests/` directory (in addition to the API parent) so `import db_test_context` works when the working directory is `/app` or `api/`.
- **`_seed_session_database`** (session-scoped fixture): Calls `_seed_database()` once — `TRUNCATE … CASCADE` on `ALL_TABLES`, then runs `seed-apartment-lab.sql`.
- **`app`**: Now depends on `_seed_session_database` so the DB is seeded before the Flask app is created.
- **`_db_transaction`** (function-scoped, autouse):
  - `engine.connect()`, `begin()` outer transaction.
  - Temporarily sets `models.database.SessionLocal` to a `sessionmaker` bound to that connection with `join_transaction_mode="create_savepoint"` and `expire_on_commit=False` so route code’s `commit()` maps to savepoints inside the outer transaction.
  - Registers the driver connection with `db_test_context`.
  - Teardown: clear context, restore original `SessionLocal`, `rollback()` outer transaction, `close()` connection.
- **Removed**: Autouse `reset_db` (per-test TRUNCATE + seed + `engine.dispose()`).
- **`auth_headers`**: No longer calls `_reset_database()`; relies on session seed + login only.
- **`_driver_connection(sa_conn)`**: Helper to get the underlying psycopg2 connection (`get_driver_connection()` on SQLAlchemy 2.x, else `connection.dbapi_connection`).
- **Comment**: Notes that another process using the same DB (e.g. a running API) can block or deadlock while tests hold open transactions.

### Tests that stopped opening their own psycopg2 connections

Each of these now uses `from db_test_context import get_raw_connection()` (or `_db_conn()` delegating to it) for raw SQL. **Shared connections are not closed** after use; only cursors are closed. **`autocommit = True`** was removed where it was used so writes stay inside the test transaction.

| File | Change summary |
|------|----------------|
| `test_admin.py` | `_query_val`, `_picker_headers`, and inline DB blocks use `get_raw_connection()`; dropped `psycopg2` / `os` imports used only for DB URL. |
| `test_inventory.py` | `_query_val` / `_query_one` use `get_raw_connection()`. |
| `test_mobile_endpoints.py` | `_db_conn()` returns `get_raw_connection()`; removed separate `psycopg2.connect` and `conn.close()` after operations. |
| `test_packing.py` | `_query_val` uses `get_raw_connection()`. |
| `test_picking.py` | `_query_one` and zone/aisle test setup blocks use `get_raw_connection()`. |
| `test_putaway.py` | `_query_one` and preferred-bin / item update blocks use `get_raw_connection()`. |
| `test_receiving.py` | `_query_one` and closed-PO setup use `get_raw_connection()`. |
| `test_shipping.py` | `_query_val`, `_query_one`, `_set_setting` use `get_raw_connection()`. |
| `test_transfers.py` | `_query_val` / `_query_one` use `get_raw_connection()`. |
| `test_wave_picking.py` | Helpers (`_create_extra_so`, `_set_so_status`, `_get_inventory`, `_get_wave_breakdowns`) and inline SQL use `get_raw_connection()`. |
| `test_workflow_integration.py` | `_query_val` uses `get_raw_connection()`. |

## Runtime behavior (short)

1. **Session start**: One TRUNCATE + seed.
2. **Each test**: Outer DB transaction; Flask routes use ORM sessions that commit via savepoints; raw SQL uses the same underlying connection.
3. **Test end**: Rollback outer transaction → database returns to post-seed state for the next test.

## Operational note

Run the test suite with **exclusive access** to the test database when possible (e.g. CI, or stop the API container if it points at the same Postgres). Long-lived transactions per test can interact badly with a concurrently running app.

## Not part of this refactor

- Fixing application/schema mismatches (e.g. admin cycle-count listing expecting a `variance` column that is not in `db/schema.sql`) is outside this harness change.
