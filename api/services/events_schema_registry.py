"""JSON Schema registry for v1.5.0 integration events.

Loads every ``docs/events/<event_type>/<version>.json`` at import time,
validates each as JSON Schema Draft 2020-12, and exposes ``get_validator``
for ``emit_event`` to call before inserting into ``integration_events``.

Boot-time guarantees:

- Every entry in ``V150_CATALOG`` must have a matching schema file. A
  missing schema is a boot failure, not a runtime surprise.
- Every schema file must validate as Draft 2020-12. A malformed schema
  is a boot failure.
- Either failure raises at import; the app never starts with a broken
  catalog. CI exercises this via a dedicated step (issue #111).
"""

import json
import os
from typing import Dict, Tuple

from jsonschema import Draft202012Validator

# Seven v1.5.0 event types. Each entry is (event_type, version) and the
# registry requires a matching docs/events/<event_type>/<version>.json
# file. Adding a type here without shipping the file fails boot.
V150_CATALOG: Tuple[Tuple[str, int], ...] = (
    ("receipt.completed", 1),
    ("adjustment.applied", 1),
    ("transfer.completed", 1),
    ("pick.confirmed", 1),
    ("pack.confirmed", 1),
    ("ship.confirmed", 1),
    ("cycle_count.adjusted", 1),
)

# Resolved once at module import from <repo>/docs/events. The api/
# package sits at <repo>/api, so docs/ is two levels up from this file.
_SCHEMAS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "events")
)

_validators: Dict[Tuple[str, int], Draft202012Validator] = {}


def _load_all() -> None:
    """Load and validate every schema in V150_CATALOG. Raises on any failure."""
    _validators.clear()
    for event_type, version in V150_CATALOG:
        path = os.path.join(_SCHEMAS_DIR, event_type, f"{version}.json")
        if not os.path.exists(path):
            raise RuntimeError(
                f"events_schema_registry: missing schema file for "
                f"({event_type!r}, {version}) at {path}"
            )
        with open(path, "r", encoding="utf-8") as f:
            try:
                schema = json.load(f)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"events_schema_registry: {path} is not valid JSON: {e}"
                ) from e
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:
            raise RuntimeError(
                f"events_schema_registry: {path} is not a valid Draft 2020-12 schema: {e}"
            ) from e
        _validators[(event_type, version)] = Draft202012Validator(schema)


# Load at import. A broken schema file or a missing catalog entry
# surfaces immediately when any caller imports this module, including
# the CI step in issue #111 that boots the registry on a fresh checkout.
_load_all()


def get_validator(event_type: str, event_version: int) -> Draft202012Validator:
    """Return the validator for (event_type, event_version).

    Raises KeyError if the pair is not registered; ``emit_event`` treats
    an unknown event type as a code bug and propagates the failure.
    """
    try:
        return _validators[(event_type, event_version)]
    except KeyError as e:
        raise KeyError(
            f"events_schema_registry: no schema registered for "
            f"({event_type!r}, {event_version})"
        ) from e


def known_types():
    """Return the list registered by V150_CATALOG, one entry per type with its versions grouped.

    Used by the GET /api/v1/events/types endpoint in #124.
    """
    grouped: Dict[str, list] = {}
    for event_type, version in V150_CATALOG:
        grouped.setdefault(event_type, []).append(version)
    return [
        {"event_type": event_type, "versions": sorted(versions)}
        for event_type, versions in grouped.items()
    ]


def schemas_dir() -> str:
    """Absolute path to docs/events; used by the schema-serving endpoint in #124."""
    return _SCHEMAS_DIR
