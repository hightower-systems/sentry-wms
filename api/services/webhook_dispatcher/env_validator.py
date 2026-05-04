"""Boot-time env-var validators for the webhook dispatcher (plan §2.10).

Mirrors the V-201 #142 weak-pepper validation shape and the V-206
#147 dangerous-combination boot guard shape from v1.5.1. Each
range validator rejects out-of-band values with a clear message
naming the offending var and its allowed range; each combination
guard refuses to boot (or logs CRITICAL on every boot, in the
softer cases) when two safe values combine into a dangerous one.

The dispatcher's daemon entry point calls ``validate_or_die()`` at
boot. A misconfigured deployment fails fast with a message naming
the variable to fix; deployments that pass keep the env values
available via :func:`int_var` / :func:`bool_var` helpers that
re-read on every call (so a runtime toggle takes effect on the
next read, mirroring the V-217 #156 lesson on module-level env
reads -- never freeze a tunable at import).
"""

import logging
import os
import sys
from typing import Optional


LOGGER = logging.getLogger("webhook_dispatcher.env_validator")


# (var_name, lower_bound, upper_bound, default_value)
_RANGE_VARS = [
    ("DISPATCHER_HTTP_TIMEOUT_MS", 1000, 60000, 10000),
    # #237: connect + read are per-operation caps requests passes
    # through to urllib3. The wall-clock cap above fires first
    # when a consumer drip-feeds bytes within the per-op budget;
    # the per-op caps still matter for connect + send phases
    # before the body read starts. Defaults: 5s connect + 8s read,
    # both inside the 10s wall-clock cap.
    ("DISPATCHER_HTTP_CONNECT_TIMEOUT_MS", 100, 60000, 5000),
    ("DISPATCHER_HTTP_READ_TIMEOUT_MS", 100, 60000, 8000),
    ("DISPATCHER_FALLBACK_POLL_MS", 500, 10000, 2000),
    ("DISPATCHER_SHUTDOWN_DRAIN_S", 1, 300, 30),
    ("DISPATCHER_MAX_CONCURRENT_POSTS", 1, 100, 16),
    ("DISPATCHER_MAX_PENDING_HARD_CAP", 1000, 10_000_000, 50_000),
    ("DISPATCHER_MAX_DLQ_HARD_CAP", 100, 1_000_000, 5_000),
]


# #212: env vars that MUST be set when the dispatcher is enabled.
# Pre-212 the dispatcher booted cleanly without REDIS_URL and the
# cross-worker invalidation publisher silently no-op'd; peer
# workers then ran on stale subscription state until their next
# 60s refresh. Required-env validation closes the gap at boot.
# DISPATCHER_ENABLED=false skips this guard so the kill switch
# stays usable even without a Redis instance.
_REQUIRED_VARS_WHEN_ENABLED = [
    (
        "REDIS_URL",
        "REDIS_URL is required when the dispatcher is enabled. The "
        "admin handlers publish cross-worker invalidation messages on "
        "this URL; an unset value silently no-ops every publish and "
        "leaves peer workers on stale subscription state. Set REDIS_URL "
        "in .env (or set DISPATCHER_ENABLED=false to use the kill "
        "switch). Same authenticated URL shape as CELERY_BROKER_URL.",
    ),
    # #227: HMAC key for the cross-worker pubsub envelope. An unset
    # or trivial value lets a Redis-side attacker forge subscription_event
    # messages (eviction storms, spammed secret_rotated). The full weak-
    # key validation (placeholder shape, length floor) lives in
    # pubsub_signing.load_key; this guard fires fast at boot so the
    # daemon refuses to come up unconfigured.
    (
        "SENTRY_PUBSUB_HMAC_KEY",
        "SENTRY_PUBSUB_HMAC_KEY is required when the dispatcher is "
        "enabled. The webhook_subscription_events Redis channel is "
        "HMAC-signed (#227); an unset key would let a Redis-side "
        "attacker forge eviction or secret_rotated messages. "
        "Generate with: python -c \"import secrets; "
        "print(secrets.token_hex(32))\"",
    ),
]


class DispatcherEnvError(RuntimeError):
    """Raised when env validation refuses to boot. Caller (the
    daemon entry point) catches this, logs the message, and
    exits non-zero so deployment automation flags the failure.
    Subclasses RuntimeError so an unexpected raise during a non-
    boot context also surfaces with a clear stack."""


def _read_str(name: str) -> Optional[str]:
    """Always re-reads from os.environ; never caches at import.
    Returns the raw string or None if unset. Empty strings are
    treated as set so a deliberately-cleared value is visible to
    the caller's "is the value sane?" check."""
    return os.environ.get(name)


def bool_var(name: str, default: bool = False) -> bool:
    """Re-read a boolean env var on every call. Only the literal
    lowercase string ``true`` (case-insensitive comparison after
    lowercase) returns True; anything else falls back to the
    default. Conservative because a bool-flag typo should not
    silently flip a security gate (e.g. SENTRY_ALLOW_HTTP_WEBHOOKS)."""
    raw = _read_str(name)
    if raw is None:
        return default
    return raw.lower() == "true"


def int_var(name: str) -> int:
    """Return the int value of a range-validated env var. The var
    must have passed validate_or_die already; otherwise the int()
    conversion raises ValueError. Defaults are baked into
    _RANGE_VARS so an unset var resolves to the documented default
    rather than the validator's lower bound."""
    raw = _read_str(name)
    if raw is None or raw == "":
        for var_name, _lo, _hi, default in _RANGE_VARS:
            if var_name == name:
                return default
        raise DispatcherEnvError(
            f"{name} is not a registered range-validated env var; "
            f"add it to _RANGE_VARS in env_validator.py"
        )
    return int(raw)


def _validate_range(name: str, lo: int, hi: int) -> None:
    raw = _read_str(name)
    if raw is None or raw == "":
        # Unset -> use default (registered in _RANGE_VARS); skip range check.
        return
    try:
        value = int(raw)
    except ValueError:
        raise DispatcherEnvError(
            f"{name}={raw!r} is not a valid integer; allowed range is "
            f"[{lo}, {hi}]"
        )
    if value < lo or value > hi:
        raise DispatcherEnvError(
            f"{name}={value} is out of range; allowed range is "
            f"[{lo}, {hi}]. The bounds are the floor that catches "
            f"misconfigurations the application code can recover from "
            f"if any tunable were to be unbounded."
        )


def _is_production() -> bool:
    """True when FLASK_ENV resolves to production. Matches the
    V-206 #147 weak-pepper-style check shape."""
    return os.environ.get("FLASK_ENV", "").lower() == "production"


def _validate_combinations() -> None:
    """Refuse boot on the dangerous combinations called out in
    plan §2.10. Each is a separate function so the test suite can
    exercise them independently."""
    allow_http = bool_var("SENTRY_ALLOW_HTTP_WEBHOOKS", default=False)
    allow_internal = bool_var("SENTRY_ALLOW_INTERNAL_WEBHOOKS", default=False)

    # Hardest gate: SSRF-into-VPC by combining http + internal opt-outs.
    # Refuse boot regardless of FLASK_ENV; this is operator-error in
    # any environment because it disables both the scheme guard and
    # the dispatch-time DNS rebinding guard simultaneously.
    if allow_http and allow_internal:
        raise DispatcherEnvError(
            "refusing to boot: SENTRY_ALLOW_HTTP_WEBHOOKS=true AND "
            "SENTRY_ALLOW_INTERNAL_WEBHOOKS=true. The combination opens "
            "an SSRF path into the VPC by relaxing both the HTTPS-only "
            "scheme check and the dispatch-time private-range guard. "
            "Each var has dev/CI use cases on its own; the combination "
            "has no legitimate use case."
        )

    # Production-only refusal: internal-webhook opt-out disables the
    # dispatch-time SSRF guard, which is load-bearing in production.
    if allow_internal and _is_production():
        raise DispatcherEnvError(
            "refusing to boot: SENTRY_ALLOW_INTERNAL_WEBHOOKS=true with "
            "FLASK_ENV=production. The dispatch-time DNS resolution + "
            "private-range reject is the security boundary against "
            "DNS-rebinding and split-horizon DNS attacks; disabling it "
            "in production is operator error."
        )

    # #237: each per-op cap must fit inside the wall-clock cap so
    # the per-op timeouts are not dead defense. A configuration
    # where READ alone exceeds TIMEOUT means the wall-clock fires
    # first on every legitimate slow consumer and the per-op cap
    # is unreachable. Refuse boot when CONNECT or READ exceeds
    # TIMEOUT individually; the SUM constraint is intentionally
    # weaker because connect and read run in different phases of
    # the roundtrip and the wall-clock cap dominates anyway.
    timeout_ms = int_var("DISPATCHER_HTTP_TIMEOUT_MS")
    connect_ms = int_var("DISPATCHER_HTTP_CONNECT_TIMEOUT_MS")
    read_ms = int_var("DISPATCHER_HTTP_READ_TIMEOUT_MS")
    if connect_ms > timeout_ms or read_ms > timeout_ms:
        raise DispatcherEnvError(
            "refusing to boot: each of DISPATCHER_HTTP_CONNECT_TIMEOUT_MS "
            f"({connect_ms}) and DISPATCHER_HTTP_READ_TIMEOUT_MS "
            f"({read_ms}) must be <= DISPATCHER_HTTP_TIMEOUT_MS "
            f"({timeout_ms}). The wall-clock cap dominates; a per-op "
            "cap larger than it is unreachable defense (#237)."
        )

    # Soft warning: HTTPS-only is a hard requirement in production but
    # an opt-out is allowed for emergency rollouts. Log CRITICAL on
    # every boot so the acknowledgement stays visible in
    # ``docker compose logs``; this matches the V-206 #147 escape-
    # hatch shape from v1.5.1.
    if allow_http and _is_production():
        LOGGER.critical(
            "SENTRY_ALLOW_HTTP_WEBHOOKS=true with FLASK_ENV=production: "
            "the dispatcher will accept http:// delivery URLs in this "
            "deployment. This relaxes the v1.6.0 HTTPS-only policy. "
            "Set SENTRY_ALLOW_HTTP_WEBHOOKS=false (or unset) and restart "
            "to re-enable the policy."
        )


def _dispatcher_enabled() -> bool:
    """Mirrors WebhookDispatcher.enabled. Default true; only the
    literal case-insensitive ``false`` disables. Looser values
    ("0", "no", "off", "") do NOT disable so an accidental config
    from another project cannot silently engage the kill switch."""
    return os.environ.get("DISPATCHER_ENABLED", "true").lower() != "false"


def _validate_required() -> None:
    """#212: refuse boot when env vars required for cross-worker
    correctness are unset. Skipped when DISPATCHER_ENABLED=false
    so the kill switch stays usable."""
    if not _dispatcher_enabled():
        return
    for name, message in _REQUIRED_VARS_WHEN_ENABLED:
        raw = _read_str(name)
        if raw is None or raw == "":
            raise DispatcherEnvError(
                f"refusing to boot: {name} is unset. {message}"
            )
    # #227: pubsub HMAC key has additional shape requirements
    # (placeholder rejection, byte-length floor) that pubsub_signing
    # owns. Surface its weak-key error here as a DispatcherEnvError
    # so the daemon entry's catch-and-exit path is uniform.
    from . import pubsub_signing  # noqa: WPS433 -- localised import

    try:
        pubsub_signing.load_key()
    except pubsub_signing.PubsubKeyConfigError as exc:
        raise DispatcherEnvError(f"refusing to boot: {exc}")


def validate_or_die() -> None:
    """Boot guard. Raises DispatcherEnvError on any range,
    required-env, or combination violation; the daemon entry
    catches that and exits non-zero. Logs CRITICAL warnings for
    soft cases (e.g. http opt-out in production) but does not
    refuse boot for those."""
    for name, lo, hi, _default in _RANGE_VARS:
        _validate_range(name, lo, hi)
    _validate_required()
    _validate_combinations()
