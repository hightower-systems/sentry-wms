"""
V-041: rate limiting beyond /auth/login.

Flask-Limiter wired up at module import time so route decorators can
reference ``limiter.limit(...)`` without a post-app-creation lookup.
``init_limiter`` binds it to the Flask app during create_app.

Key function returns the authenticated user_id when available and
falls back to the remote IP otherwise. V-107: the two code paths
see different values for ``g.current_user`` because they run at
different points in the request lifecycle, and the net effect is
that only per-route limits are user-aware:

- Per-route limits applied via ``@limiter.limit(...)`` decorate the
  view function, which Flask calls AFTER ``@require_auth`` has
  populated ``g.current_user``. Those limits get the JWT-aware
  ``user:<id>`` key (useful for users behind a NAT).
- The global ``default_limits`` are evaluated in a Flask
  ``before_request`` hook that fires BEFORE ``@require_auth``, so
  ``g.current_user`` is unset and the key always falls back to
  ``ip:<remote_addr>``. Treat the default as a coarse per-IP
  backstop, not a per-user quota.

Default: 300/minute per IP. Sensitive routes override via
``@limiter.limit("N per minute")`` with a tighter user-keyed budget.

Storage backend: Redis DB 1 in production (derived from
CELERY_BROKER_URL, supports both ``redis://`` and TLS ``rediss://``),
in-memory otherwise. Both modes handle the same decorator surface so
tests and dev do not need Redis running.
"""

import os
from urllib.parse import urlparse, urlunparse

from flask import g, request
from flask_limiter import Limiter

DEFAULT_LIMITS = ["300 per minute"]


def _rate_limit_key() -> str:
    """Prefer the authenticated X-WMS-Token or JWT user; fall back to the remote IP.

    Buckets are namespaced by source so a noisy v1.5.0 connector token
    cannot starve interactive cookie users and vice versa. Evaluation
    order matches the request lifecycle: @require_wms_token runs on
    /api/v1/* routes and populates g.current_token; @require_auth on
    cookie-auth routes populates g.current_user with a user_id.
    """
    try:
        token = getattr(g, "current_token", None)
        if token and token.get("token_id") is not None:
            return f"token:{token['token_id']}"
    except Exception:
        pass
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

    V-107: accepts both ``redis://`` and ``rediss://`` (TLS) prefixes.
    A previous version only matched ``redis://`` and silently dropped
    TLS configurations back to in-memory, which multiplies the
    effective limit by gunicorn worker count in production.
    """
    broker = os.getenv("CELERY_BROKER_URL", "")
    if not broker.startswith(("redis://", "rediss://")):
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
