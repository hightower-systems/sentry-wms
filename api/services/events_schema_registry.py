"""JSON Schema registry for v1.5.0 integration events.

Loads every ``api/schemas_v1/events/<event_type>/<version>.json`` at
import time, validates each as JSON Schema Draft 2020-12, and exposes
``get_validator`` for ``emit_event`` to call before inserting into
``integration_events``.

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

# Seven v1.5.0 event types. Each entry is (event_type, version,
# aggregate_type) and the registry requires a matching
# api/schemas_v1/events/<event_type>/<version>.json file. Adding a
# type here without shipping the file fails boot. The aggregate_type
# value surfaces on the /api/v1/events/types endpoint so consumers
# can build their aggregate index without parsing every schema body.
V150_CATALOG: Tuple[Tuple[str, int, str], ...] = (
    ("receipt.completed",    1, "item_receipt"),
    ("adjustment.applied",   1, "inventory_adjustment"),
    ("transfer.completed",   1, "inventory_transfer"),
    ("pick.confirmed",       1, "sales_order"),
    ("pack.confirmed",       1, "sales_order"),
    ("ship.confirmed",       1, "sales_order"),
    ("cycle_count.adjusted", 1, "inventory_adjustment"),
)

# Resolved once at module import from api/schemas_v1/events. The
# schemas live inside the api/ package so they travel with the image
# that loads them (fixed in #137; previously at repo-root docs/events
# which the api Dockerfile's ./api/ build context never copied in).
_SCHEMAS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "schemas_v1", "events")
)

_validators: Dict[Tuple[str, int], Draft202012Validator] = {}


def _load_all() -> None:
    """Load and validate every schema in V150_CATALOG. Raises on any failure."""
    _validators.clear()
    for event_type, version, _aggregate_type in V150_CATALOG:
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


def known_types(event_types_filter=None):
    """Return the catalog for GET /api/v1/events/types.

    Groups versions per event_type and carries the aggregate_type so
    consumers can build an aggregate -> event_type index without
    parsing every schema file.

    v1.5.1 V-212 (#151): ``event_types_filter`` narrows the response
    to the intersection of V150_CATALOG and the caller's allowed
    event_types list. Passing None returns every entry (admin /
    internal callers); passing an iterable returns only the
    matching subset. An empty iterable returns an empty list,
    matching Decision S "empty = no access".
    """
    if event_types_filter is None:
        allow = None
    else:
        allow = set(event_types_filter)
    grouped: Dict[str, dict] = {}
    for event_type, version, aggregate_type in V150_CATALOG:
        if allow is not None and event_type not in allow:
            continue
        entry = grouped.setdefault(
            event_type,
            {"event_type": event_type, "versions": [], "aggregate_type": aggregate_type},
        )
        entry["versions"].append(version)
    for entry in grouped.values():
        entry["versions"].sort()
    return list(grouped.values())


def schema_path(event_type: str, version: int) -> str:
    """Absolute path to the JSON Schema file for (event_type, version).

    Returns the path whether or not the file exists; callers decide
    between 404 (no such file) and 200 (stream the bytes). Used by
    the schema-serving endpoint in #124.
    """
    return os.path.join(_SCHEMAS_DIR, event_type, f"{version}.json")


def schemas_dir() -> str:
    """Absolute path to the schemas directory; used by the schema-serving endpoint in #124."""
    return _SCHEMAS_DIR
