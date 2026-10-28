import pytest
from shipyard.helpers import outbound_manifest_bytes, workbook_bytes

from services.shipyard.outbound_manifest_parser import (
    OutboundManifestParseError,
    parse_outbound_workbook,
)


def test_parse_formal_outbound_manifest_requires_material_identity():
    parsed = parse_outbound_workbook(outbound_manifest_bytes())

    assert parsed.factory_shipment_no == "CX-20261027-001"
    assert len(parsed.lines) == 1
    line = parsed.lines[0]
    assert line.source_row_no == "1"
    assert line.material_code == "MAT-001"
    assert line.item_name == "船用支架"
    assert line.item_spec == "S-100"
    assert line.demand_quantity == 4
    assert line.expected_pallet_count == 2
    assert line.expected_tag_suffixes == ("1-1", "1-2")
    assert parsed.metadata["material_contract"] == (
        "material_code_and_item_name_are_required"
    )


def test_parse_formal_outbound_rejects_blank_material_code():
    content = outbound_manifest_bytes(material_code="")

    with pytest.raises(OutboundManifestParseError) as raised:
        parse_outbound_workbook(content)

    assert raised.value.diagnostics[0]["code"] == "MISSING_REQUIRED_FIELDS"
    assert "material_code" in raised.value.diagnostics[0]["fields"]


def test_parse_formal_outbound_rejects_duplicate_rows_and_suffix_mismatch():
    duplicate = workbook_bytes(
        [
            [
                "清单行号",
                "船厂出货单号",
                "船号",
                "工程号",
                "制作图号",
                "物料编码",
                "物料名称",
                "需求数量",
            ],
            ["1", "CX-1", "H1", "H1", "D-1", "MAT-1", "支架", 2],
            ["1", "CX-1", "H1", "H1", "D-1", "MAT-1", "支架", 2],
        ]
    )
    with pytest.raises(OutboundManifestParseError) as duplicate_error:
        parse_outbound_workbook(duplicate)
    assert duplicate_error.value.diagnostics[0]["code"] == "DUPLICATE_SOURCE_ROW"

    mismatch = outbound_manifest_bytes(
        expected_pallet_count=3,
        suffixes="1-1、1-2",
    )
    with pytest.raises(OutboundManifestParseError) as suffix_error:
        parse_outbound_workbook(mismatch)
    assert suffix_error.value.diagnostics[0]["code"] == ("PALLET_SUFFIX_COUNT_MISMATCH")
