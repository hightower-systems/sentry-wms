"""Catalog-coverage tests (#204).

These guard the structural-fix invariant: every error_kind the
dispatcher emits must have a server-owned catalog entry, and the
admin endpoint must never surface raw consumer-controlled bytes.
"""

import pytest

from services.webhook_dispatcher import error_catalog
from services.webhook_dispatcher import http_client as http_client_module


_REQUIRED_KEYS = ("short_message", "description", "triage_hint")


class TestCatalogShape:
    @pytest.mark.parametrize("kind", error_catalog.all_kinds())
    def test_every_kind_has_required_keys(self, kind):
        entry = error_catalog.get_entry(kind)
        for key in _REQUIRED_KEYS:
            assert key in entry and entry[key], (
                f"catalog entry for {kind!r} missing/empty {key!r}"
            )

    def test_unknown_kind_falls_back_to_unknown_entry(self):
        entry = error_catalog.get_entry("not-a-real-kind")
        assert entry is error_catalog.get_entry("unknown")


class TestCatalogCoversDispatcherKinds:
    """classify_exception and classify_status_code must never emit
    a kind missing from the catalog. A new error class introduced
    without a catalog entry would land an admin row with a None
    description; this test fails loudly first."""

    def test_classify_exception_kinds_in_catalog(self):
        import requests
        kinds = set()
        for exc in (
            requests.exceptions.SSLError("x"),
            requests.exceptions.Timeout("x"),
            requests.exceptions.ConnectTimeout("x"),
            requests.exceptions.ReadTimeout("x"),
            requests.exceptions.ConnectionError("x"),
            requests.exceptions.TooManyRedirects("x"),
            ValueError("x"),
        ):
            kind, _ = http_client_module.classify_exception(exc)
            kinds.add(kind)
        missing = kinds - set(error_catalog.all_kinds())
        assert missing == set(), f"kinds without catalog entries: {missing}"

    def test_classify_status_code_kinds_in_catalog(self):
        kinds = {
            http_client_module.classify_status_code(s)
            for s in (302, 400, 404, 422, 500, 502, 504)
        }
        missing = kinds - set(error_catalog.all_kinds())
        assert missing == set(), f"kinds without catalog entries: {missing}"

    def test_ssrf_rejected_in_catalog(self):
        # ssrf_rejected is set in http_client.send when ssrf_guard
        # rejects the URL; it does not flow through classify_*.
        # Guard it explicitly.
        assert "ssrf_rejected" in error_catalog.all_kinds()
