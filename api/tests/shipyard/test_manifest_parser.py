from io import BytesIO

import pytest
from openpyxl import Workbook

from services.shipyard.manifest_parser import (
    ManifestParseError,
    parse_inbound_workbook,
)


def _workbook_bytes(rows, auxiliary_rows=None, title="外高桥"):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    if auxiliary_rows is not None:
        auxiliary = workbook.create_sheet("Sheet1")
        for row in auxiliary_rows:
            auxiliary.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_parses_fixed_template_by_header_labels():
    content = _workbook_bytes(
        [
            ["测试船舶配件有限公司"],
            ["2026舾装件发货清单"],
            [
                "序号",
                "船号",
                "品名",
                "图号",
                "图名",
                "件数",
                "PO NO",
                "送货单号",
                "PO号",
                "重量",
                "螺丝",
            ],
            [
                "1",
                "H9001",
                "舾装件",
                None,
                "E063-4002（1020）：0101",
                15,
                "9122",
                "0929505",
                "SWS202510290045",
                39,
                "基座",
            ],
            [
                "2",
                "H9001",
                "舾装件",
                "931ME303EM",
                "E963-2213（1020）：0098",
                1,
                "9121",
                "0929505",
                "SWS202510290045",
                1.59,
                None,
            ],
        ],
        auxiliary_rows=[[None, None], ["辅助物料", 24]],
    )

    parsed = parse_inbound_workbook(content, source_file_name="manifest.xlsx")

    assert parsed.sheet_name == "外高桥"
    assert parsed.header_row == 3
    assert parsed.metadata["manufacturer_name"] == "测试船舶配件有限公司"
    assert parsed.metadata["ignored_sheets"] == ["Sheet1"]
    assert parsed.metadata["line_count"] == 2
    assert parsed.metadata["total_quantity"] == "16"
    assert parsed.metadata["row_contract"] == (
        "one_source_row_equals_one_ticket_or_ticket_group"
    )
    assert parsed.lines[0].production_drawing_no == ""
    assert parsed.lines[0].drawing_name == "E063-4002（1020）：0101"
    assert parsed.lines[0].delivery_note_no == "0929505"
    assert parsed.lines[0].po_no == "SWS202510290045"
    assert parsed.lines[0].declared_quantity == 15
    assert parsed.lines[1].production_drawing_no == "931ME303EM"


def test_invalid_quantity_returns_row_diagnostics():
    content = _workbook_bytes(
        [
            ["测试船舶配件有限公司"],
            [None],
            ["序号", "船号", "品名", "图名", "件数"],
            ["1", "H9002", "舾装件", "制作图A", "不是数字"],
        ]
    )

    with pytest.raises(ManifestParseError) as exc_info:
        parse_inbound_workbook(content)

    assert exc_info.value.diagnostics == [
        {
            "code": "INVALID_QUANTITY",
            "excel_row": 4,
            "ship_no": "H9002",
            "value": "不是数字",
            "message": "件数必须为正数",
        }
    ]


def test_duplicate_source_row_is_rejected():
    content = _workbook_bytes(
        [
            ["测试船舶配件有限公司"],
            [None],
            ["序号", "船号", "品名", "图名", "件数"],
            ["1", "H9003", "舾装件", "制作图A", 2],
            ["1", "H9003", "舾装件", "制作图B", 3],
        ]
    )

    with pytest.raises(ManifestParseError) as exc_info:
        parse_inbound_workbook(content)

    assert exc_info.value.diagnostics[0]["code"] == "DUPLICATE_SOURCE_ROW"


def test_non_xlsx_payload_is_rejected_before_openpyxl():
    with pytest.raises(ManifestParseError) as exc_info:
        parse_inbound_workbook(b"not-an-xlsx")

    assert "有效的 .xlsx 文件" in str(exc_info.value)
