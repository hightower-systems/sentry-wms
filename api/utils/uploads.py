"""Bounded upload helpers shared by shipyard file and photo endpoints."""

from __future__ import annotations

from werkzeug.datastructures import FileStorage

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class UploadTooLargeError(ValueError):
    """The uploaded payload exceeds the endpoint's byte limit."""


class UnsupportedImageError(ValueError):
    """The uploaded bytes are not a supported raster image."""


def safe_upload_name(raw_name: str, *, fallback: str) -> str:
    name = (raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(char for char in name if char.isprintable()).strip()
    return (name or fallback)[:255]


def read_upload_bytes(
    uploaded: FileStorage,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> bytes:
    content = uploaded.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise UploadTooLargeError("上传文件超过大小限制")
    return content


def detect_image_content_type(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    raise UnsupportedImageError("仅支持 JPEG、PNG 或 WebP 照片")
