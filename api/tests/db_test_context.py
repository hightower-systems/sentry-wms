"""Thread-local raw DB-API connection for tests (aligned with SQLAlchemy test transaction)."""

import threading

_tls = threading.local()


def set_raw_connection(conn):
    _tls.connection = conn


def get_raw_connection():
    try:
        return _tls.connection
    except AttributeError as e:
        raise RuntimeError(
            "get_raw_connection() is only valid during a test (inside conftest _db_transaction)"
        ) from e


def clear_raw_connection():
    if hasattr(_tls, "connection"):
        del _tls.connection
