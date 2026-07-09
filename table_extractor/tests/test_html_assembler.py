import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_assembler import (
    resolve_footnotes,
    build_page_html,
    build_toc,
    assemble_full_document,
    write_page_files,
)


def test_write_page_files_creates_per_page_html():
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = os.path.join(tmp, "extracted", "test-sid")
        pages_data = [
            {"html": "<p>Page 0 content</p>"},
            {"html": "<p>Page 1 content</p>"},
        ]
        write_page_files("test-sid", pages_data, "Test Doc", output_root=os.path.join(tmp, "extracted"))

        assert os.path.exists(os.path.join(session_dir, "page-0.html"))
        assert os.path.exists(os.path.join(session_dir, "page-1.html"))
        assert os.path.exists(os.path.join(session_dir, "index.html"))

        with open(os.path.join(session_dir, "page-0.html"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "Page 0 content" in content
        assert "Test Doc" in content


class TestResolveFootnotes:
    def test_links_markers_in_body_text(self):
        page = (
            '<p>Mileage<sup>*</sup> is 20kmpl.</p>'
            '<aside class="footnotes">'
            "<p><sup>*</sup> As per ARAI certification testing.</p>"
            "</aside>"
        )
        result = resolve_footnotes(page)
        assert 'class="footnote-ref"' in result
        assert 'data-footnote=' in result
        assert 'id="fn-0"' in result
        assert 'class="footnote-back"' in result

    def test_returns_unchanged_when_no_footnote_block(self):
        raw = '<p>No footnotes here<sup>*</sup>.</p>'
        assert resolve_footnotes(raw) == raw

    def test_handles_multiple_markers(self):
        page = (
            '<p>Value A<sup>*</sup>, Value B<sup>**</sup>.</p>'
            '<div class="footnotes">'
            "<p><sup>*</sup> Footnote one.</p>"
            "<p><sup>**</sup> Footnote two.</p>"
            "</div>"
        )
        result = resolve_footnotes(page)
        assert 'id="fn-0"' in result
        assert 'id="fn-1"' in result


class TestBuildPageHtml:
    def test_wraps_content_with_page_id_and_label(self):
        html = build_page_html(0, 5, "<h2>Hello</h2>")
        assert 'id="page-0"' in html
        assert "1 of 5" in html
        assert "<h2>Hello</h2>" in html

    def test_second_page_label(self):
        html = build_page_html(1, 3, "<p>Page 2 content</p>")
        assert 'id="page-1"' in html
        assert "2 of 3" in html


class TestBuildToc:
    def test_creates_one_link_per_page(self):
        toc = build_toc(3)
        assert toc.count("page-") == 3  # one href per page
        assert toc.count("Page ") == 3  # one link text per page
        assert 'href="#page-0"' in toc
        assert 'href="#page-2"' in toc


class TestAssembleFullDocument:
    def test_produces_valid_html_document(self):
        pages = [
            {"html": "<h1>Title</h1>"},
            {"html": "<table><tr><td>X</td></tr></table>"},
        ]
        result = assemble_full_document(pages, "Test Brochure")
        assert "<!DOCTYPE html>" in result
        assert "Test Brochure" in result
        assert 'class="toc-sidebar"' in result
        assert 'id="page-0"' in result
        assert 'id="page-1"' in result

    def test_page_divider_between_pages(self):
        pages = [{"html": "<p>A</p>"}, {"html": "<p>B</p>"}]
        result = assemble_full_document(pages, "Title")
        assert 'class="page-divider"' in result

    def test_single_page_no_divider(self):
        pages = [{"html": "<p>Only one page</p>"}]
        result = assemble_full_document(pages, "Title")
        assert 'class="page-divider"' not in result

    def test_applies_footnote_resolution(self):
        fn_body = (
            '<p>Value<sup>*</sup></p>'
            '<aside class="footnotes"><p><sup>*</sup> Note.</p></aside>'
        )
        pages = [{"html": fn_body}]
        result = assemble_full_document(pages, "Title")
        assert 'class="footnote-ref"' in result


def test_index_page_has_page_grid():
    from table_extractor.html_assembler import write_page_files
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        write_page_files("idx-test", [{"html": "<p>A</p>"}, {"html": "<p>B</p>"}], "Idx", output_root=tmp)
        with open(os.path.join(tmp, "idx-test", "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        assert "page-grid" in html
        assert "page-0.html" in html
        assert "page-1.html" in html
