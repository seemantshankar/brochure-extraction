"""Tests for HTML extraction and prompt cleanup."""
import os
import sys
import time
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_extractor import (
    load_full_prompt,
    load_prompt,
    clean_up_html_fragment,
    run_extraction,
)


class TestLoadPrompt:
    """Tests for prompt loading helpers."""

    def test_load_master_returns_content(self):
        """The master prompt file loads and contains expected sections."""
        content = load_prompt("_master.txt")
        assert "Table Reconstitution" in content
        assert "Zero Commentary" in content

    def test_load_full_prompt_includes_master_and_all_hints(self):
        """The full prompt combines the master with all hint files."""
        prompt = load_full_prompt()
        assert "Zero Commentary" in prompt
        assert "Ruled Table" in prompt
        assert "Grouped Specification Table" in prompt
        assert "Bullet Panel" in prompt
        assert "Swatch Grid" in prompt
        assert "Stat Cards" in prompt
        assert "Technical Drawing" in prompt
        assert "Icon Badge" in prompt
        assert "Footnote Block" in prompt
        assert "Section Heading" in prompt
        assert "General / Other" in prompt


class TestCleanUpHtmlFragment:
    """Tests for cleaning raw LLM HTML output."""

    def test_strips_backtick_fences_multiline(self):
        """Markdown HTML fences are stripped."""
        raw = "```html\n<table><tr><td>X</td></tr></table>\n```"
        result = clean_up_html_fragment(raw)
        assert "<table>" in result
        assert "```" not in result

    def test_strips_plain_backtick_block_no_language(self):
        """Plain markdown fences are stripped."""
        raw = "```\n<p>Hello</p>\n```"
        result = clean_up_html_fragment(raw)
        assert result == "<p>Hello</p>"

    def test_preserves_plain_html_unchanged(self):
        """Plain HTML without fences is preserved."""
        raw = "<table><tr><td>A</td></tr></table>"
        assert clean_up_html_fragment(raw) == raw

    def test_strips_leading_trailing_whitespace(self):
        """Surrounding whitespace is trimmed."""
        raw = "  <p>Hi</p>  "
        assert clean_up_html_fragment(raw) == "<p>Hi</p>"

    def test_empty_string_returns_empty(self):
        """Empty input returns empty output."""
        assert clean_up_html_fragment("") == ""


class TestRunExtractionParallel:
    """Tests for the parallel HTML extraction pipeline."""

    def _make_session_manager(self, tmp_path, pages_config):
        """Return a minimal fake session manager for extraction tests."""
        class FakeSessionManager:
            """Fake session manager backed by tmp_path."""

            def load_meta(self, sid):
                """Return fixed session metadata."""
                return {"files": ["test.pdf"], "pages": pages_config}

            def get_page_dir(self, sid):
                """Return the temporary page directory."""
                return str(tmp_path)

            def get_session_dir(self, sid):
                """Return the temporary session directory."""
                return str(tmp_path)

        return FakeSessionManager()

    def _make_page_image(self, tmp_path, filename):
        """Create a minimal page image for extraction tests."""
        path = os.path.join(str(tmp_path), filename)
        img = Image.new("RGB", (100, 100), "blue")
        img.save(path)
        return path

    def _make_crop_image(self, crop_root, session_id, filename):
        """Create a minimal crop image for extraction tests."""
        crop_dir = os.path.join(crop_root, session_id)
        os.makedirs(crop_dir, exist_ok=True)
        path = os.path.join(crop_dir, filename)
        img = Image.new("RGB", (50, 50), "red")
        img.save(path)
        return path

    def _fake_extract(self, crop_image, model):
        """Return a simple HTML fragment for mocked extraction."""
        time.sleep(0.01)
        return "<p>fragment</p>"

    def test_preserves_page_ordering_in_output(self, tmp_path):
        """Page ordering is preserved in generated files."""
        self._make_page_image(tmp_path, "page_000.png")
        self._make_page_image(tmp_path, "page_001.png")

        sm = self._make_session_manager(tmp_path, [
            {"path": "page_000.png", "classification": "Simple", "crops": []},
            {"path": "page_001.png", "classification": "Simple", "crops": []},
        ])

        with patch("table_extractor.html_extractor.extract_crop_as_html", self._fake_extract):
            events = list(run_extraction(
                session_id="test",
                sm=sm,
                crop_root=str(tmp_path / "crops"),
                model="test/model",
                output_root=str(tmp_path / "extracted"),
            ))

        done_event = events[-1]
        assert done_event["status"] == "done"
        assert done_event["page_files"] == ["page-0.html", "page-1.html"]
        assert done_event["index"] == "index.html"

        out_dir = os.path.join(str(tmp_path / "extracted"), "test")
        with open(os.path.join(out_dir, "page-0.html"), encoding="utf-8") as f:
            page0_html = f.read()
        with open(os.path.join(out_dir, "page-1.html"), encoding="utf-8") as f:
            page1_html = f.read()
        assert 'id="page-0"' in page0_html
        assert 'id="page-1"' in page1_html
        assert 'id="page-1"' not in page0_html
        assert 'id="page-0"' not in page1_html

    def test_preserves_crop_y_sorting_within_page(self, tmp_path):
        """Crops are ordered top-to-bottom within a complex page."""
        crop_root = str(tmp_path / "crops")
        session_id = "test_y_sort"
        self._make_page_image(tmp_path, "page_000.png")
        self._make_crop_image(crop_root, session_id, "crop_bottom.png")
        self._make_crop_image(crop_root, session_id, "crop_top.png")
        self._make_crop_image(crop_root, session_id, "crop_mid.png")

        sm = self._make_session_manager(tmp_path, [
            {
                "path": "page_000.png",
                "classification": "Complex",
                "crops": [
                    {"filename": "crop_bottom.png", "bbox": [0, 0.8, 1, 1.0]},
                    {"filename": "crop_top.png", "bbox": [0, 0.0, 1, 0.2]},
                    {"filename": "crop_mid.png", "bbox": [0, 0.4, 1, 0.6]},
                ],
            },
        ])

        def fake_extract(crop_image, model):
            """Return a fragment labeled by crop filename."""
            return f"<p>{crop_image.filename}</p>"

        with patch("table_extractor.html_extractor.extract_crop_as_html", fake_extract):
            events = list(run_extraction(
                session_id=session_id,
                sm=sm,
                crop_root=crop_root,
                model="test/model",
                output_root=str(tmp_path / "extracted"),
            ))

        done_event = events[-1]
        assert done_event["status"] == "done"
        out_dir = os.path.join(str(tmp_path / "extracted"), session_id)
        with open(os.path.join(out_dir, "page-0.html"), encoding="utf-8") as f:
            page_html = f.read()
        assert page_html.index("crop_top.png") < page_html.index("crop_mid.png") < page_html.index("crop_bottom.png")

    def test_yields_progress_events_for_each_task(self, tmp_path):
        """Progress events are yielded for each extraction task."""
        self._make_page_image(tmp_path, "page_000.png")
        self._make_page_image(tmp_path, "page_001.png")

        sm = self._make_session_manager(tmp_path, [
            {"path": "page_000.png", "classification": "Simple", "crops": []},
            {"path": "page_001.png", "classification": "Simple", "crops": []},
        ])

        with patch("table_extractor.html_extractor.extract_crop_as_html", self._fake_extract):
            events = list(run_extraction(
                session_id="test",
                sm=sm,
                crop_root=str(tmp_path / "crops"),
                model="test/model",
            ))

        progress_events = [e for e in events if e["status"] == "progress"]
        assert len(progress_events) >= 3
        assert progress_events[-2]["page"] == 2
        assert progress_events[-2]["totalPages"] == 2
        assert len([e for e in events if e["status"] == "done"]) == 1

    def test_error_in_one_task_does_not_block_others(self, tmp_path):
        """A failing crop does not prevent other crops from being included."""
        crop_root = str(tmp_path / "crops")
        session_id = "test_error"
        self._make_page_image(tmp_path, "page_000.png")
        self._make_crop_image(crop_root, session_id, "crop_good.png")
        self._make_crop_image(crop_root, session_id, "crop_bad.png")
        self._make_crop_image(crop_root, session_id, "crop_also_good.png")

        def fake_extract_with_error(crop_image, model):
            """Raise for crop_bad.png, otherwise return a fragment."""
            if crop_image.filename.endswith("crop_bad.png"):
                raise RuntimeError("Simulated failure")
            return "<p>fragment</p>"

        sm = self._make_session_manager(tmp_path, [
            {
                "path": "page_000.png",
                "classification": "Complex",
                "crops": [
                    {"filename": "crop_good.png", "bbox": [0, 0.0, 1, 0.2]},
                    {"filename": "crop_bad.png", "bbox": [0, 0.3, 1, 0.5]},
                    {"filename": "crop_also_good.png", "bbox": [0, 0.6, 1, 0.8]},
                ],
            },
        ])

        with patch("table_extractor.html_extractor.extract_crop_as_html", fake_extract_with_error):
            events = list(run_extraction(
                session_id=session_id,
                sm=sm,
                crop_root=crop_root,
                model="test/model",
                output_root=str(tmp_path / "extracted"),
            ))

        done_event = events[-1]
        assert done_event["status"] == "done"
        out_dir = os.path.join(str(tmp_path / "extracted"), session_id)
        with open(os.path.join(out_dir, "page-0.html"), encoding="utf-8") as f:
            page_html = f.read()
        assert "error-region" in page_html
        assert "Simulated failure" in page_html
        assert "<p>fragment</p>" in page_html

    def test_run_extraction_writes_page_files(self, tmp_path):
        """Extraction writes page files and an index to the output root."""
        self._make_page_image(tmp_path, "page_000.png")

        sm = self._make_session_manager(tmp_path, [
            {"path": "page_000.png", "classification": "Simple", "crops": []},
        ])

        with patch("table_extractor.html_extractor.extract_crop_as_html", self._fake_extract):
            events = list(run_extraction(
                session_id="test",
                sm=sm,
                crop_root=str(tmp_path / "crops"),
                model="test/model",
                output_root=str(tmp_path / "extracted"),
            ))

        done = events[-1]
        assert done["status"] == "done"
        assert done["page_files"] == ["page-0.html"]
        assert done["index"] == "index.html"

        out_dir = os.path.join(str(tmp_path / "extracted"), "test")
        assert os.path.exists(os.path.join(out_dir, "page-0.html"))
        assert os.path.exists(os.path.join(out_dir, "index.html"))
