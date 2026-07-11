# crop_app/tests/test_llm.py
from __future__ import annotations
import os
import pytest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from llm import analyze_page, _parse_response_strict
from table_extractor.retry import MalformedOutputError


def test_parse_response_simple():
    raw = '{"classification": "Simple"}'
    result = _parse_response_strict(raw)
    assert result["classification"] == "Simple"
    assert result["error"] is None


def test_parse_response_complex():
    raw = '{"classification": "Complex"}'
    result = _parse_response_strict(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_parse_response_with_markdown_fences():
    raw = '```json\n{"classification": "Complex"}\n```'
    result = _parse_response_strict(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_parse_response_strict_raises_on_bad_json():
    with pytest.raises(MalformedOutputError):
        _parse_response_strict("not json at all <<<")


def test_parse_response_strict_raises_on_invalid_classification():
    with pytest.raises(MalformedOutputError):
        _parse_response_strict('{"classification": "Maybe"}')


def test_analyze_page_success(tmp_path):
    from PIL import Image
    img_path = str(tmp_path / "page.png")
    Image.new("RGB", (100, 100), "white").save(img_path)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"classification": "Complex"}'

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("llm._get_client", return_value=mock_client):
        result = analyze_page(img_path)

    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_analyze_page_returns_none_classification_on_api_failure(tmp_path):
    img = os.path.join(str(tmp_path), "p_fail.png")
    from PIL import Image
    Image.new("RGB", (10, 10), "red").save(img)

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    with patch("llm._get_client") as get_client:
        get_client.return_value.chat.completions.create.side_effect = boom
        result = analyze_page(img)
    assert result["classification"] is None
    assert "network down" in result["error"]


def test_parse_response_strict_accepts_valid(tmp_path):
    img = os.path.join(str(tmp_path), "p_valid.png")
    from PIL import Image
    Image.new("RGB", (10, 10), "blue").save(img)
    with patch("llm._get_client") as get_client:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"classification": "Simple"}'
        get_client.return_value.chat.completions.create.return_value = resp
        result = analyze_page(img)
    assert result["classification"] == "Simple"


def test_analyze_page_uses_cache(tmp_path):
    img = os.path.join(str(tmp_path), "cache_test.png")
    from PIL import Image
    Image.new("RGB", (10, 10), "green").save(img)

    with patch("llm._get_client") as get_client:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"classification": "Simple"}'
        get_client.return_value.chat.completions.create.return_value = resp

        result1 = analyze_page(img)
        assert result1["classification"] == "Simple"

        get_client.return_value.chat.completions.create.side_effect = Exception("should not be called")
        result2 = analyze_page(img)
        assert result2["classification"] == "Simple"
        assert result2["error"] is None


