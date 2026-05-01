"""HMAC-SHA256 signer with 24-hour dual-accept rotation (plan §3).

Three responsibilities:

1. Hold per-subscription HMAC secret material in a wrapper
   (:class:`SecretMaterial`) that refuses to leak plaintext via
   ``repr``, ``str``, or ``%r`` formatting (plan §3.2).
2. Compute the canonical signing input ``f"{timestamp}.{body}"``
   and HMAC-SHA256 it (plan §3.1). The body bytes are produced
   exactly once by :mod:`envelope` and reused unchanged across
   sign and send (single-serialization invariant).
3. Verify a given signature against one or more candidate
   secrets in constant time via ``hmac.compare_digest``,
   supporting the 24-hour dual-accept rotation window.

The dispatcher signs with ``generation=1`` only. Consumers
accept either generation until ``expires_at`` (plan §3.2). The
verifier here is consumer-shaped and used by the test suite to
assert wire-compatibility; the production dispatcher only
exercises :func:`sign_request`.

Plaintext lifecycle (plan §3.2): the SENTRY_ENCRYPTION_KEY
master key decrypts ``webhook_secrets.secret_ciphertext`` into a
local variable inside this module; the plaintext is wrapped in
``SecretMaterial`` and never crosses the call stack out as a raw
``bytes`` object. ``SecretMaterial.__repr__`` is loud-safe;
``SecretMaterial.__str__`` raises so an accidental ``f"{secret}"``
coerce surfaces as a TypeError instead of a silent leak.
"""

import hashlib
import hmac
import os
import time as _time
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from cryptography.fernet import Fernet

from . import envelope as envelope_module


# Master-key cache. Populated on first decrypt, never cleared.
# Mirrors v1.3 ``credential_vault._get_fernet`` -- the master key is
# static per deployment, so a runtime change is operator error and
# should require a process restart. This is intentionally a different
# pattern from env_validator's int_var/bool_var (which re-read every
# call) because tunables vs master-secrets have different lifecycles
# (V-217 #156: tunables must be live-readable; master keys must not
# rotate silently mid-process).
_fernet_cache: Optional[Fernet] = None

# Placeholder strings shipped in .env.example. A deployment that
# left these in place would Fernet-decrypt with the placeholder
# bytes and produce gibberish plaintext that HMAC-signs to a value
# no consumer can reconstruct. V-201 #142 pattern: reject the
# placeholder shape AND whitespace-only AND too-short keys at boot
# rather than producing silent gibberish at runtime.
_PLACEHOLDER_KEYS = frozenset({
    "replace-me-with-fernet-generate-key",
})


def _get_fernet() -> Fernet:
    """Return the process-wide Fernet cipher. Reads
    SENTRY_ENCRYPTION_KEY on first call; subsequent calls reuse.
    Operates on raw bytes (BYTEA) rather than strings (which is
    what ``credential_vault`` does).

    Validation mirrors the V-201 #142 weak-pepper validation
    pattern: rejects unset, empty, whitespace-only, and the
    .env.example placeholder. A Fernet key must be 32 url-safe
    base64 bytes, so the lower length bound is documented in the
    error message but the conservative gate here just checks that
    the value is non-trivial; Fernet itself will reject malformed
    keys with a separate error so the failure surface is loud
    either way.
    """
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache
    key = os.environ.get("SENTRY_ENCRYPTION_KEY")
    if not key or not key.strip():
        raise RuntimeError(
            "SENTRY_ENCRYPTION_KEY environment variable is required for the "
            "webhook dispatcher (decrypts webhook_secrets.secret_ciphertext "
            "for HMAC signing). Generate with: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key()"
            ".decode())\""
        )
    if key.strip() in _PLACEHOLDER_KEYS:
        raise RuntimeError(
            "SENTRY_ENCRYPTION_KEY is set to the .env.example placeholder "
            "value; a deployment with this value would Fernet-decrypt with "
            "garbage and HMAC-sign to a value no consumer can reconstruct. "
            "Generate a real key with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    _fernet_cache = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet_cache


def _decrypt(ciphertext: bytes) -> bytes:
    """Fernet-decrypt BYTEA ciphertext to plaintext bytes."""
    return _get_fernet().decrypt(ciphertext)


class SecretMaterial:
    """Wrapper that holds HMAC plaintext and refuses to leak it.

    Access plaintext via ``bytes(secret)`` only. ``repr`` returns
    a fixed safe string; ``str`` raises so an accidental
    f-string coerce surfaces loud. ``__slots__`` keeps the
    plaintext attribute out of any ``__dict__`` that a debugger
    or dump tool might walk.
    """

    __slots__ = ("_plaintext", "generation")

    def __init__(self, plaintext: bytes, generation: int):
        if generation not in (1, 2):
            raise ValueError(
                f"SecretMaterial.generation must be 1 or 2, got {generation}"
            )
        if not isinstance(plaintext, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"SecretMaterial.plaintext must be bytes-like, got "
                f"{type(plaintext).__name__}"
            )
        self._plaintext = bytes(plaintext)
        self.generation = generation

    def __repr__(self) -> str:
        return f"<SecretMaterial generation={self.generation}>"

    def __str__(self) -> str:
        # Loud refusal: an accidental f"{secret}" must not
        # silently coerce. Surfacing a TypeError is the test the
        # CI suite exercises; production code paths never hit
        # this because the plaintext path goes through bytes().
        raise TypeError(
            "SecretMaterial cannot be converted to string; access plaintext "
            "via bytes(secret) inside signing.py only"
        )

    def __bytes__(self) -> bytes:
        return self._plaintext

    # #220: refuse pickle on every protocol path. The default
    # __slots__ pickle behavior writes _plaintext into the stream
    # via copyreg.__reduce_ex__, defeating the repr/str refusals.
    # __reduce_ex__ catches every pickle protocol version (incl. 0
    # and 5); __getstate__ catches a hand-rolled "save state for
    # later" caller; __setstate__ catches an attempt to restore an
    # object from an externally-built state dict. All three raise
    # the same TypeError so the failure shape is unambiguous and
    # any future serialization layer (multiprocessing, shelve, APM
    # local-capture, joblib, etc.) surfaces the breach loudly.
    _PICKLE_REFUSAL = (
        "SecretMaterial is not picklable; access plaintext via "
        "bytes(secret) inside signing.py only"
    )

    def __reduce_ex__(self, protocol):
        raise TypeError(self._PICKLE_REFUSAL)

    def __reduce__(self):
        raise TypeError(self._PICKLE_REFUSAL)

    def __getstate__(self):
        raise TypeError(self._PICKLE_REFUSAL)

    def __setstate__(self, state):
        raise TypeError(self._PICKLE_REFUSAL)


def _row_value(row, position: int, key: str):
    """Read a column from a row that may be either a tuple (the
    default psycopg2 cursor shape) or a RealDictRow (used by
    dispatch.deliver_one). Tries key access first; falls back
    to positional. Decouples the signer from the caller's
    cursor type."""
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return row[position]


def load_secret_for_signing(cur, subscription_id: str) -> SecretMaterial:
    """Load and decrypt the primary (generation=1) secret for a
    subscription. Raises RuntimeError if no primary secret exists.

    The cursor is the caller's responsibility (transaction scope,
    connection pooling, etc.); this function does not commit or
    rollback. Works with both default cursors and RealDictCursor.
    """
    cur.execute(
        "SELECT secret_ciphertext FROM webhook_secrets "
        "WHERE subscription_id = %s AND generation = 1",
        (str(subscription_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"no primary (generation=1) secret found for subscription "
            f"{subscription_id}; the dispatcher cannot sign without one"
        )
    ciphertext = bytes(_row_value(row, 0, "secret_ciphertext"))
    plaintext = _decrypt(ciphertext)
    return SecretMaterial(plaintext, generation=1)


def load_all_active_secrets(cur, subscription_id: str) -> List[SecretMaterial]:
    """Load all non-expired secrets for a subscription. Includes
    the primary (generation=1, expires_at IS NULL) and the
    previous (generation=2) when its expires_at has not yet
    elapsed. Used by the consumer-shaped verifier in tests; the
    dispatcher itself signs with the primary only.
    """
    cur.execute(
        "SELECT generation, secret_ciphertext FROM webhook_secrets "
        "WHERE subscription_id = %s "
        "  AND (expires_at IS NULL OR expires_at > NOW()) "
        "ORDER BY generation",
        (str(subscription_id),),
    )
    rows = cur.fetchall()
    out: List[SecretMaterial] = []
    for row in rows:
        generation = int(_row_value(row, 0, "generation"))
        ciphertext = bytes(_row_value(row, 1, "secret_ciphertext"))
        out.append(SecretMaterial(_decrypt(ciphertext), generation=generation))
    return out


def compute_signature(timestamp: int, body: bytes, secret: SecretMaterial) -> str:
    """HMAC-SHA256 over ``f"{timestamp}.".encode("ascii") + body``.
    Returns the hex digest prefixed with ``sha256=`` to match the
    ``X-Sentry-Signature`` header shape.

    Plan §3.1 mandates the timestamp prefix to bind the signature
    to a specific moment; the consumer's 5-minute replay-protection
    window relies on it.
    """
    canonical = f"{timestamp}.".encode("ascii") + body
    digest = hmac.new(bytes(secret), canonical, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    timestamp: int,
    body: bytes,
    given_signature: str,
    candidate_secrets: Sequence[SecretMaterial],
) -> Optional[int]:
    """Try each candidate secret in order; return the matching
    generation (1 or 2) or None. Comparison is constant-time via
    ``hmac.compare_digest``; iteration order does not leak which
    generation matched because the comparison cost is fixed.

    Used by the test suite to assert wire-compatibility; the
    consumer integration guide will document this exact shape so
    a downstream reconstructor can be ported one-for-one.

    Iteration order is non-secret. Wall-clock time may differ
    between "matched on first secret" and "matched on second"
    by one HMAC computation, but the matching generation is
    returned to the caller explicitly anyway, so the timing
    leak adds no information beyond the return value.
    """
    for secret in candidate_secrets:
        expected = compute_signature(timestamp, body, secret)
        if hmac.compare_digest(expected, given_signature):
            return secret.generation
    return None


@dataclass(frozen=True, eq=False)
class SignedRequest:
    """Output of :func:`sign_request`. The body bytes are the
    EXACT bytes the HTTP client must send; the runtime assertion
    at the HTTP-client boundary (D5/D8) checks
    ``request.body == signed.body`` as the bottom rung that
    catches any transformation between sign and send.

    ``eq=False`` is deliberate: the dataclass auto-generated
    ``__eq__`` compares ``body``/``signature``/``timestamp`` with
    ``==``, which is non-constant-time on bytes/str. The signer
    never compares two ``SignedRequest`` instances; if a future
    call site needs equality (e.g. caching), introduce a method
    that uses ``hmac.compare_digest`` explicitly rather than
    relying on the auto-generated path.
    """

    body: bytes
    signature: str
    timestamp: int
    secret_generation: int


def sign_request(
    envelope: Mapping[str, Any],
    secret: SecretMaterial,
    timestamp: Optional[int] = None,
) -> SignedRequest:
    """Combine canonical-serialize + HMAC-sign in one call. The
    body bytes are constructed exactly ONCE here via
    :func:`envelope.serialize_envelope`; callers MUST pass
    ``signed.body`` through to the HTTP client unchanged. A
    refactor that re-serializes the envelope between sign and
    send would silently break wire-compatibility; the runtime
    assertion at the HTTP-client boundary surfaces that as a
    test failure (D5).

    ``timestamp`` is injectable so the test suite can pin a
    deterministic value for the canonical-signature vector;
    production calls default to ``int(time.time())``.
    """
    body = envelope_module.serialize_envelope(envelope)
    ts = timestamp if timestamp is not None else int(_time.time())
    signature = compute_signature(ts, body, secret)
    return SignedRequest(
        body=body,
        signature=signature,
        timestamp=ts,
        secret_generation=secret.generation,
    )
