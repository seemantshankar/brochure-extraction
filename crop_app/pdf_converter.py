import os
from pdf2image import convert_from_path
from PyPDF2 import PdfReader


def pdf_to_pages(
    pdf_path: str, out_dir: str, dpi: int = 150, start_index: int = 0
) -> list[str]:
    """Convert every page of a PDF to a PNG saved in out_dir.

    Defaults to 150 DPI for LLM-analysis-friendly resolution.
    start_index offsets the page_NNN.png sequence so multiple PDFs in one
    session produce unique filenames.
    Returns a list of absolute paths to the generated PNG files.
    """
    images = convert_from_path(pdf_path, dpi=dpi)
    paths = []
    for i, img in enumerate(images):
        filename = f"page_{start_index + i:03d}.png"
        filepath = os.path.join(out_dir, filename)
        img.save(filepath, "PNG")
        paths.append(os.path.abspath(filepath))
    return paths


def upgrade_page_to_hires(
    pdf_path: str, page_path: str, page_index: int, dpi: int = 300
) -> str:
    """Re-render a single PDF page at higher DPI, replacing the existing file.

    Args:
        pdf_path:      Absolute path to the original PDF.
        page_path:     Absolute path to the page PNG to overwrite.
        page_index:    0-based page index within the PDF.
        dpi:           Target DPI for the high-resolution render.

    Returns the absolute path to the replaced file.
    """
    images = convert_from_path(
        pdf_path,
        dpi=dpi,
        first_page=page_index + 1,
        last_page=page_index + 1,
    )
    images[0].save(page_path, "PNG")
    return os.path.abspath(page_path)


def get_page_count(pdf_path: str) -> int:
    """Return the number of pages in a PDF."""
    reader = PdfReader(pdf_path)
    return len(reader.pages)
