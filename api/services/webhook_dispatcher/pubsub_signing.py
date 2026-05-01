"""HMAC envelope for cross-worker pubsub messages (#227).

The webhook_subscription_events Redis channel carries
subscription_id + event_kind tuples that drive eviction, secret
refresh, and HTTP-session teardown across dispatcher workers. Pre-
#227 the channel was unauthenticated; an attacker with publish
rights on the broker could forge messages and trigger silent
operational outages (`event="deleted"`) or DoS amplification
(`event="secret_rotated"` in a tight loop).

This module provides the HMAC-SHA256 envelope: every published
payload is wrapped as ``{"sig": <hex>, "payload": <inner-json>}``
where ``sig`` is the HMAC of ``payload`` keyed on
``SENTRY_PUBSUB_HMAC_KEY``. Subscribers verify with
``hmac.compare_digest`` before dispatching the inner payload to
the wake orchestrator's queue. Unsigned or tampered messages are
logged and dropped.

The key is intentionally separate from SENTRY_ENCRYPTION_KEY
(webhook secret material), SENTRY_TOKEN_PEPPER (inbound auth),
and JWT_SECRET (session signing); each rotates on its own
schedule. Boot validation lives in env_validator.py: an unset or
trivial value refuses dispatcher boot under DISPATCHER_ENABLED=true.
"""

import hashlib
import hmac
import json
import os
from typing import Optional


_KEY_ENV_VAR = "SENTRY_PUBSUB_HMAC_KEY"
_PLACEHOLDER_KEYS = frozenset(
    {
        "replace-me-with-secrets-token-hex-32",
        "replace-me-with-openssl-rand-hex-32",
    }
)
# Minimum byte-length when the key value is treated as raw bytes.
# 32 bytes (== HMAC-SHA256 block boundary, 256 bits) gives full
# preimage resistance for the key; shorter keys reduce the
# resistance and surface as a configuration weakness.
_MIN_KEY_BYTES = 32


class PubsubKeyConfigError(RuntimeError):
    """Raised when the pubsub HMAC key is unset, placeholder, or
    too short. Catches the same shape v1.5.1's pepper validator
    raises so the boot-fail surface stays uniform."""


def load_key() -> bytes:
    """Read SENTRY_PUBSUB_HMAC_KEY on every call (V-217 #156: no
    module-level env reads). Raises PubsubKeyConfigError if the
    value is unset, the .env.example placeholder, or shorter than
    _MIN_KEY_BYTES bytes when interpreted as utf-8."""
    raw = os.environ.get(_KEY_ENV_VAR)
    if raw is None or not raw.strip():
        raise PubsubKeyConfigError(
            f"{_KEY_ENV_VAR} is unset; the dispatcher refuses to "
            "publish or accept cross-worker pubsub messages without "
            "an HMAC key. Generate with: python -c \"import secrets; "
            "print(secrets.token_hex(32))\""
        )
    if raw.strip() in _PLACEHOLDER_KEYS:
        raise PubsubKeyConfigError(
            f"{_KEY_ENV_VAR} is set to the .env.example placeholder; "
            "a deployment with this value would let any attacker "
            "with the same .env.example value forge messages. "
            "Generate a real key with: python -c \"import secrets; "
            "print(secrets.token_hex(32))\""
        )
    encoded = raw.encode("utf-8")
    if len(encoded) < _MIN_KEY_BYTES:
        raise PubsubKeyConfigError(
            f"{_KEY_ENV_VAR} is shorter than {_MIN_KEY_BYTES} bytes; "
            "the dispatcher refuses keys that fall below the "
            "documented entropy floor. Generate with: python -c "
            "\"import secrets; print(secrets.token_hex(32))\""
        )
    return encoded


def canonical_payload(subscription_id: str, event: str) -> str:
    """Serialize the inner payload deterministically. JSON keys are
    sorted so the publisher and subscriber always produce the same
    bytes for the same logical message; an attacker cannot wiggle
    key ordering to invalidate the signature."""
    return json.dumps(
        {"subscription_id": subscription_id, "event": event},
        sort_keys=True,
        separators=(",", ":"),
    )


def sign_payload(payload: str, key: bytes) -> str:
    """HMAC-SHA256 hex digest of ``payload``. The hex form is on
    the wire to avoid base64 / binary-safety quirks across Redis
    client libraries."""
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_payload(payload: str, signature: str, key: bytes) -> bool:
    """Constant-time compare via ``hmac.compare_digest``. Returns
    False if the signature does not match, the payload was
    mutated, or the key is wrong."""
    expected = sign_payload(payload, key)
    return hmac.compare_digest(expected, signature)


def build_envelope(subscription_id: str, event: str, key: bytes) -> str:
    """Build the wire envelope ``{"sig": ..., "payload": ...}`` as
    a JSON string ready for redis.publish. The inner payload is
    the canonical-serialized form so the sig the publisher wrote
    is the one the subscriber recomputes."""
    payload = canonical_payload(subscription_id, event)
    return json.dumps(
        {"sig": sign_payload(payload, key), "payload": payload},
        separators=(",", ":"),
    )


def parse_envelope(raw: str, key: bytes) -> Optional[dict]:
    """Verify the envelope and return the inner payload dict, or
    None on any verification failure (malformed JSON, missing
    fields, signature mismatch). The caller logs the rejection;
    this helper does not log so it stays test-friendly.
    """
    try:
        outer = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(outer, dict):
        return None
    sig = outer.get("sig")
    payload = outer.get("payload")
    if not isinstance(sig, str) or not isinstance(payload, str):
        return None
    if not verify_payload(payload, sig, key):
        return None
    try:
        inner = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(inner, dict):
        return None
    return inner
