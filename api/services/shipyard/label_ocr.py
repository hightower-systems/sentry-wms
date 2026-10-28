"""Public-cloud OCR and deterministic parsing for shipyard pallet labels."""

from __future__ import annotations

import base64
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from services.shipyard.errors import OcrProviderError, OcrUnavailableError

BAIDU_OAUTH_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

_FIELD_ALIASES = {
    "船号": "ship_no",
    "工程号": "engineering_no",
    "制作图号": "production_drawing_no",
    "制作图编号": "production_drawing_no",
    "图号": "production_drawing_no",
    "物料编码": "material_code",
    "物资编码": "material_code",
    "品名": "item_name",
    "物料名称": "item_name",
    "规格": "item_spec",
    "规格型号": "item_spec",
    "型号": "item_spec",
    "批次号": "batch_no",
    "批次": "batch_no",
    "托盘后缀": "tag_suffix",
    "吊牌后缀": "tag_suffix",
    "安装托盘号": "installation_pallet_no",
    "安装托盘名称": "installation_pallet_name",
    "二维码托盘号": "factory_pallet_no",
    "托盘号": "factory_pallet_no",
    "厂家": "manufacturer_name",
    "厂家名称": "manufacturer_name",
    "目的地": "destination",
    "数量": "label_quantity",
    "件数": "label_quantity",
    "ship_no": "ship_no",
    "engineering_no": "engineering_no",
    "production_drawing_no": "production_drawing_no",
    "drawing_no": "production_drawing_no",
    "material_code": "material_code",
    "item_name": "item_name",
    "material_name": "item_name",
    "item_spec": "item_spec",
    "batch_no": "batch_no",
    "tag_suffix": "tag_suffix",
    "suffix": "tag_suffix",
    "manufacturer_name": "manufacturer_name",
    "destination": "destination",
    "pallet_no": "factory_pallet_no",
    "pallet": "factory_pallet_no",
    "quantity": "label_quantity",
    "qty": "label_quantity",
}

_ALIASES_BY_LENGTH = sorted(_FIELD_ALIASES, key=len, reverse=True)
_SUFFIX_PATTERN = re.compile(r"(?<!\d)([1-9]\d*-[1-9]\d*)(?!\d)")
_SUFFIX_DASHES = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "－": "-",
    }
)
_COLON_VALUES = re.compile(
    rf"^({'|'.join(re.escape(item) for item in _ALIASES_BY_LENGTH)})"
    r"\s*[:=：]\s*(.+)$",
    re.IGNORECASE,
)
_SPACE_VALUES = re.compile(
    rf"^({'|'.join(re.escape(item) for item in _ALIASES_BY_LENGTH)})"
    r"\s+(.+)$",
    re.IGNORECASE,
)


def _timeout(name: str, default: float) -> float:
    try:
        return max(float(os.getenv(name, default)), 1.0)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LabelOcrResult:
    provider: str
    raw_text: str
    raw_lines: list[str]
    fields: dict[str, Any]
    confidence: float | None


def _normalized_key(value: str) -> str:
    compact = re.sub(r"\s+", "", value or "").casefold()
    return _FIELD_ALIASES.get(compact, "")


def _clean_value(field: str, value: str) -> Any:
    text = value.strip().strip("：:=|;,，；")
    if field == "material_code":
        return re.sub(r"\s+", "", text)
    if field == "tag_suffix":
        match = _SUFFIX_PATTERN.search(text.translate(_SUFFIX_DASHES))
        return match.group(1) if match else ""
    if field == "label_quantity":
        number = text.replace(",", "").replace("件", "").strip()
        return number or ""
    return text


def parse_label_text(raw_text: str) -> dict[str, Any]:
    """Parse key/value label lines without guessing unrelated free text."""

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in (raw_text or "").splitlines()
        if line.strip()
    ]
    fields: dict[str, Any] = {}
    for index, line in enumerate(lines):
        for segment in re.split(r"[|；;]", line):
            match = _COLON_VALUES.match(segment.strip()) or _SPACE_VALUES.match(
                segment.strip()
            )
            if match:
                field = _normalized_key(match.group(1))
                value = _clean_value(field, match.group(2))
                if field and value not in ("", None):
                    fields[field] = value
                continue
            exact_field = _normalized_key(segment.strip())
            if exact_field and index + 1 < len(lines):
                value = _clean_value(exact_field, lines[index + 1])
                if value not in ("", None):
                    fields[exact_field] = value

    suffix = _SUFFIX_PATTERN.search((raw_text or "").translate(_SUFFIX_DASHES))
    if suffix and not fields.get("tag_suffix"):
        fields["tag_suffix"] = suffix.group(1)
    return fields


def _provider() -> str:
    return (os.getenv("SHIPYARD_OCR_PROVIDER") or "baidu").strip().casefold()


def _baidu_access_token(*, force_refresh: bool = False) -> str:
    api_key = (os.getenv("BAIDU_OCR_API_KEY") or "").strip()
    secret_key = (os.getenv("BAIDU_OCR_SECRET_KEY") or "").strip()
    if not api_key or not secret_key:
        raise OcrUnavailableError(
            "吊牌 OCR 尚未配置",
            {"required_env": ["BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"]},
        )

    cache_key = f"{api_key}:{secret_key}"
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if not force_refresh and cached and cached[1] > now:
            return cached[0]
        try:
            # _timeout() always returns a finite positive value.
            response = requests.post(  # nosec B113
                BAIDU_OAUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": api_key,
                    "client_secret": secret_key,
                },
                timeout=_timeout("SHIPYARD_OCR_TOKEN_TIMEOUT_S", 10),
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OcrProviderError("OCR 鉴权服务暂时不可用") from exc
        if not isinstance(payload, dict):
            raise OcrProviderError("OCR 鉴权服务返回格式异常")
        token = payload.get("access_token")
        if response.status_code != 200 or not token:
            raise OcrProviderError(
                "OCR 鉴权失败",
                {"provider_error": payload.get("error") or "authentication_failed"},
            )
        expires_at = now + max(int(payload.get("expires_in", 2592000)) - 60, 60)
        _TOKEN_CACHE[cache_key] = (token, expires_at)
        return token


def _baidu_ocr_request(content: bytes, token: str) -> tuple[int, dict[str, Any]]:
    try:
        # _timeout() always returns a finite positive value.
        response = requests.post(  # nosec B113
            BAIDU_OCR_URL,
            params={"access_token": token},
            data={
                "image": base64.b64encode(content).decode("ascii"),
                "language_type": "CHN_ENG",
                "detect_direction": "true",
                "probability": "true",
            },
            timeout=_timeout("SHIPYARD_OCR_TIMEOUT_S", 20),
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise OcrProviderError("吊牌 OCR 服务暂时不可用") from exc
    if not isinstance(payload, dict):
        raise OcrProviderError("吊牌 OCR 服务返回格式异常")
    return response.status_code, payload


def _baidu_recognize(content: bytes) -> tuple[str, list[str], float | None]:
    token = _baidu_access_token()
    status_code, payload = _baidu_ocr_request(content, token)
    if str(payload.get("error_code") or "") in {"110", "111"}:
        token = _baidu_access_token(force_refresh=True)
        status_code, payload = _baidu_ocr_request(content, token)

    if status_code != 200 or payload.get("error_code"):
        raise OcrProviderError(
            "吊牌 OCR 识别失败",
            {"provider_error": payload.get("error_code") or status_code},
        )

    words = payload.get("words_result") or []
    lines = [
        str(item.get("words") or "").strip()
        for item in words
        if str(item.get("words") or "").strip()
    ]
    probabilities = [
        float(item["probability"]["average"])
        for item in words
        if isinstance(item.get("probability"), dict)
        and item["probability"].get("average") is not None
    ]
    confidence = (
        round(sum(probabilities) / len(probabilities), 4) if probabilities else None
    )
    return "\n".join(lines), lines, confidence


def recognize_label_image(content: bytes) -> LabelOcrResult:
    provider = _provider()
    if provider != "baidu":
        raise OcrUnavailableError(
            "不支持的吊牌 OCR 服务",
            {"provider": provider},
        )
    raw_text, raw_lines, confidence = _baidu_recognize(content)
    return LabelOcrResult(
        provider="baidu",
        raw_text=raw_text,
        raw_lines=raw_lines,
        fields=parse_label_text(raw_text),
        confidence=confidence,
    )
