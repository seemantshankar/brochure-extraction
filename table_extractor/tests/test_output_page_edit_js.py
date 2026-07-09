import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_edit_js_injects_editable_regions():
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "output_page_edit.js"
    )
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    assert "contenteditable" in js or "input" in js
    assert "MutationObserver" in js
    assert "Save Changes" in js
    assert "page-nav" in js
