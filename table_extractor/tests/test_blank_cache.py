from __future__ import annotations
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import patch, MagicMock
from PIL import Image
from table_extractor.html_extractor import extract_crop_as_html
from table_extractor.cache import CACHE_DIR, _cache_key
from table_extractor.retry import BlankResponseError


def _make_img():
    return Image.new("RGB", (20, 20), "green")


def test_blank_response_raises_and_retries_then_succeeds():
    with patch("table_extractor.html_extractor._get_client") as gc, \
         patch("table_extractor.html_extractor.load_full_prompt", return_value="P"):
        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message.content = ""
        resp1.usage = None

        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message.content = "<p>real</p>"
        resp2.usage = None

        gc.return_value.chat.completions.create.side_effect = [resp1, resp2]

        with patch("table_extractor.html_extractor.cached_call", side_effect=lambda **kw: kw["fn"]()):
            result = extract_crop_as_html(_make_img(), "m")
    assert result == "<p>real</p>"


def test_stale_blank_cache_deleted_and_overwritten():
    img = _make_img()
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    key = _cache_key(img_bytes, "html_extract", "m", "PROMPT")
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(["", {}], f)

    try:
        with patch("table_extractor.html_extractor.load_full_prompt", return_value="PROMPT"), \
             patch("table_extractor.html_extractor._get_client") as gc:
            
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "<p>fresh</p>"
            resp.usage = None
            gc.return_value.chat.completions.create.return_value = resp

            result = extract_crop_as_html(img, "m")

        assert result == "<p>fresh</p>"
        assert os.path.exists(cache_file)
        with open(cache_file, "r") as f:
            cached_data = json.load(f)
        assert cached_data[0] == "<p>fresh</p>"
    finally:
        if os.path.exists(cache_file):
            os.unlink(cache_file)
