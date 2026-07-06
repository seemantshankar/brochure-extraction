import os
import pytest
import tempfile
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rc
from PIL import Image
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pdf_converter import pdf_to_pages, upgrade_page_to_hires, get_page_count


@pytest.fixture
def sample_pdf(tmp_path):
    pdf_path = tmp_path / "test.pdf"
    c = rc.Canvas(str(pdf_path), pagesize=letter)
    c.drawString(100, 700, "Page 1")
    c.showPage()
    c.drawString(100, 700, "Page 2")
    c.showPage()
    c.drawString(100, 700, "Page 3")
    c.save()
    return str(pdf_path)


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "pages"
    d.mkdir()
    return str(d)


def test_pdf_to_pages_creates_correct_count(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir)
    assert len(pages) == 3


def test_pdf_to_pages_creates_png_files(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir)
    for p in pages:
        assert p.endswith(".png")
        assert os.path.exists(p)


def test_page_count(sample_pdf):
    assert get_page_count(sample_pdf) == 3


def test_page_naming_convention(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir)
    assert all("page_" in os.path.basename(p) for p in pages)


def test_default_dpi_is_150(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir)
    img = Image.open(pages[0])
    w, h = img.size
    expected_w = int(8.5 * 150)
    expected_h = int(11 * 150)
    assert abs(w - expected_w) <= 1
    assert abs(h - expected_h) <= 1


def test_explicit_300_dpi(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir, dpi=300)
    img = Image.open(pages[0])
    w, h = img.size
    expected_w = int(8.5 * 300)
    expected_h = int(11 * 300)
    assert abs(w - expected_w) <= 1
    assert abs(h - expected_h) <= 1


def test_start_index_offsets_page_names(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir, start_index=5)
    basenames = [os.path.basename(p) for p in pages]
    assert basenames == ["page_005.png", "page_006.png", "page_007.png"]


def test_upgrade_page_to_hires_replaces_with_larger_image(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir, dpi=150)
    page_path = pages[0]
    low_img = Image.open(page_path)
    low_w, low_h = low_img.size
    low_img.close()

    upgrade_page_to_hires(sample_pdf, page_path, page_index=0, dpi=300)

    hi_img = Image.open(page_path)
    hi_w, hi_h = hi_img.size
    hi_img.close()

    assert hi_w == low_w * 2
    assert hi_h == low_h * 2


def test_upgrade_page_to_hires_correct_page(sample_pdf, out_dir):
    pages = pdf_to_pages(sample_pdf, out_dir, dpi=150)
    page_path = pages[1]

    upgrade_page_to_hires(sample_pdf, page_path, page_index=1, dpi=300)

    hi_img = Image.open(page_path)
    hi_w, hi_h = hi_img.size
    hi_img.close()

    expected_w = int(8.5 * 300)
    expected_h = int(11 * 300)
    assert abs(hi_w - expected_w) <= 1
    assert abs(hi_h - expected_h) <= 1
