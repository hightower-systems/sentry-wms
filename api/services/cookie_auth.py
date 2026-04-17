"""
Cookie-based auth helpers for V-045.

The admin SPA authenticates via an HttpOnly auth cookie plus a readable
CSRF token cookie (double-submit pattern). Mobile and other bearer-token
clients continue to use the Authorization header; the cookies are
ignored on that path.
"""

import secrets

from flask import request

from services.auth_service import TOKEN_EXPIRY_HOURS

AUTH_COOKIE_NAME = "sentry_auth"
CSRF_COOKIE_NAME = "sentry_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _cookie_secure() -> bool:
    # True over HTTPS (direct or via a TLS-terminating reverse proxy).
    return bool(request.is_secure) or request.headers.get("X-Forwarded-Proto") == "https"


def set_auth_cookies(response, token: str, csrf_token: str) -> None:
    secure = _cookie_secure()
    max_age = TOKEN_EXPIRY_HOURS * 3600
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="Strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="Strict",
        path="/",
    )


def clear_auth_cookies(response) -> None:
    secure = _cookie_secure()
    response.set_cookie(
        AUTH_COOKIE_NAME,
        "",
        expires=0,
        max_age=0,
        httponly=True,
        secure=secure,
        samesite="Strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        "",
        expires=0,
        max_age=0,
        httponly=False,
        secure=secure,
        samesite="Strict",
        path="/",
    )


def csrf_token_matches() -> bool:
    header = request.headers.get(CSRF_HEADER_NAME)
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not header or not cookie:
        return False
    return secrets.compare_digest(header, cookie)
