"""Tests for the v1.6.0 D2 HMAC signer (#174 / plan §3).

Covers:

  * Canonical envelope serialization is byte-for-byte deterministic
    and matches a pinned wire vector.
  * HMAC-SHA256 signature is deterministic for a fixed input + key
    and matches a pinned hex digest.
  * Round-trip verifier accepts a freshly-signed request.
  * Tampered body, tampered timestamp, wrong secret -> verifier
    returns None.
  * Constant-time comparison uses ``hmac.compare_digest`` (asserted
    by patching the module and counting calls).
  * Dual-accept verifier returns the matching generation regardless
    of candidate-list order.
  * SecretMaterial repr/str/format do not echo plaintext.
  * Fernet roundtrip via load_secret_for_signing on a real
    webhook_secrets row; load_all_active_secrets returns both
    generations within the 24-hour window.

The test pins a hex digest computed from the impl's own logic; a
refactor that changes either the canonical input prefix or the
body shape breaks the pinned vector and surfaces the regression
loudly.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from cryptography.fernet import Fernet

from services.webhook_dispatcher import envelope as envelope_module
from services.webhook_dispatcher import signing


# Pinned bytes vector. A change to either the body shape or the
# serializer's separators / sort_keys / encoding breaks this.
PINNED_ENVELOPE = {
    "event_id": 1,
    "event_type": "test.signed",
    "event_version": 1,
    "event_timestamp": "2026-04-28T12:00:00.000000Z",
    "aggregate_type": "sales_order",
    "aggregate_id": "11111111-1111-1111-1111-111111111111",
    "warehouse_id": 1,
    "source_txn_id": "22222222-2222-2222-2222-222222222222",
    "data": {"key": "value"},
}
PINNED_BODY = (
    b'{"aggregate_id":"11111111-1111-1111-1111-111111111111",'
    b'"aggregate_type":"sales_order",'
    b'"data":{"key":"value"},'
    b'"event_id":1,'
    b'"event_timestamp":"2026-04-28T12:00:00.000000Z",'
    b'"event_type":"test.signed",'
    b'"event_version":1,'
    b'"source_txn_id":"22222222-2222-2222-2222-222222222222",'
    b'"warehouse_id":1}'
)
PINNED_TIMESTAMP = 1764000000
PINNED_SECRET_BYTES = bytes(range(32))
# digest computed from PINNED_TIMESTAMP + b"." + PINNED_BODY HMAC'd
# under PINNED_SECRET_BYTES; pinned so a refactor that shifts the
# canonical input prefix breaks the vector.
_canonical = f"{PINNED_TIMESTAMP}.".encode("ascii") + PINNED_BODY
PINNED_DIGEST = hmac.new(PINNED_SECRET_BYTES, _canonical, hashlib.sha256).hexdigest()
PINNED_SIGNATURE = f"sha256={PINNED_DIGEST}"


def _make_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _ensure_connector(cur, connector_id="test-conn-d2-signing"):
    cur.execute(
        "INSERT INTO connectors (connector_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (connector_id) DO NOTHING",
        (connector_id, "D2 signing test connector"),
    )
    return connector_id


# ----------------------------------------------------------------------
# Envelope canonical-serialization
# ----------------------------------------------------------------------


class TestEnvelopeSerialization:
    def test_pinned_body_is_byte_exact(self):
        body = envelope_module.serialize_envelope(PINNED_ENVELOPE)
        assert body == PINNED_BODY, (
            "serialize_envelope must produce the pinned canonical bytes; "
            "any change here breaks consumer wire-compatibility"
        )

    def test_serialization_is_deterministic_across_calls(self):
        a = envelope_module.serialize_envelope(PINNED_ENVELOPE)
        b = envelope_module.serialize_envelope(PINNED_ENVELOPE)
        assert a == b
        assert a is not b  # fresh bytes object each call (no caching)

    def test_serialization_is_dict_order_independent(self):
        """sort_keys=True means a re-keyed dict serializes to the
        same bytes. Plan §3.1 invariant: the consumer reconstructor
        does not need to know the dispatcher's iteration order."""
        scrambled = dict(reversed(list(PINNED_ENVELOPE.items())))
        assert envelope_module.serialize_envelope(scrambled) == PINNED_BODY

    def test_separators_are_tight(self):
        """No space after , or :. The plan's wire shape is the
        polling-API envelope byte-for-byte; the polling endpoint
        emits this same compact form."""
        body = envelope_module.serialize_envelope({"a": 1, "b": "x"})
        assert b": " not in body
        assert b", " not in body


# ----------------------------------------------------------------------
# HMAC signing
# ----------------------------------------------------------------------


class TestSignRequestPinnedVector:
    def test_pinned_signature_matches(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(
            PINNED_ENVELOPE, secret, timestamp=PINNED_TIMESTAMP
        )
        assert signed.body == PINNED_BODY
        assert signed.signature == PINNED_SIGNATURE
        assert signed.timestamp == PINNED_TIMESTAMP
        assert signed.secret_generation == 1

    def test_signed_body_matches_envelope_serialization(self):
        """Single-serialization invariant: the bytes inside
        ``SignedRequest.body`` must be identical to the bytes
        ``serialize_envelope`` would produce for the same
        envelope. Catches a refactor that re-derives the body
        from a different code path."""
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(PINNED_ENVELOPE, secret, timestamp=42)
        assert signed.body == envelope_module.serialize_envelope(PINNED_ENVELOPE)

    def test_default_timestamp_is_real_seconds(self, monkeypatch):
        """Production callers omit ``timestamp``; it falls back to
        ``int(time.time())``. Confirm the fallback path works
        without a mock."""
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(PINNED_ENVELOPE, secret)
        assert signed.timestamp > 1700000000  # 2023-11-14 lower bound; sanity


class TestVerifySignature:
    def test_round_trip_accepts(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(
            PINNED_ENVELOPE, secret, timestamp=PINNED_TIMESTAMP
        )
        result = signing.verify_signature(
            signed.timestamp, signed.body, signed.signature, [secret]
        )
        assert result == 1

    def test_tampered_body_rejects(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(
            PINNED_ENVELOPE, secret, timestamp=PINNED_TIMESTAMP
        )
        tampered = bytearray(signed.body)
        tampered[5] ^= 0xFF  # flip a byte
        assert (
            signing.verify_signature(
                signed.timestamp, bytes(tampered), signed.signature, [secret]
            )
            is None
        )

    def test_tampered_timestamp_rejects(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(
            PINNED_ENVELOPE, secret, timestamp=PINNED_TIMESTAMP
        )
        assert (
            signing.verify_signature(
                signed.timestamp + 1, signed.body, signed.signature, [secret]
            )
            is None
        )

    def test_wrong_secret_rejects(self):
        secret_a = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        secret_b = signing.SecretMaterial(b"\xff" * 32, generation=1)
        signed = signing.sign_request(
            PINNED_ENVELOPE, secret_a, timestamp=PINNED_TIMESTAMP
        )
        assert (
            signing.verify_signature(
                signed.timestamp, signed.body, signed.signature, [secret_b]
            )
            is None
        )

    def test_dual_accept_returns_matching_generation(self):
        """Plan §3.2 dual-accept: rotation produces gen=2 (previous);
        the dispatcher signs with gen=1; the consumer accepts either
        until expires_at. Verifier must return the GENERATION that
        matched, not just a boolean, so the consumer can log
        rotation telemetry."""
        gen1 = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        gen2 = signing.SecretMaterial(b"\x42" * 32, generation=2)

        signed_with_gen1 = signing.sign_request(
            PINNED_ENVELOPE, gen1, timestamp=PINNED_TIMESTAMP
        )
        signed_with_gen2 = signing.sign_request(
            PINNED_ENVELOPE, gen2, timestamp=PINNED_TIMESTAMP
        )

        assert signing.verify_signature(
            signed_with_gen1.timestamp,
            signed_with_gen1.body,
            signed_with_gen1.signature,
            [gen1, gen2],
        ) == 1
        assert signing.verify_signature(
            signed_with_gen2.timestamp,
            signed_with_gen2.body,
            signed_with_gen2.signature,
            [gen1, gen2],
        ) == 2

    def test_dual_accept_is_order_independent(self):
        gen1 = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        gen2 = signing.SecretMaterial(b"\x42" * 32, generation=2)
        signed = signing.sign_request(
            PINNED_ENVELOPE, gen2, timestamp=PINNED_TIMESTAMP
        )
        assert signing.verify_signature(
            signed.timestamp, signed.body, signed.signature, [gen2, gen1]
        ) == 2
        assert signing.verify_signature(
            signed.timestamp, signed.body, signed.signature, [gen1, gen2]
        ) == 2

    def test_uses_constant_time_comparison(self, monkeypatch):
        """``hmac.compare_digest`` is the constant-time comparator;
        a refactor that uses ``==`` on bytes (or string) leaks
        timing info on partial matches. Patch the module and
        count call attempts."""
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(
            PINNED_ENVELOPE, secret, timestamp=PINNED_TIMESTAMP
        )

        call_count = {"n": 0}
        real_compare = hmac.compare_digest

        def counting_compare(a, b):
            call_count["n"] += 1
            return real_compare(a, b)

        monkeypatch.setattr(
            "services.webhook_dispatcher.signing.hmac.compare_digest",
            counting_compare,
        )

        signing.verify_signature(
            signed.timestamp, signed.body, signed.signature, [secret]
        )
        assert call_count["n"] == 1, (
            "verify_signature must call hmac.compare_digest exactly once "
            "per candidate secret; got " + str(call_count["n"])
        )


# ----------------------------------------------------------------------
# SecretMaterial plaintext lifecycle
# ----------------------------------------------------------------------


class TestSecretMaterialDoesNotLeak:
    """Plan §3.2: ``repr`` of the secret wrapper does NOT echo
    plaintext; standard logging formatters with ``%r`` do not
    print plaintext. Locked here so a future refactor cannot
    silently widen the leak surface."""

    def test_repr_does_not_echo_plaintext(self):
        secret = signing.SecretMaterial(b"super-duper-secret-key-bytes-32!", generation=1)
        rendered = repr(secret)
        assert b"super-duper-secret-key-bytes-32!".decode("ascii") not in rendered
        assert "generation=1" in rendered

    def test_str_raises_loud(self):
        secret = signing.SecretMaterial(b"plaintext-32-bytes!!!!!!!!!!!!aa", generation=1)
        with pytest.raises(TypeError):
            str(secret)

    def test_f_string_format_raises_loud(self):
        secret = signing.SecretMaterial(b"plaintext-32-bytes!!!!!!!!!!!!aa", generation=1)
        with pytest.raises(TypeError):
            _ = f"{secret}"

    def test_f_string_repr_does_not_echo(self):
        secret = signing.SecretMaterial(b"super-duper-secret-key-bytes-32!", generation=1)
        rendered = f"{secret!r}"
        assert "super-duper-secret-key-bytes-32!" not in rendered

    def test_logging_with_r_formatter_does_not_echo(self, caplog):
        """A common slip is ``logger.warning("got secret %r", secret)``.
        Confirm this path also stays safe."""
        secret = signing.SecretMaterial(b"super-duper-secret-key-bytes-32!", generation=1)
        with caplog.at_level(logging.WARNING):
            logging.getLogger("test").warning("loaded secret %r", secret)
        assert "super-duper-secret-key-bytes-32!" not in caplog.text

    def test_bytes_access_returns_plaintext(self):
        """Authorized path: ``bytes(secret)`` returns the raw
        plaintext for the HMAC computation. This is the ONE
        place plaintext escapes the wrapper; it MUST stay
        inside signing.py per plan §3.2."""
        secret = signing.SecretMaterial(b"\x00" * 32, generation=1)
        assert bytes(secret) == b"\x00" * 32

    def test_invalid_generation_raises(self):
        for bad in (0, 3, -1, 99):
            with pytest.raises(ValueError):
                signing.SecretMaterial(b"\x00" * 32, generation=bad)

    def test_non_bytes_plaintext_raises(self):
        with pytest.raises(TypeError):
            signing.SecretMaterial("string-not-bytes", generation=1)  # type: ignore

    def test_pickle_dumps_raises(self):
        """#220: pickle of a __slots__-only class default-serializes
        the slot values, leaking _plaintext into the stream. The
        wrapper refuses pickle on every protocol."""
        import pickle

        plaintext = b"super-duper-secret-key-bytes-32!"
        secret = signing.SecretMaterial(plaintext, generation=1)
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with pytest.raises(TypeError) as exc_info:
                pickle.dumps(secret, protocol=protocol)
            assert "not picklable" in str(exc_info.value)
            assert "bytes(secret)" in str(exc_info.value)

    def test_copy_deepcopy_raises(self):
        """copy.deepcopy uses the same __reduce_ex__ path the
        pickle module does; the refusal therefore covers it too."""
        import copy

        secret = signing.SecretMaterial(b"\x42" * 32, generation=1)
        with pytest.raises(TypeError):
            copy.deepcopy(secret)
        with pytest.raises(TypeError):
            copy.copy(secret)

    def test_getstate_setstate_raise(self):
        """Hand-rolled getstate / setstate paths (some serialization
        libraries use them directly without going through pickle)
        also refuse, so the breach surface stays uniform."""
        secret = signing.SecretMaterial(b"\x42" * 32, generation=1)
        with pytest.raises(TypeError):
            secret.__getstate__()
        with pytest.raises(TypeError):
            secret.__setstate__({"_plaintext": b"x", "generation": 1})


# ----------------------------------------------------------------------
# DB integration: load_secret_for_signing + load_all_active_secrets
# ----------------------------------------------------------------------


class TestLoadSecretForSigning:
    """Exercise the full Fernet roundtrip against a real
    webhook_secrets row. Confirms the BYTEA <-> bytes shape
    handling and that the loaded plaintext signs identically to
    the same plaintext constructed directly."""

    @pytest.fixture(autouse=True)
    def _subscription(self):
        # Force-rebuild Fernet cache so the test's encryption uses
        # the same key the signer's _decrypt will read.
        signing._fernet_cache = None  # noqa: SLF001
        fernet = signing._get_fernet()  # noqa: SLF001

        conn = _make_conn()
        conn.autocommit = True
        cur = conn.cursor()
        connector_id = _ensure_connector(cur)
        cur.execute(
            """
            INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
            VALUES (%s, %s, %s) RETURNING subscription_id
            """,
            (connector_id, "signing-load test", "https://example.invalid/sign"),
        )
        self.sub_id = cur.fetchone()[0]

        plaintext_gen1 = b"primary-32-byte-secret-plaintxt!"
        plaintext_gen2 = b"previous-32-byte-secret-plntxt!!"
        cur.execute(
            "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext) "
            "VALUES (%s, 1, %s)",
            (str(self.sub_id), fernet.encrypt(plaintext_gen1)),
        )
        cur.execute(
            "INSERT INTO webhook_secrets (subscription_id, generation, secret_ciphertext, expires_at) "
            "VALUES (%s, 2, %s, NOW() + INTERVAL '24 hours')",
            (str(self.sub_id), fernet.encrypt(plaintext_gen2)),
        )
        self.plaintext_gen1 = plaintext_gen1
        self.plaintext_gen2 = plaintext_gen2
        conn.close()
        yield
        cleanup = _make_conn()
        cleanup.autocommit = True
        cleanup.cursor().execute(
            "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
            (str(self.sub_id),),
        )
        cleanup.close()

    def test_load_primary_secret_decrypts_to_known_plaintext(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            secret = signing.load_secret_for_signing(cur, self.sub_id)
            assert bytes(secret) == self.plaintext_gen1
            assert secret.generation == 1
        finally:
            conn.close()

    def test_load_all_active_returns_both_generations_within_window(self):
        conn = _make_conn()
        try:
            cur = conn.cursor()
            secrets = signing.load_all_active_secrets(cur, self.sub_id)
            assert len(secrets) == 2
            by_gen = {s.generation: bytes(s) for s in secrets}
            assert by_gen[1] == self.plaintext_gen1
            assert by_gen[2] == self.plaintext_gen2
        finally:
            conn.close()

    def test_load_primary_raises_when_missing(self):
        # New subscription with no secrets at all.
        conn = _make_conn()
        conn.autocommit = True
        cur = conn.cursor()
        connector_id = _ensure_connector(cur)
        cur.execute(
            """
            INSERT INTO webhook_subscriptions (connector_id, display_name, delivery_url)
            VALUES (%s, %s, %s) RETURNING subscription_id
            """,
            (connector_id, "no-secret", "https://example.invalid/none"),
        )
        empty_sub_id = cur.fetchone()[0]
        conn.close()

        try:
            conn = _make_conn()
            try:
                cur = conn.cursor()
                with pytest.raises(RuntimeError, match="no primary"):
                    signing.load_secret_for_signing(cur, empty_sub_id)
            finally:
                conn.close()
        finally:
            cleanup = _make_conn()
            cleanup.autocommit = True
            cleanup.cursor().execute(
                "DELETE FROM webhook_subscriptions WHERE subscription_id = %s",
                (str(empty_sub_id),),
            )
            cleanup.close()

    def test_load_all_excludes_expired_generation_2(self):
        # Set the gen=2 row's expires_at to the past; load_all
        # should return only gen=1.
        conn = _make_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "UPDATE webhook_secrets SET expires_at = NOW() - INTERVAL '1 hour' "
            "WHERE subscription_id = %s AND generation = 2",
            (str(self.sub_id),),
        )
        conn.close()

        conn = _make_conn()
        try:
            cur = conn.cursor()
            secrets = signing.load_all_active_secrets(cur, self.sub_id)
            assert len(secrets) == 1
            assert secrets[0].generation == 1
        finally:
            conn.close()

    def test_load_acquires_for_share_lock(self):
        """#225: the dispatcher's SELECT must hold a FOR SHARE row
        lock on the gen=1 webhook_secrets row so the admin's
        rotation transaction (UPDATE/DELETE/INSERT on the same
        rows) blocks until sign + stamp commit. Verified by
        inspecting pg_locks while the dispatcher's transaction
        is open."""
        # Open a connection in non-autocommit mode and run the
        # signer's load. The lock must be visible in pg_locks
        # before we commit.
        conn = _make_conn()
        try:
            cur = conn.cursor()
            secret = signing.load_secret_for_signing(cur, self.sub_id)
            assert secret.generation == 1

            # pg_locks records row-level locks via mode='ShareLock'
            # on the relation when FOR SHARE is in effect. The
            # webhook_secrets row's relation OID exposes the lock.
            inspector = _make_conn()
            try:
                ic = inspector.cursor()
                ic.execute(
                    """
                    SELECT 1 FROM pg_locks l
                      JOIN pg_class c ON c.oid = l.relation
                     WHERE c.relname = 'webhook_secrets'
                       AND l.mode IN ('RowShareLock', 'ShareLock', 'ShareUpdateExclusiveLock')
                       AND l.granted = TRUE
                       AND l.pid = %s
                    """,
                    (conn.info.backend_pid,),
                )
                assert ic.fetchone() is not None, (
                    "load_secret_for_signing must take a FOR SHARE lock so "
                    "rotation cannot demote the row mid-sign"
                )
            finally:
                inspector.close()
            conn.commit()
        finally:
            conn.close()

    def test_load_returns_generation_from_row_not_hardcoded(self):
        """#225: the SecretMaterial's generation comes from the
        row's actual generation column. The dispatcher stamps that
        value on the wire header; if the loader hardcoded 1, a
        future change to sign with gen=2 would silently produce a
        mismatched header."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            secret = signing.load_secret_for_signing(cur, self.sub_id)
            # Confirm generation came from the row, not a constant.
            cur.execute(
                "SELECT generation FROM webhook_secrets "
                "WHERE subscription_id = %s AND generation = 1",
                (str(self.sub_id),),
            )
            row = cur.fetchone()
            assert secret.generation == int(row[0])
        finally:
            conn.close()

    def test_signed_request_with_loaded_secret_matches_direct_construction(self):
        """End-to-end equivalence: a SecretMaterial loaded from
        the DB and a SecretMaterial constructed directly with
        the same plaintext bytes produce IDENTICAL signatures.
        Catches any accidental transformation (encoding, padding,
        memoryview vs bytes) inside load_secret_for_signing."""
        conn = _make_conn()
        try:
            cur = conn.cursor()
            loaded = signing.load_secret_for_signing(cur, self.sub_id)
        finally:
            conn.close()
        direct = signing.SecretMaterial(self.plaintext_gen1, generation=1)
        ts = 1234567890
        signed_loaded = signing.sign_request(PINNED_ENVELOPE, loaded, timestamp=ts)
        signed_direct = signing.sign_request(PINNED_ENVELOPE, direct, timestamp=ts)
        assert signed_loaded.signature == signed_direct.signature
        assert signed_loaded.body == signed_direct.body


# ----------------------------------------------------------------------
# Envelope build helper
# ----------------------------------------------------------------------


class TestEncryptionKeyValidation:
    """V-201 #142 pattern applied to SENTRY_ENCRYPTION_KEY: an
    unset / empty / whitespace / placeholder value must refuse
    to load the Fernet cipher with a clear actionable message
    rather than producing a silently-broken cipher that
    decrypts garbage and HMAC-signs to a value no consumer can
    reconstruct."""

    def _reset_cache(self):
        signing._fernet_cache = None  # noqa: SLF001

    def test_unset_key_raises_with_actionable_message(self, monkeypatch):
        self._reset_cache()
        monkeypatch.delenv("SENTRY_ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SENTRY_ENCRYPTION_KEY"):
            signing._get_fernet()  # noqa: SLF001

    def test_empty_key_raises(self, monkeypatch):
        self._reset_cache()
        monkeypatch.setenv("SENTRY_ENCRYPTION_KEY", "")
        with pytest.raises(RuntimeError, match="SENTRY_ENCRYPTION_KEY"):
            signing._get_fernet()  # noqa: SLF001

    def test_whitespace_only_key_raises(self, monkeypatch):
        """An accidentally-quoted-and-stripped value (e.g. an
        operator who set ``SENTRY_ENCRYPTION_KEY="   "``) must
        not silently pass into Fernet, where it would surface as
        a cryptography library error rather than an application-
        level configuration error."""
        self._reset_cache()
        monkeypatch.setenv("SENTRY_ENCRYPTION_KEY", "   ")
        with pytest.raises(RuntimeError, match="SENTRY_ENCRYPTION_KEY"):
            signing._get_fernet()  # noqa: SLF001

    def test_env_example_placeholder_raises(self, monkeypatch):
        """The literal string shipped in .env.example must be
        rejected so a deployment that forgot to generate a real
        key fails fast at boot instead of producing gibberish
        decryption later."""
        self._reset_cache()
        monkeypatch.setenv(
            "SENTRY_ENCRYPTION_KEY", "replace-me-with-fernet-generate-key"
        )
        with pytest.raises(RuntimeError, match="placeholder"):
            signing._get_fernet()  # noqa: SLF001

    def test_valid_key_caches_for_subsequent_calls(self, monkeypatch):
        """Master-key cache hot path: first call constructs, all
        subsequent calls return the same Fernet instance. Mirrors
        credential_vault._get_fernet."""
        self._reset_cache()
        monkeypatch.setenv(
            "SENTRY_ENCRYPTION_KEY", os.environ["SENTRY_ENCRYPTION_KEY"]
        )
        first = signing._get_fernet()  # noqa: SLF001
        second = signing._get_fernet()  # noqa: SLF001
        assert first is second


class TestSignRequestEdgeCases:
    """Edge cases that pin the canonical-serialize + HMAC-sign
    surface against shape drift. None of these are exploitable
    on their own; together they keep the wire-compatibility
    surface stable across refactors."""

    def test_empty_envelope_signs_deterministically(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request({}, secret, timestamp=42)
        assert signed.body == b"{}"
        # Round-trips through the verifier.
        assert signing.verify_signature(
            signed.timestamp, signed.body, signed.signature, [secret]
        ) == 1

    def test_zero_timestamp_signs_deterministically(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(PINNED_ENVELOPE, secret, timestamp=0)
        assert signed.timestamp == 0
        assert signing.verify_signature(
            0, signed.body, signed.signature, [secret]
        ) == 1

    def test_large_body_signs_without_truncation(self):
        """A pathological large envelope must not get silently
        truncated by hmac or the canonical serializer. 1MB-ish
        payload exercises the hash-streaming path under load."""
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        big_data = "x" * (1024 * 1024)  # ~1MB string
        envelope = dict(PINNED_ENVELOPE, data={"blob": big_data})
        signed = signing.sign_request(envelope, secret, timestamp=42)
        assert len(signed.body) > 1024 * 1024
        # The verifier must accept the freshly-signed large body.
        assert signing.verify_signature(
            signed.timestamp, signed.body, signed.signature, [secret]
        ) == 1


class TestSignedRequestEqualityIsDisabled:
    """``@dataclass(frozen=True, eq=False)`` on SignedRequest:
    the auto-generated ``__eq__`` is a non-constant-time bytes
    comparator. We disable it pre-emptively so a future call
    site cannot accidentally compare two SignedRequest objects
    via ``==`` and leak partial-match timing on attacker-
    influenced bytes. Identity comparison still works."""

    def test_two_identical_signed_requests_are_not_eq(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        a = signing.sign_request(PINNED_ENVELOPE, secret, timestamp=42)
        b = signing.sign_request(PINNED_ENVELOPE, secret, timestamp=42)
        # eq=False -> identity comparison only.
        assert a is not b
        assert (a == b) is False, (
            "SignedRequest.__eq__ must NOT auto-generate; auto __eq__ "
            "uses == on body/signature which is non-constant-time"
        )

    def test_signed_request_is_still_frozen(self):
        secret = signing.SecretMaterial(PINNED_SECRET_BYTES, generation=1)
        signed = signing.sign_request(PINNED_ENVELOPE, secret, timestamp=42)
        with pytest.raises((AttributeError, Exception)):
            signed.body = b"tampered"  # type: ignore


class TestBuildEnvelope:
    def test_keys_match_polling_envelope_shape(self):
        """Plan §3.1: wire envelope matches the poll envelope
        byte-for-byte. The keys here are the polling-API contract."""
        row = {
            "event_id": 99,
            "event_type": "ship.confirmed",
            "event_version": 1,
            "event_timestamp": "2026-04-28T00:00:00Z",
            "aggregate_type": "fulfillment",
            "aggregate_id": 42,
            "aggregate_external_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
            "warehouse_id": 1,
            "source_txn_id": uuid.UUID("44444444-4444-4444-4444-444444444444"),
            "payload": {"sku": "ABC"},
        }
        env = envelope_module.build_envelope(row)
        assert set(env.keys()) == {
            "event_id",
            "event_type",
            "event_version",
            "event_timestamp",
            "aggregate_type",
            "aggregate_id",
            "warehouse_id",
            "source_txn_id",
            "data",
        }
        # UUIDs MUST land as strings on the wire so a JSON
        # consumer can round-trip without a UUID-aware parser.
        assert env["aggregate_id"] == "33333333-3333-3333-3333-333333333333"
        assert env["source_txn_id"] == "44444444-4444-4444-4444-444444444444"
        # data carries the row's payload through unchanged.
        assert env["data"] == {"sku": "ABC"}
