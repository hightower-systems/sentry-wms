"""v1.9.0 dockd shared utilities.

The dockd surface has its own body-size cap (separate from the inbound
SENTRY_INBOUND_MAX_BODY_KB) because dockd POST bodies are tiny (a few
hundred bytes for a ship or void) where inbound bodies can be large
canonical-shape JSON. A 64 KB default refuses abusive clients early
without constraining legitimate traffic.

Boot validation lives in app.create_app(); the helper here trusts the
boot guard and reads the env var on each request.
"""

import hashlib
import json
import os
from typing import Any, Mapping


SENTRY_DOCKD_MAX_BODY_KB_DEFAULT = 64
SENTRY_DOCKD_MAX_BODY_KB_FLOOR = 16
SENTRY_DOCKD_MAX_BODY_KB_CEILING = 1024


def get_max_body_kb() -> int:
    """SENTRY_DOCKD_MAX_BODY_KB env var (16..1024; default 64).

    Boot validates the range in app.create_app(); this helper is the
    read-side path the handler uses on each request and trusts the boot
    guard rather than re-clamping silently. A typo'd value would have
    refused to boot the app, so on-the-request-path we just int() it.
    """
    return int(os.getenv("SENTRY_DOCKD_MAX_BODY_KB", str(SENTRY_DOCKD_MAX_BODY_KB_DEFAULT)))


def canonical_body_sha256(body_dict: Mapping[str, Any]) -> str:
    """SHA-256 over the JSON serialization of a Pydantic-parsed body
    with idempotency_key excluded and keys sorted lexicographically.

    Excluding idempotency_key means a client retrying with the same key
    can produce byte-different JSON (whitespace, key order) and still
    get a cache hit. Including all other fields means a tracking,
    carrier, or operator change is detected as a cache miss and surfaces
    as 409 idempotency_key_reused_with_different_body.

    The caller is expected to pass a dict that has already been through
    ``model_dump(mode='json')``; that conversion turns Decimal / UUID /
    datetime into JSON-compatible primitives so json.dumps does not
    raise on the second pass.
    """
    payload = {k: v for k, v in body_dict.items() if k != "idempotency_key"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
