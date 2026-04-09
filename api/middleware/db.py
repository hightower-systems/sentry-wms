"""
Database session decorator - eliminates manual get_db() / try / except / finally
boilerplate from every route handler.
"""

from functools import wraps

from flask import g

import models.database as _db


def with_db(f):
    """Decorator that provides g.db for the duration of a request.

    Rolls back on exception, always closes the session.
    Routes must still call g.db.commit() explicitly for write operations.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        g.db = _db.SessionLocal()
        try:
            return f(*args, **kwargs)
        except Exception:
            g.db.rollback()
            raise
        finally:
            g.db.close()
    return wrapper
