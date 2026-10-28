import pytest
from shipyard.helpers import distribution_demand_bytes, workbook_bytes

from services.shipyard.distribution_parser import (
    DistributionParseError,
    parse_distribution_workbook,
)


def test_distribution_workbook_maps_known_and_unnamed_columns():
    parsed = parse_distribution_workbook(
        distribution_demand_bytes(),
        source_file_name="distribution.xlsx",
    )

    assert parsed.sheet_name == "外高桥"
    assert parsed.header_row == 1
    assert parsed.metadata["line_count"] == 3
    assert parsed.metadata["engineering_count"] == 2
    assert parsed.metadata["pallet_count"] == 3
    assert parsed.metadata["field_mapping"]["delivery_status"] == "未命名列F"
    assert parsed.warnings[0]["code"] == "UNNAMED_HEADER_MAPPED"
    assert parsed.lines[0].engineering_no == "H1632"
    assert parsed.lines[0].delivery_status == "未到"
    assert parsed.lines[0].demand_start_at.isoformat() == ("2026-10-26T10:30:00+08:00")
    assert parsed.lines[0].completed_at.isoformat() == ("2026-10-24T14:10:40+08:00")
    assert parsed.lines[0].raw_data["未命名列F"] == "未到"


def test_distribution_workbook_rejects_duplicate_pallet():
    content = workbook_bytes(
        [
            ["工程号", "分段号", "托盘号", "托盘名称", "需求时间"],
            [
                "H1",
                "101",
                "P1",
                "托盘A",
                "2026-10-26 10:00:00~2026-10-26 11:00:00",
            ],
            [
                "H1",
                "101",
                "P1",
                "托盘B",
                "2026-10-26 11:00:00~2026-10-26 12:00:00",
            ],
        ]
    )

    with pytest.raises(DistributionParseError) as exc_info:
        parse_distribution_workbook(content)

    assert exc_info.value.diagnostics[0]["code"] == "DUPLICATE_DEMAND_PALLET"


def test_distribution_workbook_rejects_invalid_demand_window():
    content = workbook_bytes(
        [
            ["工程号", "分段号", "托盘号", "托盘名称", "需求时间"],
            ["H1", "101", "P1", "托盘A", "十点以后"],
        ]
    )

    with pytest.raises(DistributionParseError) as exc_info:
        parse_distribution_workbook(content)

    assert exc_info.value.diagnostics[0]["code"] == "INVALID_DEMAND_WINDOW"
