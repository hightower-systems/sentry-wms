"""Unit tests for the pubsub HMAC envelope (#227)."""

import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from services.webhook_dispatcher import pubsub_signing


_GOOD_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class TestLoadKey:
    def test_load_key_returns_bytes(self, monkeypatch):
        monkeypatch.setenv("SENTRY_PUBSUB_HMAC_KEY", _GOOD_KEY)
        key = pubsub_signing.load_key()
        assert isinstance(key, bytes)
        assert key == _GOOD_KEY.encode("utf-8")

    def test_load_key_unset_raises(self, monkeypatch):
        monkeypatch.delenv("SENTRY_PUBSUB_HMAC_KEY", raising=False)
        with pytest.raises(pubsub_signing.PubsubKeyConfigError, match="unset"):
            pubsub_signing.load_key()

    def test_load_key_empty_raises(self, monkeypatch):
        monkeypatch.setenv("SENTRY_PUBSUB_HMAC_KEY", "   ")
        with pytest.raises(pubsub_signing.PubsubKeyConfigError, match="unset"):
            pubsub_signing.load_key()

    def test_load_key_placeholder_raises(self, monkeypatch):
        monkeypatch.setenv(
            "SENTRY_PUBSUB_HMAC_KEY",
            "replace-me-with-secrets-token-hex-32",
        )
        with pytest.raises(
            pubsub_signing.PubsubKeyConfigError, match="placeholder"
        ):
            pubsub_signing.load_key()

    def test_load_key_short_raises(self, monkeypatch):
        monkeypatch.setenv("SENTRY_PUBSUB_HMAC_KEY", "too-short")
        with pytest.raises(
            pubsub_signing.PubsubKeyConfigError, match="32 bytes"
        ):
            pubsub_signing.load_key()


class TestEnvelope:
    def test_canonical_payload_is_deterministic(self):
        a = pubsub_signing.canonical_payload("sub-x", "paused")
        b = pubsub_signing.canonical_payload("sub-x", "paused")
        assert a == b
        # Sorted keys: subscription_id < event would NOT sort that
        # way, so verify the actual ordering matches what
        # json.dumps(sort_keys=True) emits.
        assert a == '{"event":"paused","subscription_id":"sub-x"}'

    def test_sign_verify_roundtrip(self):
        key = b"k" * 32
        payload = pubsub_signing.canonical_payload("s", "deleted")
        sig = pubsub_signing.sign_payload(payload, key)
        assert pubsub_signing.verify_payload(payload, sig, key) is True

    def test_verify_rejects_wrong_signature(self):
        key = b"k" * 32
        payload = pubsub_signing.canonical_payload("s", "deleted")
        assert (
            pubsub_signing.verify_payload(payload, "00" * 32, key) is False
        )

    def test_verify_rejects_payload_mutation(self):
        key = b"k" * 32
        payload = pubsub_signing.canonical_payload("s", "paused")
        sig = pubsub_signing.sign_payload(payload, key)
        mutated = pubsub_signing.canonical_payload("s", "deleted")
        assert pubsub_signing.verify_payload(mutated, sig, key) is False

    def test_parse_rejects_payload_swap_with_old_sig(self):
        """An attacker who captures a legitimate envelope and
        swaps the inner payload while keeping the original sig
        cannot get the parser to accept the new payload."""
        import json

        key = b"k" * 32
        good = pubsub_signing.build_envelope("sub-x", "paused", key)
        outer = json.loads(good)
        sig_for_paused = outer["sig"]
        deleted_inner = pubsub_signing.canonical_payload("sub-x", "deleted")
        tampered = json.dumps(
            {"sig": sig_for_paused, "payload": deleted_inner}
        )
        assert pubsub_signing.parse_envelope(tampered, key) is None

    def test_verify_rejects_wrong_key(self):
        key_a = b"a" * 32
        key_b = b"b" * 32
        payload = pubsub_signing.canonical_payload("s", "paused")
        sig = pubsub_signing.sign_payload(payload, key_a)
        assert pubsub_signing.verify_payload(payload, sig, key_b) is False

    def test_build_parse_roundtrip(self):
        key = b"k" * 32
        wire = pubsub_signing.build_envelope("abc", "secret_rotated", key)
        inner = pubsub_signing.parse_envelope(wire, key)
        assert inner == {"subscription_id": "abc", "event": "secret_rotated"}

    def test_parse_rejects_unsigned_inner_payload(self):
        """A pre-#227 publisher's raw inner payload (no envelope)
        must not parse as a valid signed envelope under any key."""
        import json

        raw = json.dumps({"subscription_id": "x", "event": "paused"})
        assert pubsub_signing.parse_envelope(raw, b"k" * 32) is None

    def test_parse_rejects_malformed_json(self):
        assert pubsub_signing.parse_envelope("not-json", b"k" * 32) is None

    def test_parse_rejects_missing_sig_field(self):
        import json

        wire = json.dumps({"payload": "{}"})
        assert pubsub_signing.parse_envelope(wire, b"k" * 32) is None

    def test_parse_rejects_non_dict_inner(self):
        """An attacker who has the key could sign a non-dict inner
        payload; the parser still refuses non-dict shapes so the
        wake handler's downstream code can rely on dict access."""
        import json

        key = b"k" * 32
        payload = json.dumps(["not", "a", "dict"])
        sig = pubsub_signing.sign_payload(payload, key)
        wire = json.dumps({"sig": sig, "payload": payload})
        assert pubsub_signing.parse_envelope(wire, key) is None
