# crop_app/tests/test_llm.py
import os
import pytest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from llm import analyze_page, _parse_response


def test_parse_response_simple():
    raw = '{"classification": "Simple"}'
    result = _parse_response(raw)
    assert result["classification"] == "Simple"
    assert result["error"] is None


def test_parse_response_complex():
    raw = '{"classification": "Complex"}'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_parse_response_with_markdown_fences():
    raw = '```json\n{"classification": "Complex"}\n```'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_parse_response_invalid_json_defaults_complex():
    raw = "not json at all"
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is not None


def test_parse_response_invalid_classification_defaults_complex():
    raw = '{"classification": "Unknown"}'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is not None


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


def test_analyze_page_api_error(tmp_path):
    from PIL import Image
    img_path = str(tmp_path / "page.png")
    Image.new("RGB", (100, 100), "white").save(img_path)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("API down")

    with patch("llm._get_client", return_value=mock_client):
        result = analyze_page(img_path)

    assert result["classification"] == "Complex"
    assert result["error"] is not None
