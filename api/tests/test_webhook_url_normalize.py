"""Unit tests for canonicalize_delivery_url (#218)."""

import os
import sys

os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
os.environ.setdefault(
    "SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8="
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from services.webhook_dispatcher.url_normalize import canonicalize_delivery_url


@pytest.mark.parametrize(
    "url, canonical",
    [
        # Scheme + host case fold.
        ("HTTPS://Example.com/hook", "https://example.com/hook"),
        ("HTTPS://EXAMPLE.com/hook", "https://example.com/hook"),
        # Default port stripping.
        ("https://example.com:443/hook", "https://example.com/hook"),
        ("http://example.com:80/hook", "http://example.com/hook"),
        # Non-default port preserved.
        ("https://example.com:8443/hook", "https://example.com:8443/hook"),
        ("http://example.com:8080/hook", "http://example.com:8080/hook"),
        # Fragment stripped.
        ("https://example.com/hook#anchor", "https://example.com/hook"),
        ("https://example.com/hook?a=1#anchor", "https://example.com/hook?a=1"),
        # Trailing slash collapse on non-root paths.
        ("https://example.com/hook/", "https://example.com/hook"),
        ("https://example.com/a/b/", "https://example.com/a/b"),
        # Root path normalized to '/'.
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        # Query preserved verbatim.
        (
            "https://example.com/hook?b=2&a=1",
            "https://example.com/hook?b=2&a=1",
        ),
        # Combined: scheme case + default port + fragment + trailing slash.
        (
            "HTTPS://Example.COM:443/hook/#anchor",
            "https://example.com/hook",
        ),
    ],
)
def test_canonicalization_table(url, canonical):
    assert canonicalize_delivery_url(url) == canonical


def test_canonicalization_is_idempotent():
    """Running the canonical form back through the helper must not
    further mutate it; the partial unique index relies on this."""
    inputs = [
        "HTTPS://Example.COM:443/hook/#anchor",
        "https://example.com/",
        "https://example.com:8443/hook",
        "http://example.com/x/y/",
    ]
    for raw in inputs:
        once = canonicalize_delivery_url(raw)
        twice = canonicalize_delivery_url(once)
        assert once == twice, raw


def test_canonicalization_preserves_userinfo_verbatim():
    """Userinfo is not part of the canonicalization rules; the SSRF
    guard inspects the resolved host rather than the URL string,
    so the helper passes credentials through untouched."""
    url = "https://USER:PASS@example.com/hook"
    out = canonicalize_delivery_url(url)
    assert out.startswith("https://USER:PASS@")
    assert out.endswith("example.com/hook")
