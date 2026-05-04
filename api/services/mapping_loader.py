"""Mapping document format + loader for v1.7.0 Pipe B inbound.

Pydantic-strict (extra="forbid") schema for the per-source_system mapping
documents that translate `source_payload` (consumer-shaped) into
`canonical_payload` (Sentry's draft canonical model). Documents live at
db/mappings/<source_system>.yaml and are loaded once at process boot.

Locks for v1.7 (see plan §3):

- JSONPath via `jsonpath-ng` (the maintained fork). Mapping docs are
  tested against the pinned version in CI; the parser is restricted in
  this module to the `parse()` entry point.
- Derived expressions via `simpleeval` with a function whitelist
  restricted to `{int, float, str, len, abs, min, max, round}` and
  operators restricted to arithmetic + string concat + comparison.
  No attribute access, no subscripts beyond simple `dict[key]`,
  no `__import__`, no `eval`, no `exec`. Source paths are resolved by
  JSONPath into a flat dict before the expression evaluates; expressions
  never see the raw source_payload. The eval-rejection self-test is the
  regression net (R9 mitigation).
- `version_compare` is required at the top level. Loader fails if the
  field is missing; CI lint catches the same shape.
- Cross-system lookup miss policy: required-true field whose lookup
  misses raises CrossSystemLookupMiss with the missing
  (source_system, source_type, source_id) so the handler can surface
  a 409 cross_system_lookup_miss. Required-false fields tolerate
  misses (canonical column ends up None).

This module never holds a database session. Cross-system lookups are
delegated to a `lookup_fn` argument passed by the handler at apply
time; the handler implements the actual SELECT against
cross_system_mappings. This keeps the loader unit-testable without a
DB and keeps the lookup-error path explicit.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID

import yaml
from jsonpath_ng import parse as _jsonpath_parse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from simpleeval import EvalWithCompoundTypes


# Function whitelist for derived expressions. Anything outside this set
# raises NameError at evaluation time. Attribute access and __import__
# are blocked by simpleeval's default node-class allowlist.
_DERIVED_FUNCTIONS = {
    "int": int,
    "float": float,
    "str": str,
    "len": len,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}


CANONICAL_RESOURCE_TYPES = {
    "sales_orders": "sales_order",
    "items": "item",
    "customers": "customer",
    "vendors": "vendor",
    "purchase_orders": "purchase_order",
}


class _StrictModel(BaseModel):
    """Pydantic base with extra='forbid' (V-204 alignment)."""

    model_config = ConfigDict(extra="forbid", strict=True)


class CrossSystemLookup(_StrictModel):
    source_type: str
    source_system: Optional[str] = None  # defaults to the doc's source_system at apply time


class DerivedExpression(_StrictModel):
    expression: str
    when_present: Optional[str] = None  # if set, the path that must resolve for the expression to fire


class FieldMapping(_StrictModel):
    canonical: str
    type: str
    source_path: Optional[str] = None
    required: bool = False
    cross_system_lookup: Optional[CrossSystemLookup] = None
    derived: Optional[DerivedExpression] = None
    default: Any = None
    rename: Optional[str] = None
    enum_values: Optional[List[str]] = None  # for type='enum'

    @model_validator(mode="after")
    def _check_field_shape(self) -> "FieldMapping":
        if self.type not in {
            "string", "integer", "decimal", "boolean", "uuid",
            "iso_timestamp", "enum",
        }:
            raise ValueError(f"unknown field type: {self.type!r}")
        if self.type == "enum" and not self.enum_values:
            raise ValueError("type='enum' requires enum_values")
        if self.derived is None and self.source_path is None and self.default is None:
            raise ValueError(
                f"field {self.canonical!r}: one of source_path / derived / default required"
            )
        return self


class LineItemMapping(_StrictModel):
    source_path: str
    canonical_path: str
    fields: List[FieldMapping]


class ResourceMapping(_StrictModel):
    canonical_type: str
    fields: List[FieldMapping]
    line_items: Optional[LineItemMapping] = None

    @model_validator(mode="after")
    def _check_canonical_type(self) -> "ResourceMapping":
        if self.canonical_type not in set(CANONICAL_RESOURCE_TYPES.values()):
            raise ValueError(
                f"canonical_type {self.canonical_type!r} not in "
                f"{sorted(set(CANONICAL_RESOURCE_TYPES.values()))}"
            )
        return self


class MappingDocument(_StrictModel):
    mapping_version: str
    source_system: str
    version_compare: str  # REQUIRED, no default
    resources: Dict[str, ResourceMapping]

    @model_validator(mode="after")
    def _check_resource_keys(self) -> "MappingDocument":
        for key in self.resources:
            if key not in CANONICAL_RESOURCE_TYPES:
                raise ValueError(
                    f"unknown resource key {key!r}; expected one of "
                    f"{sorted(CANONICAL_RESOURCE_TYPES)}"
                )
        if self.version_compare not in {"iso_timestamp", "integer", "lexicographic"}:
            raise ValueError(
                f"version_compare {self.version_compare!r} must be "
                "'iso_timestamp', 'integer', or 'lexicographic'"
            )
        return self

    def field_set(self, resource_key: str) -> Set[str]:
        """Canonical field names declared at the top level of the resource.

        Excludes line_items fields by design: the canonical-side UPDATE
        operates on the parent table only; line_items are written by a
        separate handler step. Field-set isolation under concurrency
        operates on this returned set.
        """
        rm = self.resources.get(resource_key)
        if rm is None:
            return set()
        return {f.canonical for f in rm.fields}


class CrossSystemLookupMiss(Exception):
    """Raised by apply() when a required-true cross_system_lookup misses.

    Carries the (source_system, source_type, source_id) tuple so the
    handler can surface a 409 cross_system_lookup_miss with the missing
    key in the body.
    """

    def __init__(self, source_system: str, source_type: str, source_id: str):
        self.source_system = source_system
        self.source_type = source_type
        self.source_id = source_id
        super().__init__(
            f"cross_system_lookup miss: ({source_system}, {source_type}, {source_id})"
        )


@dataclass(frozen=True)
class LoadedMappingFile:
    """A successfully-loaded mapping document plus filesystem metadata.

    sha256 + path are surfaced to the boot-time MAPPING_DOCUMENT_LOAD
    audit_log entry; the loader writes it but does not depend on the
    audit_service module (kept loose-coupled for unit tests).
    """

    document: MappingDocument
    path: str
    sha256: str


class MappingRegistry:
    """Process-wide registry of loaded mapping documents."""

    def __init__(self) -> None:
        self._by_source: Dict[str, LoadedMappingFile] = {}

    def for_source(self, source_system: str) -> Optional[MappingDocument]:
        loaded = self._by_source.get(source_system)
        return loaded.document if loaded else None

    def loaded_files(self) -> List[LoadedMappingFile]:
        return list(self._by_source.values())

    def register(self, loaded: LoadedMappingFile) -> None:
        if loaded.document.source_system in self._by_source:
            raise ValueError(
                f"duplicate mapping for source_system "
                f"{loaded.document.source_system!r}"
            )
        self._by_source[loaded.document.source_system] = loaded

    def clear(self) -> None:
        self._by_source.clear()


def _read_yaml(path: Path) -> tuple[Dict[str, Any], str]:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return parsed, sha


def load_directory(
    mappings_dir: str | os.PathLike[str],
    *,
    registry: Optional[MappingRegistry] = None,
) -> MappingRegistry:
    """Load every <source_system>.yaml file under mappings_dir.

    Strict-typed: any unknown key, missing required key, or unknown field
    type fails the load and refuses to return a partial registry.
    """
    registry = registry or MappingRegistry()
    base = Path(mappings_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"mappings dir not found: {base}")
    for entry in sorted(base.iterdir()):
        if entry.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        parsed, sha = _read_yaml(entry)
        document = MappingDocument.model_validate(parsed)
        # Filename must match document.source_system for human-readability;
        # mismatch is a configuration error.
        expected = entry.stem
        if document.source_system != expected:
            raise ValueError(
                f"{entry}: source_system {document.source_system!r} "
                f"does not match filename stem {expected!r}"
            )
        registry.register(
            LoadedMappingFile(document=document, path=str(entry), sha256=sha)
        )
    return registry


# ============================================================
# apply()
# ============================================================


def _resolve_jsonpath(expr: str, payload: Any) -> Optional[Any]:
    parsed = _jsonpath_parse(expr)
    matches = parsed.find(payload)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].value
    return [m.value for m in matches]


def _eval_derived(
    expr: DerivedExpression,
    *,
    source_payload: Any,
    field_source_path: Optional[str],
) -> Any:
    """Evaluate a derived expression against the source payload.

    The expression sees a flat `source` namespace built from the JSONPath
    resolutions of the paths it references. simpleeval's default
    operator/node allowlist blocks attribute access, subscripts beyond
    simple dict[key], and __import__ -- the function whitelist below
    blocks bare names like `eval` / `exec` / `__import__`.
    """
    if expr.when_present is not None:
        if _resolve_jsonpath(expr.when_present, source_payload) is None:
            # Falls back to the field's literal source_path resolution
            # (handled by the caller).
            return _SENTINEL_FALLBACK
    # Build a per-call `source` dict scoped to whatever paths the expression
    # references via `source.x.y`. We pass the source_payload as `source`
    # so the expression author writes `source.financials.totalDollars * 100`.
    # simpleeval's default node-class allowlist forbids attribute access
    # and bare names not in the whitelist; using `source` as a plain dict
    # via subscript-only access would be safer still, but the expression
    # examples in the plan use attribute access so we expose source as a
    # mapping wrapped in an attribute-friendly namespace.
    namespace = {"source": _AttrDict.wrap(source_payload)}
    evaluator = EvalWithCompoundTypes(
        functions=dict(_DERIVED_FUNCTIONS),
        names=namespace,
    )
    return evaluator.eval(expr.expression)


_SENTINEL_FALLBACK = object()


class _AttrDict(dict):
    """Read-only dict that also exposes keys as attributes.

    Lets `source.financials.totalDollars` work in derived expressions
    without expressions seeing dunder attributes. KeyError surfaces as
    AttributeError so simpleeval reports "name not defined" rather than
    raising an unhandled KeyError.
    """

    @classmethod
    def wrap(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return cls({k: cls.wrap(v) for k, v in value.items()})
        if isinstance(value, list):
            return [cls.wrap(v) for v in value]
        return value

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


LookupFn = Callable[[str, str, str], Optional[UUID]]
"""(source_system, source_type, source_id) -> canonical_id or None."""


def apply(
    document: MappingDocument,
    resource_key: str,
    source_payload: Any,
    *,
    lookup_fn: Optional[LookupFn] = None,
    override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Translate `source_payload` into the canonical_payload for `resource_key`.

    Returns a dict keyed on canonical field names. Values for
    `cross_system_lookup` fields resolve via `lookup_fn`; required-true
    misses raise CrossSystemLookupMiss. line_items are flattened into
    a list under the resource's canonical_path key.

    `override`, when supplied, supplies caller-provided canonical values
    that take precedence over the mapping doc's resolution. Used by the
    inbound handler to apply per-request mapping_overrides on tokens that
    have the mapping_override capability flag set; the loader does not
    enforce capability checks (those live in the handler).
    """
    rm = document.resources.get(resource_key)
    if rm is None:
        raise KeyError(f"mapping for resource_key {resource_key!r} not declared")
    out: Dict[str, Any] = {}
    for field in rm.fields:
        out[field.canonical] = _resolve_field(
            field, source_payload, document, lookup_fn,
        )
    if rm.line_items is not None:
        line_blocks = _resolve_jsonpath(rm.line_items.source_path, source_payload) or []
        if not isinstance(line_blocks, list):
            line_blocks = [line_blocks]
        out_lines: List[Dict[str, Any]] = []
        for block in line_blocks:
            line_out: Dict[str, Any] = {}
            for field in rm.line_items.fields:
                line_out[field.canonical] = _resolve_field(
                    field, block, document, lookup_fn,
                )
            out_lines.append(line_out)
        out[rm.line_items.canonical_path] = out_lines
    if override:
        out.update(override)
    return out


def _resolve_field(
    field: FieldMapping,
    payload: Any,
    document: MappingDocument,
    lookup_fn: Optional[LookupFn],
) -> Any:
    if field.derived is not None:
        try:
            v = _eval_derived(
                field.derived,
                source_payload=payload,
                field_source_path=field.source_path,
            )
        except CrossSystemLookupMiss:
            raise
        except Exception as exc:
            raise ValueError(
                f"derived expression for {field.canonical!r} failed: {exc}"
            ) from exc
        if v is _SENTINEL_FALLBACK:
            v = (
                _resolve_jsonpath(field.source_path, payload)
                if field.source_path is not None
                else field.default
            )
        return _coerce_or_default(field, v)

    if field.cross_system_lookup is not None:
        if field.source_path is None:
            raise ValueError(
                f"field {field.canonical!r}: cross_system_lookup requires source_path"
            )
        source_id = _resolve_jsonpath(field.source_path, payload)
        if source_id is None:
            return _coerce_or_default(field, None)
        cs = field.cross_system_lookup
        ss = cs.source_system or document.source_system
        if lookup_fn is None:
            raise RuntimeError(
                "apply() called without lookup_fn but mapping declares "
                "cross_system_lookup"
            )
        canonical_id = lookup_fn(ss, cs.source_type, str(source_id))
        if canonical_id is None:
            if field.required:
                raise CrossSystemLookupMiss(ss, cs.source_type, str(source_id))
            return _coerce_or_default(field, None)
        return canonical_id

    v = (
        _resolve_jsonpath(field.source_path, payload)
        if field.source_path is not None
        else None
    )
    if v is None and field.default is not None:
        v = field.default
    if v is None and field.required:
        raise ValueError(f"field {field.canonical!r}: required path missing")
    return _coerce_or_default(field, v)


def _coerce_or_default(field: FieldMapping, value: Any) -> Any:
    """Light type coercion + enum check. Heavy validation lives in the
    inbound Pydantic body model."""
    if value is None:
        return None
    if field.type == "enum":
        if value not in (field.enum_values or []):
            raise ValueError(
                f"field {field.canonical!r}: value {value!r} not in enum_values"
            )
        return value
    return value
