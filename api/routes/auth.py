"""
Auth endpoints: login and token refresh.
"""

from datetime import datetime, timezone, timedelta

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from middleware.auth_middleware import require_auth
from middleware.db import with_db
from schemas.auth import ChangePasswordRequest, LoginRequest
from services.auth_service import authenticate_user, decode_token, generate_token, validate_password
from services.cookie_auth import (
    AUTH_COOKIE_NAME,
    clear_auth_cookies,
    csrf_token_matches,
    generate_csrf_token,
    set_auth_cookies,
)
from utils.validation import validate_body

ALL_FUNCTIONS = ["receive", "putaway", "pick", "pack", "ship", "count", "transfer"]

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
# V-024: cap login_attempts.key at 64 chars so an attacker cannot bloat
# the table by spraying long random usernames. Anything longer is SHA-256
# hashed (hex digest = 64 chars) before it reaches the DB.
LOGIN_ATTEMPT_KEY_MAX_LEN = 64

auth_bp = Blueprint("auth", __name__)


def _normalize_rate_limit_key(key: str) -> str:
    if len(key) <= LOGIN_ATTEMPT_KEY_MAX_LEN:
        return key
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _check_rate_limit(db, key):
    """Check if a rate-limit key is locked out. Returns (locked, remaining_seconds)."""
    key = _normalize_rate_limit_key(key)
    row = db.execute(
        text("SELECT attempts, locked_until FROM login_attempts WHERE key = :key"),
        {"key": key},
    ).fetchone()
    if not row or not row.locked_until:
        return False, 0
    now = datetime.now(timezone.utc)
    if row.locked_until > now:
        remaining = int((row.locked_until - now).total_seconds())
        return True, remaining
    return False, 0


def _record_failure(db, key, allow_lockout):
    """Record a failed login attempt against ``key``.

    V-023: only keys that are allowed to cause lockout (IP keys) ever
    set ``locked_until``. User-scoped keys (``user:<name>``) still
    increment the attempts counter for observability but never lock
    the account -- an attacker from one IP cannot lock out the real
    user, who may be logging in from a different IP.

    Returns (locked_out, attempts_remaining). ``locked_out`` is only
    True when ``allow_lockout`` is also True and the key has crossed
    the threshold.
    """
    key = _normalize_rate_limit_key(key)
    lockout_at = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
    db.execute(
        text("""
            INSERT INTO login_attempts (key, attempts, last_attempt)
            VALUES (:key, 1, NOW())
            ON CONFLICT (key) DO UPDATE
            SET attempts = login_attempts.attempts + 1, last_attempt = NOW()
        """),
        {"key": key},
    )
    row = db.execute(
        text("SELECT attempts FROM login_attempts WHERE key = :key"),
        {"key": key},
    ).fetchone()
    if allow_lockout and row and row.attempts >= MAX_LOGIN_ATTEMPTS:
        db.execute(
            text("UPDATE login_attempts SET locked_until = :until, attempts = 0 WHERE key = :key"),
            {"key": key, "until": lockout_at},
        )
        db.commit()
        return True, 0
    db.commit()
    return False, MAX_LOGIN_ATTEMPTS - (row.attempts if row else 0)


def _reset_attempts(db, key):
    """Clear attempts after successful login."""
    key = _normalize_rate_limit_key(key)
    db.execute(
        text("DELETE FROM login_attempts WHERE key = :key"),
        {"key": key},
    )
    db.commit()


@auth_bp.route("/login", methods=["POST"])
@validate_body(LoginRequest)
@with_db
def login(validated):
    username = validated.username.lower().strip()
    client_ip = request.remote_addr or "unknown"
    user_key = f"user:{username}"
    ip_key = f"ip:{client_ip}"

    # V-023: only the IP is allowed to cause a lockout. An attacker from
    # one IP spamming wrong passwords for a known username no longer
    # locks the real user out of other IPs. User-scoped attempts are
    # still counted for observability but do not set locked_until.
    locked, remaining = _check_rate_limit(g.db, ip_key)
    if locked:
        minutes = remaining // 60
        seconds = remaining % 60
        return jsonify({
            "error": f"Too many failed attempts from your IP. Try again in {minutes}m {seconds}s",
        }), 429

    user = authenticate_user(g.db, validated.username, validated.password)

    if not user:
        # Record failure against both keys, but only the IP key is
        # allowed to trip the lockout threshold.
        _record_failure(g.db, user_key, allow_lockout=False)
        locked, _ = _record_failure(g.db, ip_key, allow_lockout=True)
        if locked:
            return jsonify({
                "error": "Too many failed attempts from your IP. Locked for 15 minutes",
            }), 429
        return jsonify({
            "error": "Invalid username or password",
        }), 401

    # Successful login - reset both trackers
    _reset_attempts(g.db, user_key)
    _reset_attempts(g.db, ip_key)
    token = generate_token(user)
    # V-045: dual-path auth. The token is returned in the body (mobile)
    # and also set as HttpOnly + CSRF cookies (admin SPA).
    csrf = generate_csrf_token()
    response = jsonify({"token": token, "user": user})
    set_auth_cookies(response, token, csrf)
    return response


@auth_bp.route("/logout", methods=["POST"])
def logout():
    # V-100: the previous version cleared cookies on every POST, which
    # meant an attacker-origin form submission could force a victim's
    # browser to apply expired cookies and end the session. SameSite=Strict
    # prevents the victim's auth cookie from being sent cross-origin but
    # does not stop the response's Set-Cookie from being applied.
    #
    # Current shape:
    #   - No auth cookie on the request -> 200 no-op, no Set-Cookie.
    #     Cross-origin attacker (SameSite=Strict stripped the cookie) and
    #     idempotent cleanup calls both land here.
    #   - Valid auth cookie -> require CSRF match; clear cookies on match,
    #     reject with 403 otherwise. A same-origin XSS can already hijack
    #     the session directly; the CSRF gate here exists to block any
    #     path that somehow exposes the auth cookie without the CSRF.
    #   - Expired / invalid auth cookie -> clear cookies silently. The
    #     session is already dead, so stale-cleanup keeps working without
    #     demanding a CSRF token the client no longer has.
    response = jsonify({"message": "logged out"})
    auth_cookie = request.cookies.get(AUTH_COOKIE_NAME)
    if not auth_cookie:
        return response
    payload = decode_token(auth_cookie)
    if payload is not None and not csrf_token_matches():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    clear_auth_cookies(response)
    return response


@auth_bp.route("/me")
@require_auth
@with_db
def me():
    user_id = g.current_user["user_id"]
    row = g.db.execute(
        text("SELECT user_id, username, full_name, role, warehouse_id, allowed_functions FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404

    if row.role == "ADMIN":
        functions = list(ALL_FUNCTIONS)
    else:
        functions = list(row.allowed_functions) if row.allowed_functions else []

    # Check packing toggle  -  filter out "pack" when packing is disabled
    packing_row = g.db.execute(
        text("SELECT value FROM app_settings WHERE key = 'require_packing_before_shipping'")
    ).fetchone()
    require_packing = not packing_row or packing_row.value != "false"

    if not require_packing:
        functions = [f for f in functions if f != "pack"]

    return jsonify({
        "user_id": row.user_id,
        "username": row.username,
        "full_name": row.full_name,
        "role": row.role,
        "warehouse_id": row.warehouse_id,
        "allowed_functions": functions,
        "require_packing": require_packing,
    })


@auth_bp.route("/refresh", methods=["POST"])
@require_auth
@with_db
def refresh():
    # Re-validate user exists and is active before issuing new token
    row = g.db.execute(
        text("""SELECT user_id, username, full_name, role, warehouse_id, warehouse_ids, is_active
               FROM users WHERE user_id = :uid"""),
        {"uid": g.current_user["user_id"]},
    ).fetchone()
    if not row or not row.is_active:
        return jsonify({"error": "Account disabled or deleted"}), 401

    user_dict = {
        "user_id": row.user_id,
        "username": row.username,
        "full_name": row.full_name,
        "role": row.role,
        "warehouse_id": row.warehouse_id,
        "warehouse_ids": list(row.warehouse_ids) if row.warehouse_ids else [],
    }
    token = generate_token(user_dict)
    csrf = generate_csrf_token()
    response = jsonify({"token": token})
    set_auth_cookies(response, token, csrf)
    return response


@auth_bp.route("/change-password", methods=["POST"])
@require_auth
@validate_body(ChangePasswordRequest)
@with_db
def change_password(validated):
    import bcrypt
    from services.audit_service import write_audit_log

    pw_error = validate_password(validated.new_password)
    if pw_error:
        return jsonify({"error": pw_error}), 400

    user_id = g.current_user["user_id"]
    row = g.db.execute(
        text(
            "SELECT password_hash, must_change_password, username, warehouse_id "
            "FROM users WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()

    if not row or not bcrypt.checkpw(validated.current_password.encode("utf-8"), row.password_hash.encode("utf-8")):
        return jsonify({"error": "Current password is incorrect"}), 403

    was_forced = bool(row.must_change_password)

    new_hash = bcrypt.hashpw(validated.new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    g.db.execute(
        text(
            "UPDATE users SET password_hash = :pw, password_changed_at = NOW(), "
            "must_change_password = FALSE WHERE user_id = :uid"
        ),
        {"pw": new_hash, "uid": user_id},
    )

    # Distinct action name when the change satisfied a forced-change flag so
    # operators can grep the audit log for onboarding events separately from
    # voluntary password rotations.
    write_audit_log(
        g.db,
        action_type="forced_password_change_completed" if was_forced else "password_change",
        entity_type="user",
        entity_id=user_id,
        user_id=row.username,
        warehouse_id=row.warehouse_id,
    )

    g.db.commit()

    return jsonify({"message": "Password changed"})
