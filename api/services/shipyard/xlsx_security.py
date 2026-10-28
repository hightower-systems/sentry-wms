"""Shared bounded XLSX archive validation for shipyard imports."""

from __future__ import annotations

from io import BytesIO
from zipfile import BadZipFile, ZipFile

MAX_ARCHIVE_ENTRIES = 2000
MAX_UNCOMPRESSED_ARCHIVE_BYTES = 50 * 1024 * 1024


class XlsxSecurityError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def validate_xlsx_archive(content: bytes) -> None:
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise XlsxSecurityError(
                    "Excel 文件包含过多内部文件，拒绝解析",
                    "XLSX_TOO_MANY_ENTRIES",
                )
            if (
                sum(entry.file_size for entry in entries)
                > MAX_UNCOMPRESSED_ARCHIVE_BYTES
            ):
                raise XlsxSecurityError(
                    "Excel 解压后体积过大，拒绝解析",
                    "XLSX_UNCOMPRESSED_TOO_LARGE",
                )
    except BadZipFile as exc:
        raise XlsxSecurityError(
            "无法读取 Excel 文件，请确认文件为有效的 .xlsx 文件",
            "XLSX_INVALID_ARCHIVE",
        ) from exc
