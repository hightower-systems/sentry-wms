"""
V-041: rate limiting beyond /auth/login.

Flask-Limiter wired up at module import time so route decorators can
reference ``limiter.limit(...)`` without a post-app-creation lookup.
``init_limiter`` binds it to the Flask app during create_app.

Key function prefers the authenticated user_id so per-user quotas
apply even when users are behind a NAT; falls back to the remote IP
for unauthenticated requests.

Default: 300/minute per key. Sensitive routes override via
``@limiter.limit("N per minute")`` with a tighter budget.

Storage backend: Redis DB 1 in production (derived from
CELERY_BROKER_URL), in-memory otherwise. Both modes handle the same
decorator surface so tests and dev do not need Redis running.
"""

import os
from urllib.parse import urlparse, urlunparse

from flask import g, request
from flask_limiter import Limiter

DEFAULT_LIMITS = ["300 per minute"]


def _rate_limit_key() -> str:
    """Prefer the authenticated user_id; fall back to the remote IP."""
    try:
        user = getattr(g, "current_user", None)
        if user and user.get("user_id") is not None:
            return f"user:{user['user_id']}"
    except Exception:
        pass
    return f"ip:{request.remote_addr or 'unknown'}"


def _resolve_storage_uri() -> str:
    """Derive a Redis URI from CELERY_BROKER_URL.

    The rate limiter uses DB 1 so it does not collide with Celery's
    task queue on DB 0. Falls back to in-memory when no broker is
    configured, which is fine for dev and tests.
    """
    broker = os.getenv("CELERY_BROKER_URL", "")
    if not broker.startswith("redis://"):
        return "memory://"
    parsed = urlparse(broker)
    parsed = parsed._replace(path="/1")
    return urlunparse(parsed)


# Module-level limiter so @limiter.limit(...) works on route decorators
# that are parsed before create_app runs.
limiter = Limiter(
    key_func=_rate_limit_key,
    default_limits=DEFAULT_LIMITS,
    storage_uri=_resolve_storage_uri(),
    strategy="fixed-window",
)


def init_limiter(app) -> Limiter:
    limiter.init_app(app)
    return limiter


def get_limiter() -> Limiter:
    return limiter
