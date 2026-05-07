"""
Tests for V-007 credential scrubber used in Celery error paths.

v1.8.0 (#52): extends scrub_secrets with a credential pattern catalog
covering credential shapes that appear outside URLs (Sentry bearer
tokens, AWS access keys, generic Bearer headers, key=value
connection-string fragments, NetSuite OAuth fragments, JWT-shaped
strings, plus a heuristic catch-all).
"""

import pytest
from utils.log_sanitize import scrub_credentials, scrub_secrets


class TestUrlUserinfo:
    def test_strips_basic_userinfo(self):
        result = scrub_secrets("connection failed to https://alice:s3cret@example.com/api")
        assert "alice" not in result
        assert "s3cret" not in result
        assert "https://example.com/api" in result

    def test_strips_password_only_userinfo(self):
        result = scrub_secrets("curl: https://:only-pass@host.internal/v1")
        assert "only-pass" not in result
        assert "https://host.internal/v1" in result

    def test_keeps_port_after_stripping_userinfo(self):
        result = scrub_secrets("fetch https://u:p@host:8443/path")
        assert "u:p" not in result
        assert "host:8443" in result

    def test_leaves_url_without_userinfo_untouched(self):
        original = "GET https://api.example.com/v1/orders returned 500"
        assert scrub_secrets(original) == original

    def test_strips_from_exception_str(self):
        try:
            raise ValueError("HTTP error for https://user:tok_abc123@api.example.com/items")
        except ValueError as exc:
            result = scrub_secrets(exc)
            assert "tok_abc123" not in result
            assert "user" not in result


class TestQueryRedaction:
    def test_redacts_api_key_query_param(self):
        result = scrub_secrets("failed: https://api.example.com/orders?api_key=SECRET123&limit=10")
        assert "SECRET123" not in result
        assert "REDACTED" in result
        assert "limit=10" in result

    def test_redacts_token_variants(self):
        for key in ("token", "access_token", "refresh_token", "client_secret", "password"):
            url = f"https://api.example.com/x?{key}=leakable"
            result = scrub_secrets(f"err: {url}")
            assert "leakable" not in result, f"{key} was not redacted: {result}"

    def test_case_insensitive_key_match(self):
        result = scrub_secrets("https://api.example.com/x?API_KEY=leak")
        assert "leak" not in result


class TestSafeDefaults:
    def test_none_returns_empty_string(self):
        assert scrub_secrets(None) == ""

    def test_empty_string_preserved(self):
        assert scrub_secrets("") == ""

    def test_non_string_coerced(self):
        err = ConnectionError("host down")
        assert scrub_secrets(err) == "host down"

    def test_plain_message_without_url_untouched(self):
        assert scrub_secrets("timeout after 30s") == "timeout after 30s"

    def test_multiple_urls_all_scrubbed(self):
        text = (
            "tried https://a:b@one.example.com then "
            "https://c:d@two.example.com?token=xyz"
        )
        result = scrub_secrets(text)
        assert "a:b" not in result
        assert "c:d" not in result
        assert "xyz" not in result


# ---------------------------------------------------------------------
# v1.8.0 (#52) credential pattern catalog
# ---------------------------------------------------------------------


CORPUS = [
    # (label, raw_message, secret_substring_that_must_disappear)
    (
        "wms_t_token",
        "Auth failed for token wms_t_AbCdEf0123456789xyzKLMNOP",
        "wms_t_AbCdEf0123456789xyzKLMNOP",
    ),
    (
        "aws_akia",
        "S3 putObject failed using AKIAIOSFODNN7EXAMPLE: AccessDenied",
        "AKIAIOSFODNN7EXAMPLE",
    ),
    (
        "bearer_header",
        "Upstream rejected: Authorization: Bearer abcdef0123456789ABCDEF.gh-pat",
        "abcdef0123456789ABCDEF.gh-pat",
    ),
    (
        "kv_password",
        "psycopg2.OperationalError: connection to host=db user=sentry password=hunter2-very-secret failed",
        "hunter2-very-secret",
    ),
    (
        "kv_apikey_hyphenated",
        "Connector failed: provider rejected api-key=ZZZZ-aaaa-1111-bbbb",
        "ZZZZ-aaaa-1111-bbbb",
    ),
    (
        "oauth_token",
        'NetSuite SOAP error: oauth_token="ABCDEFGHIJKLMNOP1234567890abcdef"',
        "ABCDEFGHIJKLMNOP1234567890abcdef",
    ),
    (
        "jwt",
        "Upstream returned 401 with token "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.signaturepartABCDEFGHIJK",
        "eyJhbGciOiJIUzI1NiJ9",
    ),
    (
        "long_after_keyword",
        "Provider rejected our key: 0123456789abcdefABCDEFGHIJ0123456789xyz",
        "0123456789abcdefABCDEFGHIJ0123456789xyz",
    ),
]


class TestCredentialPatterns:
    @pytest.mark.parametrize("label,raw,secret", CORPUS, ids=[c[0] for c in CORPUS])
    def test_secret_redacted(self, label, raw, secret):
        result = scrub_secrets(raw)
        assert secret not in result, (
            f"{label}: secret survived scrubbing.\n  in : {raw}\n  out: {result}"
        )

    def test_aws_redacted_form_kept(self):
        # Confirm the redacted token appears so debugging context is
        # preserved (we do not strip the prefix entirely).
        result = scrub_secrets("AKIAIOSFODNN7EXAMPLE leaked")
        assert "AKIA<REDACTED>" in result

    def test_bearer_redacted_form_kept(self):
        result = scrub_secrets("Bearer abcdefghijklmnopqrstuvwxyz0123")
        assert "Bearer <REDACTED>" in result

    def test_jwt_redacted_form_kept(self):
        result = scrub_secrets(
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signaturepart"
        )
        assert "<JWT_REDACTED>" in result

    def test_plain_message_without_credentials_untouched(self):
        msg = "timeout after 30s while reading from upstream"
        assert scrub_secrets(msg) == msg

    def test_short_keyword_value_not_overredacted(self):
        # The catch-all requires 32+ alphanumeric chars after the keyword
        # so short values like password=foo are caught only by the
        # connection-string regex, not the heuristic.
        assert scrub_secrets("password=foo") == "password=<REDACTED>"

    def test_scrub_credentials_handles_empty(self):
        assert scrub_credentials("") == ""
        assert scrub_credentials(None) is None


class TestIdempotency:
    @pytest.mark.parametrize("label,raw,_secret", CORPUS, ids=[c[0] for c in CORPUS])
    def test_double_application_is_fixed_point(self, label, raw, _secret):
        once = scrub_secrets(raw)
        twice = scrub_secrets(once)
        assert once == twice, (
            f"{label}: scrub_secrets is not idempotent.\n  once : {once}\n  twice: {twice}"
        )

    def test_url_then_credential_idempotent(self):
        msg = (
            "POST https://u:p@api.example.com/v1?api_key=ZZZ failed "
            "Bearer abcdefghijklmnopqrstuvwxyz0123 also leaked"
        )
        once = scrub_secrets(msg)
        twice = scrub_secrets(once)
        assert once == twice
