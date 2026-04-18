"""Base connector interface and result types for ERP/commerce integrations.

This module defines the contract that all Sentry WMS connectors must implement.
Connectors bridge external systems (NetSuite, BigCommerce, Shopify, etc.) with
the WMS by providing a standard interface for syncing orders, items, inventory,
and pushing fulfillment data back to the source system.

Result types use pydantic for validation and serialization.
"""

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import requests
from pydantic import BaseModel, Field, field_validator

# Maximum length of the human-readable ``message`` field on a
# ConnectionResult. Capping prevents a connector from returning a
# multi-kilobyte HTTP response body (which could include credentials,
# PII, or crafted content) back to the admin UI.
CONNECTION_MESSAGE_MAX_LEN = 500

# Characters permitted in a ConnectionResult message. Anything else is
# stripped before the value reaches the admin UI. We allow the 95
# printable-ASCII range plus tab/newline/CR so friendly multi-line
# status text still renders; everything else (control chars,
# non-ASCII, ANSI escapes, zero-width tricks) is dropped.
_ALLOWED_MESSAGE_CHARS = frozenset(
    chr(c) for c in range(0x20, 0x7F)
) | {"\t", "\n", "\r"}


def _sanitize_connection_message(value: str) -> str:
    """Strip non-printable bytes and truncate to CONNECTION_MESSAGE_MAX_LEN."""
    if value is None:
        return ""
    cleaned = "".join(ch for ch in str(value) if ch in _ALLOWED_MESSAGE_CHARS)
    if len(cleaned) > CONNECTION_MESSAGE_MAX_LEN:
        cleaned = cleaned[: CONNECTION_MESSAGE_MAX_LEN - 3] + "..."
    return cleaned

from connectors.rate_limiter import (
    MAX_RETRIES_PER_CALL,
    CircuitBreakerState,
    CircuitOpenError,
    RateLimitState,
    exponential_backoff,
)
from urllib.parse import urlsplit as _urlsplit

from connectors.url_guard import BlockedDestinationError, assert_url_allowed, pinned_host

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types -- returned by connector methods
# ---------------------------------------------------------------------------


class SyncResult(BaseModel):
    """Outcome of a sync operation (orders, items, or inventory).

    Connectors return this from sync_orders(), sync_items(), and
    sync_inventory() to report how many records were pulled and
    whether any errors occurred during the sync.
    """

    success: bool = Field(..., description="True if the sync completed without fatal errors")
    records_synced: int = Field(0, ge=0, description="Number of records successfully synced")
    errors: list[str] = Field(default_factory=list, description="Non-fatal error messages encountered during sync")


class PushResult(BaseModel):
    """Outcome of pushing data back to the external system.

    Returned by push_fulfillment() to confirm that the ERP received
    the shipment/tracking data. external_id is the identifier the
    ERP assigned (e.g. NetSuite fulfillment ID).
    """

    success: bool = Field(..., description="True if the push was accepted by the external system")
    external_id: Optional[str] = Field(None, description="ID assigned by the external system, if any")
    error: Optional[str] = Field(None, description="Error message if the push failed")


class ConnectionResult(BaseModel):
    """Outcome of a connection test.

    Returned by test_connection() so the admin panel can show
    whether credentials and endpoints are valid before enabling a connector.

    The ``message`` field is intentionally short. Connector authors MUST
    summarize the outcome (e.g. "Connected as Acme Corp") rather than
    pasting raw HTTP response bodies. Anything longer than
    CONNECTION_MESSAGE_MAX_LEN is truncated; control characters and
    non-ASCII bytes are stripped before storage so a malicious or
    misconfigured upstream cannot smuggle exfil payloads back through
    the admin UI.
    """

    connected: bool = Field(..., description="True if the connection test succeeded")
    message: str = Field(..., description="Human-readable status message")

    @field_validator("message", mode="before")
    @classmethod
    def _clamp_message(cls, v):
        return _sanitize_connection_message(v)


# ---------------------------------------------------------------------------
# Abstract base class -- the interface contract
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Abstract base class that all connectors must implement.

    Each connector represents an integration with one external system.
    Subclasses must implement every abstract method. The registry will
    refuse to register a class that does not fully implement this interface.

    Connectors are mostly stateless but do hold rate limit and circuit
    breaker state between calls on the same instance. New instances
    start fresh (e.g. between celery task runs).

    Subclasses can override the rate limit header names if their API
    uses non-standard headers. The circuit breaker threshold and
    cooldown are tunable too.
    """

    # Rate limit header names - override in subclasses if the API uses
    # non-standard header names (e.g. Shopify uses X-Shopify-Shop-Api-Call-Limit)
    rate_limit_remaining_header: str = "X-RateLimit-Remaining"
    rate_limit_limit_header: str = "X-RateLimit-Limit"
    retry_after_header: str = "Retry-After"

    # Circuit breaker tuning
    circuit_breaker_threshold: int = 5       # consecutive failures before opening
    circuit_breaker_cooldown: int = 300      # 5 minutes

    # Proactive slowdown threshold: when rate_limit_remaining drops below
    # this fraction of the limit, add a small delay before the next call
    rate_limit_slowdown_threshold: float = 0.1

    def __init__(self, config: dict):
        """Initialize the connector with its configuration.

        Args:
            config: Dictionary of settings for this connector instance
                    (API keys, base URLs, tenant IDs, etc.). The shape
                    is defined by get_config_schema().
        """
        self.config = config
        self._rate_limit = RateLimitState()
        self._circuit_breaker = CircuitBreakerState(
            threshold=self.circuit_breaker_threshold,
            cooldown_seconds=self.circuit_breaker_cooldown,
        )

    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP wrapper with retry, backoff, rate limit awareness, and circuit breaking.

        Connectors should call this instead of requests.get/post directly.
        Every connector automatically gets:
        - 3 retries with exponential backoff on 429/503
        - Proactive slowdown when X-RateLimit-Remaining drops below 10%
        - Circuit breaker that opens after 5 consecutive failures
        - Retry-After header compliance

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full URL to request
            **kwargs: Passed through to requests.request (headers, json, params, timeout, etc.)

        Returns:
            requests.Response on success (2xx or 4xx other than 429).

        Raises:
            CircuitOpenError: If the breaker is open and cooldown hasn't expired.
            requests.HTTPError: For final 5xx responses after exhausting retries.
            requests.RequestException: For network-level errors after exhausting retries.
        """
        # V-009 + V-108: reject internal / private / loopback destinations
        # before issuing the HTTP call, and pin DNS resolution to the
        # validated IP so an attacker-controlled DNS server cannot return
        # a public IP on the guard lookup and a private IP on the actual
        # connection. assert_url_allowed returns the IP to pin; the
        # retry loop below runs inside pinned_host(hostname, ip) so
        # every attempt uses the same validated address.
        pinned_ip = assert_url_allowed(url)
        request_hostname = (_urlsplit(url).hostname or "").lower()

        # Fail fast if the circuit is open
        self._circuit_breaker.check()

        # Proactive slowdown based on last response's rate limit headers
        slowdown = self._rate_limit.compute_slowdown(self.rate_limit_slowdown_threshold)
        if slowdown > 0:
            logger.info("Rate limit slowdown: waiting %.2fs", slowdown)
            time.sleep(slowdown)

        # Honor Retry-After from the previous response
        if self._rate_limit.retry_after and self._rate_limit.retry_after > 0:
            wait = self._rate_limit.retry_after
            self._rate_limit.retry_after = None  # consume it
            logger.info("Honoring Retry-After: waiting %.2fs", wait)
            time.sleep(wait)

        last_exception = None
        with pinned_host(request_hostname, pinned_ip):
            for attempt in range(MAX_RETRIES_PER_CALL):
                try:
                    response = requests.request(method, url, **kwargs)
                except requests.RequestException as exc:
                    last_exception = exc
                    logger.warning(
                        "Request error on attempt %d: %s", attempt + 1, exc,
                    )
                    self._circuit_breaker.record_failure()
                    if attempt + 1 < MAX_RETRIES_PER_CALL:
                        time.sleep(exponential_backoff(attempt))
                    continue

                # Update rate limit state from response headers
                self._rate_limit.update_from_response(
                    response,
                    self.rate_limit_remaining_header,
                    self.rate_limit_limit_header,
                    self.retry_after_header,
                )

                # 429 (rate limited) and 503 (service unavailable) are retryable
                if response.status_code in (429, 503) and attempt + 1 < MAX_RETRIES_PER_CALL:
                    # Respect Retry-After if the server set one, otherwise exponential backoff
                    if self._rate_limit.retry_after and self._rate_limit.retry_after > 0:
                        delay = self._rate_limit.retry_after
                        self._rate_limit.retry_after = None
                    else:
                        delay = exponential_backoff(attempt)
                    logger.info(
                        "Got %d, retrying in %.2fs (attempt %d/%d)",
                        response.status_code, delay, attempt + 1, MAX_RETRIES_PER_CALL,
                    )
                    time.sleep(delay)
                    continue

                # Success or non-retryable failure - update breaker and return
                if response.status_code < 500:
                    self._circuit_breaker.record_success()
                else:
                    self._circuit_breaker.record_failure()

                return response

            # Exhausted all retries with a connection error
            self._circuit_breaker.record_failure()
            if last_exception is not None:
                raise last_exception
            # Should not reach here, but return the last 429/503 response just in case
            return response

    @abstractmethod
    def sync_orders(self, since: datetime) -> SyncResult:
        """Pull new or updated sales orders from the external system.

        Args:
            since: Only fetch orders created or modified after this timestamp.

        Returns:
            SyncResult with the count of orders synced and any errors.
        """

    @abstractmethod
    def sync_items(self, since: datetime) -> SyncResult:
        """Pull item master data (SKUs, descriptions, UPCs) from the external system.

        Args:
            since: Only fetch items created or modified after this timestamp.

        Returns:
            SyncResult with the count of items synced and any errors.
        """

    @abstractmethod
    def sync_inventory(self, since: datetime) -> SyncResult:
        """Pull inventory levels from the external system.

        Some systems push inventory to the WMS (e.g. initial stock counts),
        others are pull-only. Connectors that don't support this should
        omit 'sync_inventory' from get_capabilities() and return
        SyncResult(success=True, records_synced=0) here.

        Args:
            since: Only fetch inventory changes after this timestamp.

        Returns:
            SyncResult with the count of inventory records synced and any errors.
        """

    @abstractmethod
    def push_fulfillment(self, order_id: str, tracking: str, carrier: str) -> PushResult:
        """Push shipment confirmation back to the external system.

        Called after Sentry WMS ships an order. The connector should
        create a fulfillment record in the ERP with the tracking info.

        Args:
            order_id: The external system's order identifier.
            tracking: Tracking number for the shipment.
            carrier: Carrier name (e.g. "UPS", "FedEx").

        Returns:
            PushResult with the external fulfillment ID if successful.
        """

    @abstractmethod
    def test_connection(self) -> ConnectionResult:
        """Verify that the connector's credentials and endpoints are valid.

        Called from the admin panel when setting up or troubleshooting
        a connector. Should make a lightweight API call (e.g. fetch
        account info) to confirm the connection works.

        Returns:
            ConnectionResult indicating success or failure with a message.
        """

    @abstractmethod
    def get_config_schema(self) -> dict:
        """Return the configuration fields this connector needs.

        The admin panel uses this to render a setup form. Each key is
        a field name, and the value describes the field.

        Example return value::

            {
                "api_key": {"type": "string", "required": True, "label": "API Key"},
                "base_url": {"type": "string", "required": True, "label": "API Base URL"},
                "account_id": {"type": "string", "required": False, "label": "Account ID"},
            }

        Returns:
            Dict mapping field names to their type/label/required metadata.
        """

    @abstractmethod
    def get_capabilities(self) -> list[str]:
        """Declare which operations this connector supports.

        Not all external systems support all sync directions. For example,
        a POS system might only support sync_orders and push_fulfillment
        but not sync_items or sync_inventory.

        Valid capability strings:
            - "sync_orders"
            - "sync_items"
            - "sync_inventory"
            - "push_fulfillment"

        Returns:
            List of capability strings this connector supports.
        """
