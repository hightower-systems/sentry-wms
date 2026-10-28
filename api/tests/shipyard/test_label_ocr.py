"""Offline tests for public-cloud OCR parsing and provider errors."""

import pytest

from services.shipyard import label_ocr
from services.shipyard.errors import OcrUnavailableError


def test_parse_label_text_supports_colons_spaces_next_line_and_suffix():
    fields = label_ocr.parse_label_text(
        """
船号：H1613
工程号 H1613
制作图号
D-001
物料编码： MAT 001
品名：船用支架
规格型号：S-100
厂家名称：测试船舶配件有限公司
安装托盘号：P-01
数量：4件
1－2
"""
    )

    assert fields == {
        "ship_no": "H1613",
        "engineering_no": "H1613",
        "production_drawing_no": "D-001",
        "material_code": "MAT001",
        "item_name": "船用支架",
        "item_spec": "S-100",
        "manufacturer_name": "测试船舶配件有限公司",
        "installation_pallet_no": "P-01",
        "label_quantity": "4",
        "tag_suffix": "1-2",
    }


def test_ocr_without_credentials_is_reported_as_unavailable(monkeypatch):
    monkeypatch.delenv("BAIDU_OCR_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_OCR_SECRET_KEY", raising=False)

    with pytest.raises(OcrUnavailableError) as raised:
        label_ocr.recognize_label_image(b"image")

    assert raised.value.code == "ocr_unavailable"
    assert raised.value.details["required_env"] == [
        "BAIDU_OCR_API_KEY",
        "BAIDU_OCR_SECRET_KEY",
    ]


def test_baidu_provider_contract_is_normalized_without_network(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == label_ocr.BAIDU_OAUTH_URL:
            return Response(
                200,
                {"access_token": "test-token", "expires_in": 3600},
            )
        return Response(
            200,
            {
                "words_result": [
                    {
                        "words": "物料编码：MAT-001",
                        "probability": {"average": 0.97},
                    },
                    {
                        "words": "品名：船用支架",
                        "probability": {"average": 0.99},
                    },
                    {"words": "托盘后缀：1-2"},
                ]
            },
        )

    monkeypatch.setenv("BAIDU_OCR_API_KEY", "test-key")
    monkeypatch.setenv("BAIDU_OCR_SECRET_KEY", "test-secret")
    monkeypatch.setattr(label_ocr.requests, "post", fake_post)
    label_ocr._TOKEN_CACHE.clear()

    result = label_ocr.recognize_label_image(b"image")

    assert result.provider == "baidu"
    assert result.fields == {
        "material_code": "MAT-001",
        "item_name": "船用支架",
        "tag_suffix": "1-2",
    }
    assert result.confidence == 0.98
    assert len(calls) == 2
    assert calls[1][1]["params"]["access_token"] == "test-token"
    assert calls[1][1]["data"]["image"] == "aW1hZ2U="
