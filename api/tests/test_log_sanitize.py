"""
Tests for V-007 credential scrubber used in Celery error paths.
"""

from utils.log_sanitize import scrub_secrets


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
