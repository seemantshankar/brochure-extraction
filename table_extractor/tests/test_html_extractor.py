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
