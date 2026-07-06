# crop_app/tests/test_llm.py
import os
import pytest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from llm import analyze_page, _parse_response


def test_parse_response_complex_true():
    raw = '{"complex": true, "labels": ["table", "swatch_grid"]}'
    result = _parse_response(raw)
    assert result["complex"] is True
    assert "table" in result["labels"]


def test_parse_response_complex_false():
    raw = '{"complex": false, "labels": []}'
    result = _parse_response(raw)
    assert result["complex"] is False
    assert result["labels"] == []


def test_parse_response_with_markdown_fences():
    raw = '```json\n{"complex": true, "labels": ["image_grid"]}\n```'
    result = _parse_response(raw)
    assert result["complex"] is True
    assert "image_grid" in result["labels"]


def test_parse_response_invalid_json():
    raw = 'not json at all'
    result = _parse_response(raw)
    assert result["complex"] is False
    assert result["labels"] == []
    assert result["error"] is not None


def test_analyze_page_success(tmp_path):
    # Create a dummy image
    from PIL import Image
    img_path = str(tmp_path / "page.png")
    Image.new("RGB", (100, 100), "white").save(img_path)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"complex": true, "labels": ["table"]}'

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("llm._get_client", return_value=mock_client):
        result = analyze_page(img_path)

    assert result["complex"] is True
    assert "table" in result["labels"]


def test_analyze_page_api_error(tmp_path):
    from PIL import Image
    img_path = str(tmp_path / "page.png")
    Image.new("RGB", (100, 100), "white").save(img_path)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("API down")

    with patch("llm._get_client", return_value=mock_client):
        result = analyze_page(img_path)

    assert result["complex"] is False
    assert result["error"] is not None
