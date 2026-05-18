"""
Route protection decorators for JWT authentication and role-based access.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

from flask import g, jsonify, request
from sqlalchemy import text

from services.auth_service import decode_token
from services.cookie_auth import (
    AUTH_COOKIE_NAME,
    CSRF_PROTECTED_METHODS,
    csrf_token_matches,
)
from services import token_cache

# v1.5.1 V-209 (#149): dedicated logger so operators can dial up
# DEBUG to recover the specific auth failure mode (missing, unknown
# hash, revoked, expired) without putting it on the wire. The HTTP
# response collapses all four into a single "invalid_token" body
# to close the enumeration oracle.
_INVALID_LOGGER = logging.getLogger("sentry_wms.auth.wms_token")


# Endpoints a user with must_change_password=true is allowed to call.
# Any other endpoint returns 403 password_change_required until the user
# completes the change-password flow. Keep this list tight -- adding a
# fourth entry widens the forced-change escape hatch and warrants review.
FORCED_CHANGE_ALLOWED_ENDPOINTS = frozenset({
    "auth.change_password",
    "auth.logout",
    "auth.me",
})


def _extract_token():
    """Return (token, source) where source is 'header' or 'cookie', or (None, None)."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1], "header"
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        return cookie_token, "cookie"
    return None, None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token, source = _extract_token()
        if not token:
            return jsonify({"error": "Unauthorized"}), 401

        # V-045: cookie-auth callers must prove they can read the CSRF cookie
        # on mutating requests (double-submit). Bearer-header callers are
        # exempt because bearer tokens don't auto-attach cross-origin.
        if source == "cookie" and request.method in CSRF_PROTECTED_METHODS:
            if not csrf_token_matches():
                return jsonify({"error": "CSRF token missing or invalid"}), 403

        payload = decode_token(token)
        if payload is None:
            return jsonify({"error": "Token expired"}), 401

        # Verify the user is still active and refresh role/warehouse_ids from DB.
        # This ensures that deactivated accounts and role/warehouse changes take
        # effect immediately rather than waiting for the JWT to expire.
        # avid-overhaul-mk1 P6.1: same session also fetches the user's
        # per-page web-admin grants (USER role only). Stashed on payload
        # so the @require_admin_or_page_permission decorator stays a
        # pure list lookup; ADMIN bypasses with allowed_pages=None.
        import models.database as _db
        db = _db.SessionLocal()
        try:
            row = db.execute(
                text(
                    "SELECT role, is_active, warehouse_ids, password_changed_at, "
                    "must_change_password "
                    "FROM users WHERE user_id = :uid"
                ),
                {"uid": payload["user_id"]},
            ).fetchone()
            allowed_pages = None
            if row and row.is_active and row.role != "ADMIN":
                page_rows = db.execute(
                    text(
                        "SELECT page_key FROM user_page_permissions "
                        "WHERE user_id = :uid"
                    ),
                    {"uid": payload["user_id"]},
                ).fetchall()
                allowed_pages = [r.page_key for r in page_rows]
        finally:
            db.close()

        if not row or not row.is_active:
            return jsonify({"error": "Unauthorized"}), 401

        # Reject tokens issued before the last password change
        if row.password_changed_at and payload.get("iat"):
            changed_ts = int(row.password_changed_at.timestamp())
            if payload["iat"] < changed_ts:
                return jsonify({"error": "Token invalidated by password change"}), 401

        # Overwrite JWT claims with live DB values so downstream role/warehouse
        # checks always reflect the current state.
        payload["role"] = row.role
        payload["warehouse_ids"] = list(row.warehouse_ids) if row.warehouse_ids else []
        payload["allowed_pages"] = allowed_pages  # None for ADMIN, list for USER

        g.current_user = payload

        # Forced password change: when the flag is set the user can only
        # hit change-password / logout / me. Everything else is 403 until
        # the flag clears. Matched by Flask endpoint (blueprint.view_fn),
        # not URL path, so query strings and method variants cannot slip
        # past.
        if row.must_change_password and request.endpoint not in FORCED_CHANGE_ALLOWED_ENDPOINTS:
            return jsonify({
                "error": "password_change_required",
                "message": "Admin must change password before accessing other resources",
            }), 403

        # Warehouse authorization: non-admin users can only access assigned
        # warehouses.
        #
        # V-033 added reading warehouse_id from URL path parameters. V-103
        # hardens that against source-mismatch smuggling: the handler's
        # function argument comes from view_args, so an attacker who sets
        # warehouse_id in the body to an allowed value while targeting a
        # different warehouse in the path would previously slip past the
        # middleware (body took priority) and still hit the path value in
        # the handler. The middleware now collects every source the caller
        # supplied and rejects mismatches with 400 before the handler runs.
        # If all sources agree, the common value is used for the allow-list
        # check. If no source is present, no check runs (the route is
        # warehouse-agnostic).
        if payload.get("role") != "ADMIN":
            allowed = payload.get("warehouse_ids") or []

            candidates: list[tuple[str, object]] = []
            if request.view_args and request.view_args.get("warehouse_id") is not None:
                candidates.append(("path", request.view_args["warehouse_id"]))
            query_wid_raw = request.args.get("warehouse_id")
            if query_wid_raw is not None:
                candidates.append(("query", query_wid_raw))
            if request.is_json:
                body = request.get_json(silent=True)
                if body and body.get("warehouse_id") is not None:
                    candidates.append(("body", body["warehouse_id"]))

            req_wid_int: int | None = None
            if candidates:
                try:
                    normalized = {src: int(v) for src, v in candidates}
                except (TypeError, ValueError):
                    return jsonify({"error": "Invalid warehouse_id"}), 400
                if len(set(normalized.values())) > 1:
                    return jsonify({
                        "error": "warehouse_id mismatch across request",
                        "sources": normalized,
                    }), 400
                req_wid_int = next(iter(normalized.values()))

            if req_wid_int is not None and req_wid_int not in allowed:
                return jsonify({"error": "Access denied for this warehouse"}), 403

        return f(*args, **kwargs)

    return decorated


def check_warehouse_access(warehouse_id):
    """Check if the current user has access to the given warehouse.

    Call after loading a resource to verify the user is authorized
    for that resource's warehouse. Returns (False, response) if denied,
    (True, None) if allowed.
    """
    user = g.current_user
    if user.get("role") == "ADMIN":
        return True, None
    allowed = user.get("warehouse_ids") or []
    if warehouse_id is not None and int(warehouse_id) not in allowed:
        return False, (jsonify({"error": "Access denied for this warehouse"}), 403)
    return True, None


def warehouse_scope_clause(column: str = "warehouse_id") -> tuple[str, dict]:
    """Return (SQL fragment, params) that scopes a query to the user's warehouses.

    Call this when building a SELECT whose existence must not leak across
    warehouse boundaries. For non-admin users, the fragment ``AND col = ANY(:_wscope)``
    is returned along with the matching parameter binding. For admins,
    an empty fragment and no params are returned (admins see all).

    Prefer this over ``check_warehouse_access`` when the concern is
    avoiding an existence oracle -- filtering in SQL means "does not
    exist" and "exists in a different warehouse" produce the same empty
    result set and therefore the same 404. See V-026.

    Args:
        column: SQL expression (with optional table alias) for the
                warehouse_id column, e.g. ``"po.warehouse_id"``.

    Returns:
        (fragment, params). Fragment is either an empty string or
        "AND <column> = ANY(:_wscope)". Params is either {} or
        {"_wscope": [warehouse_ids]}.
    """
    user = g.current_user
    if user.get("role") == "ADMIN":
        return "", {}
    allowed = list(user.get("warehouse_ids") or [])
    return f"AND {column} = ANY(:_wscope)", {"_wscope": allowed}


def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.current_user["role"] not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)

        return decorated

    return decorator


def has_override(override_key: str) -> bool:
    """avid-overhaul-mk1 P7: feature-flag grant lookup.

    Returns True iff the user explicitly holds override_key in their
    allowed_pages list. Unlike @require_admin_or_page_permission, an
    ADMIN role does NOT auto-pass: the PO CLOSED/ARCHIVED gate that
    existed pre-P7 is a deliberate safety net (an admin re-opens via
    the status dropdown before resuming line work, preserving a clean
    audit trail). Overrides are the new lower-bar path for non-admins
    who need to bypass that gate; ADMIN keeps the original behavior.

    Read-only: does not 403 on its own. Callers branch on the
    boolean to either widen the allowed-status set or surface a
    targeted 403 with their own message (preserves the existing
    handler-controlled error responses).
    """
    user = g.current_user
    allowed = user.get("allowed_pages")
    if allowed is None:
        return False
    return override_key in allowed


def require_admin_or_page_permission(page_key):
    """avid-overhaul-mk1 P6.1: web-admin permission gate.

    ADMIN bypasses the check (g.current_user["allowed_pages"] is None).
    Any other role must carry the page_key in their allowed_pages
    list (populated at auth time from user_page_permissions),
    otherwise the request is rejected with 403 + {error:
    "Permission denied", page_key: "..."} so the frontend can
    surface a clean "Permissions Error" popup.

    Drop-in replacement for @require_role("ADMIN") - applied in the
    same decorator slot (after @require_auth, before @validate_body
    / @with_db).
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = g.current_user
            allowed = user.get("allowed_pages")
            # ADMIN: allowed is None (sentinel) -> pass.
            # USER:  allowed is a list; page_key must be in it.
            if allowed is None or page_key in allowed:
                return f(*args, **kwargs)
            return jsonify({
                "error": "Permission denied",
                "page_key": page_key,
            }), 403

        return decorated

    return decorator


# v1.5.1 V-201 (#142): the v1.5.0 guard rejected only unset / empty
# pepper values; a 1-byte pepper passed silently. A weak pepper
# collapses the precomputation defense pepper exists to provide.
# Cross-site consistency here ensures the boot guard (app.py), the
# request-time hasher (_load_pepper), and the admin issuance hasher
# (admin_tokens._hash_for_storage) all reject the same bad inputs.
_PEPPER_PLACEHOLDER = "replace-me-with-secrets-token-hex-32"
_MIN_PEPPER_CHARS = 32


def validate_pepper_config(raw) -> bytes:
    """Return the UTF-8 bytes of ``raw`` or raise RuntimeError when
    the value is unset, empty, whitespace-only, the ``.env.example``
    placeholder, or shorter than 32 characters.

    The returned bytes are the exact inbound bytes so existing
    wms_tokens.token_hash values computed from a well-formed pepper
    continue to match after upgrade; this helper is a gate, not a
    normaliser.
    """
    if raw is None or raw == "":
        raise RuntimeError(
            "SENTRY_TOKEN_PEPPER environment variable is required for "
            "X-WMS-Token auth. Generate with "
            "python -c 'import secrets; print(secrets.token_hex(32))' "
            "(see .env.example)."
        )
    if not raw.strip():
        raise RuntimeError(
            "SENTRY_TOKEN_PEPPER is whitespace-only. Generate a real "
            "value with python -c 'import secrets; print(secrets.token_hex(32))'."
        )
    if raw == _PEPPER_PLACEHOLDER or raw.strip() == _PEPPER_PLACEHOLDER:
        raise RuntimeError(
            "SENTRY_TOKEN_PEPPER is set to the .env.example placeholder "
            "string. Generate a real value with "
            "python -c 'import secrets; print(secrets.token_hex(32))'."
        )
    if len(raw) < _MIN_PEPPER_CHARS:
        raise RuntimeError(
            f"SENTRY_TOKEN_PEPPER must be at least {_MIN_PEPPER_CHARS} "
            f"characters (got {len(raw)}). Generate with "
            "python -c 'import secrets; print(secrets.token_hex(32))'."
        )
    return raw.encode("utf-8")


def _load_pepper() -> bytes:
    """Read SENTRY_TOKEN_PEPPER from the environment or raise.

    Looked up lazily (first request, not module import) so importing
    this module from unit tests does not require the env var; the
    app-level boot guard in ``app.py`` raises at ``create_app`` time
    for real deployments. v1.5.1 (#142) extends the guard with
    length + placeholder checks.
    """
    return validate_pepper_config(os.environ.get("SENTRY_TOKEN_PEPPER"))


def _hash_token(raw: str) -> str:
    """token_hash per Decision Q: SHA256(pepper || plaintext).hexdigest()."""
    return hashlib.sha256(_load_pepper() + raw.encode("utf-8")).hexdigest()


# v1.5.1 V-200 (#140): map user-facing endpoint slugs to the Flask
# endpoint names @require_wms_token sees at request time. The
# wms_tokens.endpoints column stores slug form (what admins type and
# see in the UI); the decorator maps to request.endpoint for the
# scope check. Adding a new /api/v1/* route means adding one entry
# here plus updating the admin UI helper text. Slugs are a stable
# wire surface; Flask endpoint names may change if blueprints are
# renamed, so the public contract lives on the slug side.
V150_ENDPOINT_SLUGS = {
    "events.poll":       "polling.poll_events",
    "events.ack":        "polling.ack_cursor",
    "events.types":      "polling.list_event_types",
    "events.schema":     "polling.serve_schema",
    "snapshot.inventory": "snapshot.snapshot_inventory",
}

# v1.7.0 Pipe B: inbound POST routes do NOT use the V150 endpoint-slug
# scope; they use the wms_tokens.inbound_resources array (Decision-S
# alignment, separate scope dimension from event_types). Map the Flask
# endpoint name to the canonical resource key the token must list.
# Adding a new /api/v1/inbound/* route means adding one entry here plus
# wiring it in the admin UI's inbound-resources checkbox group.
V170_INBOUND_RESOURCE_BY_ENDPOINT = {
    "inbound.post_sales_orders":     "sales_orders",
    "inbound.post_items":            "items",
    "inbound.post_customers":        "customers",
    "inbound.post_vendors":          "vendors",
    "inbound.post_purchase_orders":  "purchase_orders",
    # v1.8.x cutover-prep extension: state-based inventory sync from upstream
    # (cutover seed, FBA mirror, drift correction). See routes/inbound.py
    # _inventory_update_post + register_inventory_update_route.
    "inbound.post_inventory_update": "inventory_update",
}


_V150_FLASK_ENDPOINTS = frozenset(V150_ENDPOINT_SLUGS.values())


# v1.9.0 dockd: one slug covers all dockd routes. Admins see and select
# a single capability ("dockd.dispatch") in the token UI; the decorator
# matches request.endpoint against the frozenset below. Adding a fourth
# dockd route adds one Flask endpoint name; the slug stays the same so
# token UX does not churn. Encoded separately from V150_ENDPOINT_SLUGS
# (1:1 outbound polling) and V170_INBOUND_RESOURCE_BY_ENDPOINT (1:1
# inbound resource) because dockd's 1:N slug shape is its own model.
V190_DOCKD_SLUG = "dockd.dispatch"

_V190_DOCKD_FLASK_ENDPOINTS = frozenset({
    "dockd.get_order",
    "dockd.ship_order",
    "dockd.void_ship",
})


# v1.10.0 POS: same 1:N slug shape as dockd. One slug ("pos.dispatch")
# covers the four counter-sale + refund routes. The constant name
# follows the V150 / V170 / V190 digit-concat convention; "1100"
# decodes as v1.10.0+. POS is a fourth direction alongside outbound
# (V150), inbound (V170), and dockd (V190); a POS token does POS,
# period, with no cross-direction bridging. Encoded separately so a
# future v1.10.x route addition lands one Flask endpoint name without
# touching any other surface.
V1100_POS_SLUG = "pos.dispatch"

_V1100_POS_FLASK_ENDPOINTS = frozenset({
    "pos.availability",
    "pos.validate_cart",
    "pos.checkout",
    "pos.refund",
})


def _is_inbound_request(flask_endpoint: Optional[str], path: str) -> bool:
    if flask_endpoint and flask_endpoint in V170_INBOUND_RESOURCE_BY_ENDPOINT:
        return True
    return path.startswith("/api/v1/inbound/")


def _is_outbound_request(flask_endpoint: Optional[str], path: str) -> bool:
    """v1.5 outbound surface: polling + snapshot. The decorator's
    cross-direction guard refuses an inbound-only token (no event_types,
    has inbound_resources) reaching this surface.

    Recognised by Flask endpoint name (production routes register under
    polling.* / snapshot.*) OR by path prefix (covers test probes
    registered under a non-prefixed URL but the production endpoint
    name; see test_wms_token_decorator.probe_app)."""
    if flask_endpoint and flask_endpoint in _V150_FLASK_ENDPOINTS:
        return True
    return path.startswith("/api/v1/events") or path.startswith("/api/v1/snapshot")


def _is_dockd_request(flask_endpoint: Optional[str], path: str) -> bool:
    """v1.9.0 dockd surface. Mirrors _is_outbound_request: matches by
    Flask endpoint name OR /api/v1/dockd/ path prefix. The path branch
    covers test probes that register under arbitrary URLs but real
    production endpoint names."""
    if flask_endpoint and flask_endpoint in _V190_DOCKD_FLASK_ENDPOINTS:
        return True
    return path.startswith("/api/v1/dockd/")


def _is_pos_request(flask_endpoint: Optional[str], path: str) -> bool:
    """v1.10.0 POS surface. Same shape as _is_dockd_request: matches
    by Flask endpoint name OR /api/v1/pos/ path prefix so a test probe
    registered under a non-prefixed URL still hits the POS scope branch."""
    if flask_endpoint and flask_endpoint in _V1100_POS_FLASK_ENDPOINTS:
        return True
    return path.startswith("/api/v1/pos/")


def require_wms_token(f):
    """Gate the decorated endpoint on a valid X-WMS-Token header.

    Applied only to v1.5.0 /api/v1/events* and /api/v1/snapshot/*
    routes. Cookie-auth routes keep @require_auth; the two decorators
    do not interact.

    On success:
    - ``g.current_token`` = the full wms_tokens row as a dict
    - ``g.current_user`` = a sentinel {"token_id": ..., "kind": "wms_token"}
      so downstream rate-limit keys and audit logs have something to
      attribute the request to without conflating with cookie users.

    Failures (v1.5.1 V-209 (#149) -- unified wire shape):
    - 401 ``{"error":"invalid_token"}`` for every auth failure:
      missing header, unknown hash, revoked row, expired row.
      Pre-v1.5.1 the decorator returned four distinct bodies
      (missing_token, invalid_token, token_expired, plus revoked
      coming out as invalid_token); the differentiation was an
      enumeration oracle letting an attacker separate "guessed a
      real token's shape" (expired) from "guessed nothing" (missing
      / invalid). The specific reason stays in a DEBUG log for
      operator forensics.
    - 403 ``endpoint_scope_violation`` when the token's endpoints
      slug list does not include the route (kept distinct because
      403 is a different HTTP semantic from 401 and the auth check
      already succeeded).
    - 403 ``cross_direction_scope_violation`` (v1.7.0) when the
      token tries to cross the inbound / outbound boundary: an
      inbound-only token (has inbound_resources, no event_types)
      reaching /api/v1/events* or /api/v1/snapshot/*, or an
      outbound-only token (no source_system) reaching
      /api/v1/inbound/*. Distinct error_kind from
      endpoint_scope_violation so audit and rate-limit dashboards
      can separate "wrong slug" from "wrong direction".
    - 403 ``inbound_resource_scope_violation`` (v1.7.0) when an
      inbound token's inbound_resources array does not list the
      target resource for an /api/v1/inbound/<resource> route.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        raw = request.headers.get("X-WMS-Token")
        if not raw:
            # v1.5.1 V-209: flatten timing of the missing-header
            # path by doing the same get_by_hash a real attempt
            # would trigger. The lookup uses a sentinel hash that
            # cannot exist in the DB so no real row ever matches.
            token_cache.get_by_hash("0" * 64)
            _INVALID_LOGGER.debug("wms_token: missing header")
            return jsonify({"error": "invalid_token"}), 401
        token_hash = _hash_token(raw)
        row = token_cache.get_by_hash(token_hash)
        if not row:
            _INVALID_LOGGER.debug("wms_token: unknown hash")
            return jsonify({"error": "invalid_token"}), 401
        if row["status"] != "active":
            _INVALID_LOGGER.debug(
                "wms_token: status=%s for token_id=%s",
                row["status"], row.get("token_id"),
            )
            return jsonify({"error": "invalid_token"}), 401
        # v1.7.0 #278: also reject when revoked_at is set, regardless of
        # status. Pre-fix, a direct-DB write of the form
        # `UPDATE wms_tokens SET revoked_at = NOW()` -- without also
        # setting status='revoked' -- produced a row that the status
        # check above let through. Mig 048's trigger now flips status
        # in lock-step on the same UPDATE, so in normal operation this
        # second condition is redundant. It stays as defense-in-depth:
        # if a future schema or trigger change drops the lock-step
        # behavior, this gate still de-authenticates the token.
        if row.get("revoked_at") is not None:
            _INVALID_LOGGER.debug(
                "wms_token: revoked_at populated for token_id=%s",
                row.get("token_id"),
            )
            return jsonify({"error": "invalid_token"}), 401
        if row.get("expires_at") and datetime.now(timezone.utc) > row["expires_at"]:
            _INVALID_LOGGER.debug(
                "wms_token: expired token_id=%s", row.get("token_id")
            )
            return jsonify({"error": "invalid_token"}), 401

        # v1.7.0 Pipe B / v1.9.0 dockd / v1.10.0 POS: route the scope
        # check based on which surface the request hit. Inbound POST
        # routes use the inbound_resources array. Outbound polling /
        # snapshot routes use the V150 slug list. Dockd routes use the
        # V190 single slug. POS routes use the V1100 single slug.
        is_inbound = _is_inbound_request(request.endpoint, request.path)
        is_outbound = _is_outbound_request(request.endpoint, request.path)
        is_dockd = _is_dockd_request(request.endpoint, request.path)
        is_pos = _is_pos_request(request.endpoint, request.path)

        if is_inbound:
            # Cross-direction: an inbound POST requires both a
            # source_system binding and at least one inbound_resources
            # entry. Outbound-only tokens (source_system NULL,
            # inbound_resources empty) are refused at the boundary
            # without leaking which dimension was missing.
            if not row.get("source_system") or not row.get("inbound_resources"):
                return jsonify({"error": "cross_direction_scope_violation"}), 403
            target = V170_INBOUND_RESOURCE_BY_ENDPOINT.get(request.endpoint)
            if target is None or target not in (row.get("inbound_resources") or []):
                return jsonify({"error": "inbound_resource_scope_violation"}), 403
        elif is_outbound:
            # Inbound-only token (has inbound_resources, no event_types)
            # cannot read outbound. Tokens with both directions present
            # fall through to the V150 slug check below.
            if (
                row.get("inbound_resources")
                and not row.get("event_types")
            ):
                return jsonify({"error": "cross_direction_scope_violation"}), 403
            # v1.5.1 V-200 endpoint-slug enforcement (unchanged).
            allowed_flask = {
                V150_ENDPOINT_SLUGS[slug]
                for slug in (row.get("endpoints") or [])
                if slug in V150_ENDPOINT_SLUGS
            }
            if request.endpoint not in allowed_flask:
                return jsonify({"error": "endpoint_scope_violation"}), 403
        elif is_dockd:
            # v1.9.0 dockd is its own direction. A token reaching this
            # surface must carry the dockd.dispatch slug AND must NOT
            # have inbound markers (source_system / inbound_resources)
            # or outbound markers (event_types). Mixed-direction tokens
            # are explicitly disallowed; a dockd token does dockd, period.
            if (
                row.get("source_system")
                or row.get("inbound_resources")
                or row.get("event_types")
            ):
                return jsonify({"error": "cross_direction_scope_violation"}), 403
            if V190_DOCKD_SLUG not in (row.get("endpoints") or []):
                return jsonify({"error": "endpoint_scope_violation"}), 403
            if request.endpoint not in _V190_DOCKD_FLASK_ENDPOINTS:
                # Path-prefix matched but no Flask endpoint claim: a
                # registration bug, fail closed. Mirrors the V-200
                # fail-closed posture for unmapped endpoints.
                return jsonify({"error": "endpoint_scope_violation"}), 403
        elif is_pos:
            # v1.10.0 POS is a fourth direction. Same posture as dockd:
            # the token must carry pos.dispatch AND must NOT carry any
            # outbound (event_types) or inbound (source_system /
            # inbound_resources) markers. A POS token does POS, period.
            if (
                row.get("source_system")
                or row.get("inbound_resources")
                or row.get("event_types")
            ):
                return jsonify({"error": "cross_direction_scope_violation"}), 403
            if V1100_POS_SLUG not in (row.get("endpoints") or []):
                return jsonify({"error": "endpoint_scope_violation"}), 403
            if request.endpoint not in _V1100_POS_FLASK_ENDPOINTS:
                # Path-prefix matched but no Flask endpoint claim: a
                # registration bug, fail closed. Same posture as the
                # dockd branch above.
                return jsonify({"error": "endpoint_scope_violation"}), 403
        else:
            # Unknown @require_wms_token-protected route. Fail closed:
            # adding a route under @require_wms_token without claiming
            # one of the surface prefixes is a wiring bug. This mirrors
            # the V-200 fail-closed posture for unmapped endpoint slugs.
            return jsonify({"error": "endpoint_scope_violation"}), 403

        g.current_token = row
        g.current_user = {"token_id": row["token_id"], "kind": "wms_token"}
        return f(*args, **kwargs)

    # v1.7.0 CI lint marker: the inbound-route lint walks each
    # @require_wms_token-protected view's wrapper chain looking for
    # this attribute so a new POST landing without the decorator
    # surfaces at CI time. functools.wraps copies __qualname__ from
    # the wrapped function, so the chain alone doesn't reveal the
    # decorator was applied; an explicit attribute does.
    wrapper.__wms_token_protected__ = True
    return wrapper
