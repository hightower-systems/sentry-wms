"""Mapping loader tests (v1.7.0).

Covers:
- strict-typed Pydantic loading (extra='forbid' at every level)
- version_compare required, no default
- field_set() returns the canonical-field set per resource
- JSONPath resolution: hit, default fallback, required-true miss raises
- derived expression: arithmetic + when_present fallback
- derived expression eval-rejection (R9): __import__, attribute-walks,
  exec / eval, file system reach attempts all fail
- cross_system_lookup: hit, required-false miss returns None,
  required-true miss raises CrossSystemLookupMiss
- line_items: list flattening + per-row field resolution
- filename / source_system mismatch refuses to load
- duplicate source_system across files refuses to register
"""

import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.mapping_loader import (  # noqa: E402
    CrossSystemLookupMiss,
    MappingDocument,
    MappingRegistry,
    apply,
    load_directory,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def tmp_mappings_dir(tmp_path):
    d = tmp_path / "mappings"
    d.mkdir()
    return d


def _write(dir_: Path, source_system: str, body: str) -> Path:
    p = dir_ / f"{source_system}.yaml"
    p.write_text(body)
    return p


def _basic_doc(source_system="acme") -> str:
    return f"""\
mapping_version: "1.0"
source_system: "{source_system}"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "string"
      - canonical: "phone"
        source_path: "$.contact.phone"
        type: "string"
"""


# ----------------------------------------------------------------------
# Strict loading
# ----------------------------------------------------------------------


class TestStrictLoading:
    def test_basic_doc_loads(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _basic_doc())
        registry = load_directory(tmp_mappings_dir)
        doc = registry.for_source("acme")
        assert isinstance(doc, MappingDocument)
        assert doc.version_compare == "iso_timestamp"

    def test_extra_top_level_key_refuses(self, tmp_mappings_dir):
        body = _basic_doc() + 'extra_key: "nope"\n'
        _write(tmp_mappings_dir, "acme", body)
        with pytest.raises(Exception):
            load_directory(tmp_mappings_dir)

    def test_extra_field_level_key_refuses(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "string"
        bogus_attr: true
"""
        _write(tmp_mappings_dir, "acme", body)
        with pytest.raises(Exception):
            load_directory(tmp_mappings_dir)

    def test_unknown_field_type_refuses(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "barfle"
"""
        _write(tmp_mappings_dir, "acme", body)
        with pytest.raises(Exception):
            load_directory(tmp_mappings_dir)

    def test_filename_mismatch_refuses(self, tmp_mappings_dir):
        # File named acme.yaml but source_system inside is 'fabric'.
        body = _basic_doc(source_system="fabric")
        (tmp_mappings_dir / "acme.yaml").write_text(body)
        with pytest.raises(ValueError, match="filename stem"):
            load_directory(tmp_mappings_dir)

    def test_duplicate_source_system_across_files_refuses(self, tmp_mappings_dir):
        registry = MappingRegistry()
        _write(tmp_mappings_dir, "acme", _basic_doc())
        load_directory(tmp_mappings_dir, registry=registry)
        # Second pass simulates a duplicate; loader's register() rejects.
        _write(tmp_mappings_dir, "acme", _basic_doc())
        with pytest.raises(ValueError, match="duplicate"):
            load_directory(tmp_mappings_dir, registry=registry)


# ----------------------------------------------------------------------
# version_compare required
# ----------------------------------------------------------------------


class TestVersionCompareRequired:
    def test_missing_version_compare_refuses(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "string"
"""
        _write(tmp_mappings_dir, "acme", body)
        with pytest.raises(Exception):
            load_directory(tmp_mappings_dir)

    def test_unknown_version_compare_strategy_refuses(self, tmp_mappings_dir):
        body = _basic_doc().replace("iso_timestamp", "natural_sort")
        _write(tmp_mappings_dir, "acme", body)
        with pytest.raises(Exception):
            load_directory(tmp_mappings_dir)


# ----------------------------------------------------------------------
# field_set
# ----------------------------------------------------------------------


class TestFieldSet:
    def test_field_set_returns_canonical_names(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _basic_doc())
        registry = load_directory(tmp_mappings_dir)
        doc = registry.for_source("acme")
        assert doc.field_set("customers") == {"email", "phone"}

    def test_field_set_unknown_resource_returns_empty(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _basic_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        assert doc.field_set("vendors") == set()

    def test_field_set_excludes_line_items_fields(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "external_order_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
    line_items:
      source_path: "$.lineItems"
      canonical_path: "lines"
      fields:
        - canonical: "item_id"
          source_path: "$.sku"
          type: "string"
        - canonical: "quantity"
          source_path: "$.qty"
          type: "integer"
"""
        _write(tmp_mappings_dir, "acme", body)
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        assert doc.field_set("sales_orders") == {"external_order_number"}


# ----------------------------------------------------------------------
# JSONPath resolution
# ----------------------------------------------------------------------


class TestJsonPath:
    def test_hit_extracts_value(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _basic_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(
            doc, "customers",
            {"contact": {"email": "user@example.com", "phone": "555-1212"}},
        )
        assert out == {"email": "user@example.com", "phone": "555-1212"}

    def test_default_fills_when_path_misses(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "is_active"
        source_path: "$.isActive"
        type: "boolean"
        default: true
"""
        _write(tmp_mappings_dir, "acme", body)
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "customers", {})
        assert out == {"is_active": True}

    def test_required_true_miss_raises(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "string"
        required: true
"""
        _write(tmp_mappings_dir, "acme", body)
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="required path missing"):
            apply(doc, "customers", {"contact": {}})


# ----------------------------------------------------------------------
# Derived expressions
# ----------------------------------------------------------------------


def _doc_with_derived(expression: str, when_present: str | None = None) -> str:
    # Single-quoted YAML so embedded double quotes pass through unescaped.
    expr_q = expression.replace("'", "''")
    when_block = ""
    if when_present is not None:
        wp_q = when_present.replace("'", "''")
        when_block = f"          when_present: '{wp_q}'\n"
    return f"""\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "total_cents"
        type: "integer"
        source_path: "$.fallback"
        derived:
          expression: '{expr_q}'
{when_block}"""


class TestDerivedExpressions:
    def test_arithmetic(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _doc_with_derived("source.dollars * 100"))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "customers", {"dollars": 12.34})
        assert out == {"total_cents": 1234.0}

    def test_when_present_fallback(self, tmp_mappings_dir):
        # when_present checks $.dollars; when absent, fall back to
        # source_path $.fallback.
        _write(
            tmp_mappings_dir, "acme",
            _doc_with_derived("source.dollars * 100", when_present="$.dollars"),
        )
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "customers", {"fallback": 5})
        assert out == {"total_cents": 5}

    def test_function_whitelist_allows_known(self, tmp_mappings_dir):
        _write(
            tmp_mappings_dir, "acme",
            _doc_with_derived("round(source.dollars * 100)"),
        )
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "customers", {"dollars": 12.345})
        assert out == {"total_cents": 1234}

    def test_eval_rejection_dunder_import(self, tmp_mappings_dir):
        _write(
            tmp_mappings_dir, "acme",
            _doc_with_derived('__import__("os").system("echo pwn")'),
        )
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="derived expression"):
            apply(doc, "customers", {})

    def test_eval_rejection_eval_call(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _doc_with_derived('eval("1+1")'))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="derived expression"):
            apply(doc, "customers", {})

    def test_eval_rejection_attribute_walk(self, tmp_mappings_dir):
        # Walking ().__class__.__bases__ etc. is the classic sandbox break.
        _write(
            tmp_mappings_dir, "acme",
            _doc_with_derived("().__class__.__bases__"),
        )
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="derived expression"):
            apply(doc, "customers", {})

    def test_eval_rejection_bare_open(self, tmp_mappings_dir):
        _write(
            tmp_mappings_dir, "acme",
            _doc_with_derived('open("/etc/passwd")'),
        )
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="derived expression"):
            apply(doc, "customers", {})


# ----------------------------------------------------------------------
# Cross-system lookup
# ----------------------------------------------------------------------


def _doc_with_cross_lookup(required: bool = True) -> str:
    return f"""\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "customer_id"
        source_path: "$.customer.id"
        type: "uuid"
        required: {str(required).lower()}
        cross_system_lookup:
          source_type: "customer"
          source_system: "acme"
"""


class TestCrossSystemLookup:
    def test_hit_returns_canonical_id(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _doc_with_cross_lookup(required=True))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        canonical_uuid = uuid4()

        def lookup(ss, st, sid):
            assert (ss, st, sid) == ("acme", "customer", "C-1")
            return canonical_uuid

        out = apply(
            doc, "sales_orders",
            {"customer": {"id": "C-1"}},
            lookup_fn=lookup,
        )
        assert out == {"customer_id": canonical_uuid}

    def test_required_false_miss_returns_none(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _doc_with_cross_lookup(required=False))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(
            doc, "sales_orders",
            {"customer": {"id": "C-1"}},
            lookup_fn=lambda *_: None,
        )
        assert out == {"customer_id": None}

    def test_required_true_miss_raises_typed_exception(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _doc_with_cross_lookup(required=True))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(CrossSystemLookupMiss) as ei:
            apply(
                doc, "sales_orders",
                {"customer": {"id": "C-1"}},
                lookup_fn=lambda *_: None,
            )
        miss = ei.value
        assert miss.source_system == "acme"
        assert miss.source_type == "customer"
        assert miss.source_id == "C-1"


# ----------------------------------------------------------------------
# Line items
# ----------------------------------------------------------------------


class TestLineItems:
    def test_line_items_flatten_to_canonical_path(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "external_order_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
    line_items:
      source_path: "$.lineItems"
      canonical_path: "lines"
      fields:
        - canonical: "sku"
          source_path: "$.sku"
          type: "string"
        - canonical: "quantity"
          source_path: "$.qty"
          type: "integer"
"""
        _write(tmp_mappings_dir, "acme", body)
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(
            doc, "sales_orders",
            {
                "orderNumber": "SO-1",
                "lineItems": [
                    {"sku": "A", "qty": 1},
                    {"sku": "B", "qty": 2},
                ],
            },
        )
        assert out["external_order_number"] == "SO-1"
        assert out["lines"] == [
            {"sku": "A", "quantity": 1},
            {"sku": "B", "quantity": 2},
        ]


# ----------------------------------------------------------------------
# v1.8.0 (#285) per-field decimal coercion + bounds
# ----------------------------------------------------------------------


from decimal import Decimal


def _decimal_doc(extra_attrs: str = "", value_path: str = "$.amount") -> str:
    return f"""\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "so_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "order_total"
        source_path: "{value_path}"
        type: "decimal"
{extra_attrs}"""


class TestDecimalCoercion:
    def test_no_bounds_passes_value_through_as_decimal(self, tmp_mappings_dir):
        # Backward-compat for existing decimal mappings: no bounds
        # declared -> pass-through behaviour, but we now coerce to
        # Decimal so downstream Postgres NUMERIC handling is explicit.
        _write(tmp_mappings_dir, "acme", _decimal_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "sales_orders", {
            "orderNumber": "SO-1", "warehouseId": 1, "amount": "12.99",
        })
        assert out["order_total"] == Decimal("12.99")

    def test_int_value_coerces_to_decimal(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "sales_orders", {
            "orderNumber": "SO-1", "warehouseId": 1, "amount": 42,
        })
        assert out["order_total"] == Decimal("42")

    def test_float_value_coerces_to_decimal_via_str(self, tmp_mappings_dir):
        # Decimal(str(value)) avoids the float -> Decimal binary
        # representation gotcha (Decimal(0.1) is 0.1000000000000000055...).
        _write(tmp_mappings_dir, "acme", _decimal_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "sales_orders", {
            "orderNumber": "SO-1", "warehouseId": 1, "amount": 0.1,
        })
        assert out["order_total"] == Decimal("0.1")

    def test_non_numeric_string_raises(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="cannot coerce"):
            apply(doc, "sales_orders", {
                "orderNumber": "SO-1", "warehouseId": 1, "amount": "not a number",
            })

    def test_bool_rejected_explicitly(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="boolean"):
            apply(doc, "sales_orders", {
                "orderNumber": "SO-1", "warehouseId": 1, "amount": True,
            })

    def test_null_value_passes_through_as_none(self, tmp_mappings_dir):
        # type='decimal' on a non-required field with no source path hit
        # should leave the canonical column NULL, matching Postgres
        # nullable column semantics.
        _write(tmp_mappings_dir, "acme", _decimal_doc())
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "sales_orders", {
            "orderNumber": "SO-1", "warehouseId": 1,
        })
        assert "order_total" not in out or out["order_total"] is None


class TestDecimalBounds:
    def test_decimal_places_violation_raises(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc(
            extra_attrs="        decimal_places: 2\n",
        ))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError) as exc:
            apply(doc, "sales_orders", {
                "orderNumber": "SO-1", "warehouseId": 1, "amount": "12.345",
            })
        assert "order_total" in str(exc.value)
        assert "decimal place" in str(exc.value)

    def test_decimal_places_at_limit_accepted(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc(
            extra_attrs="        decimal_places: 2\n",
        ))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "sales_orders", {
            "orderNumber": "SO-1", "warehouseId": 1, "amount": "12.99",
        })
        assert out["order_total"] == Decimal("12.99")

    def test_max_digits_violation_raises(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc(
            extra_attrs="        max_digits: 12\n",
        ))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="significant digit"):
            apply(doc, "sales_orders", {
                "orderNumber": "SO-1", "warehouseId": 1,
                "amount": "12345678901234",  # 14 digits
            })

    def test_ge_violation_raises(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc(
            extra_attrs='        ge: "0"\n',
        ))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="ge="):
            apply(doc, "sales_orders", {
                "orderNumber": "SO-1", "warehouseId": 1, "amount": "-0.01",
            })

    def test_le_violation_raises(self, tmp_mappings_dir):
        _write(tmp_mappings_dir, "acme", _decimal_doc(
            extra_attrs='        le: "9999999999.99"\n',
        ))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        with pytest.raises(ValueError, match="le="):
            apply(doc, "sales_orders", {
                "orderNumber": "SO-1", "warehouseId": 1,
                "amount": "10000000000.00",
            })

    def test_all_bounds_combined_happy_path(self, tmp_mappings_dir):
        # The exact bounds the v1.8 sales_orders order_total +
        # customer_shipping_paid columns deserve (mig 050: NUMERIC(12,2)).
        _write(tmp_mappings_dir, "acme", _decimal_doc(
            extra_attrs=(
                "        max_digits: 12\n"
                "        decimal_places: 2\n"
                '        ge: "0"\n'
                '        le: "9999999999.99"\n'
            ),
        ))
        doc = load_directory(tmp_mappings_dir).for_source("acme")
        out = apply(doc, "sales_orders", {
            "orderNumber": "SO-1", "warehouseId": 1, "amount": "9999999999.99",
        })
        assert out["order_total"] == Decimal("9999999999.99")

    def test_bounds_on_non_decimal_type_refused_at_load(self, tmp_mappings_dir):
        body = """\
mapping_version: "1.0"
source_system: "acme"
version_compare: "iso_timestamp"
resources:
  customers:
    canonical_type: "customer"
    fields:
      - canonical: "email"
        source_path: "$.contact.email"
        type: "string"
        max_digits: 12
"""
        _write(tmp_mappings_dir, "acme", body)
        with pytest.raises(ValueError, match="only valid for type='decimal'"):
            load_directory(tmp_mappings_dir).for_source("acme")
