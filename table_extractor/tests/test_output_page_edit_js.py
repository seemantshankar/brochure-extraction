"""Tests for the inline editing JavaScript template."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_edit_js_injects_editable_regions():
    """The edit JS enables inline editing and saving."""
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "output_page_edit.js"
    )
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    assert "contenteditable" in js or "input" in js
    assert "MutationObserver" in js
    assert "Save Changes" in js
    assert "page-nav" in js
    assert "/save-page/" in js


def test_edit_script_detects_embedded_mode_without_changing_save_path():
    """The edit JS detects ?embed=1 and adds embedded-review class."""
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "output_page_edit.js"
    )
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    assert 'searchParams.get("embed") === "1"' in js
    assert 'classList.add("embedded-review")' in js
    assert '"/save-page/"' in js
