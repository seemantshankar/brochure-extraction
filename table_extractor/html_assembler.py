"""Assemble extracted HTML fragments into full documents and per-page files."""
import os
import re
import html

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _load_template(name: str) -> str:
    """Load a template file from the templates directory."""
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def resolve_footnotes(page_html: str) -> str:
    """Resolve footnote markers to interactive linked references.

    Searches for a footnote block element (<aside|div|ol class="footnotes">).
    If found, each <sup>marker</sup> found in body content BEFORE the footnote block
    is wrapped as an <a> footnote-ref, and each footnote entry is wrapped with
    an id and a back-link.

    Footnote resolution operates per-page. Cross-page footnotes are out of scope.
    """
    match = re.search(
        r'<(aside|div|ol)([^>]*\bclass\s*=\s*"[^"]*\bfootnotes\b[^"]*"[^>]*)>(.*?)</\1>',
        page_html,
        re.DOTALL,
    )
    if not match:
        return page_html

    tag_name = match.group(1)
    tag_attrs = match.group(2)
    block_content = match.group(3)

    pre_html = page_html[:match.start()]
    post_html = page_html[match.end():]

    entries = re.findall(r'<p[^>]*>(.*?)</p>', block_content, re.DOTALL)
    if not entries:
        return page_html

    new_entries = []
    for idx, entry in enumerate(entries):
        marker_match = re.search(r'<sup>([^<]+)</sup>\s*(.*)', entry, re.DOTALL)
        if marker_match:
            marker = marker_match.group(1)
            fn_text = marker_match.group(2)
            fn_id = f"fn-{idx}"
            src_id = f"src-fn-{idx}"

            back_link = f' <a href="#{src_id}" class="footnote-back">\u21a9</a>'
            ref = (
                f'<a href="#{fn_id}" id="{src_id}" class="footnote-ref" '
                f'data-footnote="{html.escape(fn_text.strip(), quote=True)}">'
                f'<sup>{html.escape(marker)}</sup></a>'
            )

            escaped_marker = re.escape(marker)
            pattern = re.compile(r"<sup>\s*" + escaped_marker + r"\s*</sup>")
            pre_html, n_subs = pattern.subn(ref, pre_html, count=1)

            new_entries.append(f'<p id="{fn_id}"><sup>{marker}</sup> {fn_text}{back_link}</p>')
        else:
            new_entries.append(f"<p>{entry}</p>")

    new_block = f"<{tag_name}{tag_attrs}>\n  " + "\n  ".join(new_entries) + f"\n</{tag_name}>"

    return pre_html + new_block + post_html


def build_page_html(page_index: int, total_pages: int, content_html: str) -> str:
    """Wrap page content in a labeled page div."""
    label = f"Page {page_index + 1} of {total_pages}"
    return (
        '<div class="page" id="page-{idx}">\n'
        '  <div class="page-label">{label}</div>\n'
        "  {content}\n"
        "</div>\n"
    ).format(idx=page_index, label=label, content=content_html)


def build_toc(total_pages: int) -> str:
    """Build a table-of-contents sidebar linking to each page."""
    lines = []
    for i in range(total_pages):
        lines.append(f'    <a href="#page-{i}">Page {i + 1}</a>')
    return "\n".join(lines)


def assemble_full_document(pages_data: list, title: str = "Brochure Extraction") -> str:
    """Assemble all page data into a complete HTML document string.

    Args:
        pages_data: list of dicts, each with {"html": str}
        title: document title

    Returns:
        Complete HTML document as a string.
    """
    total = len(pages_data)
    toc = build_toc(total)

    page_blocks = []
    for i, pdata in enumerate(pages_data):
        content = pdata.get("html", "")
        content = resolve_footnotes(content)
        page_blocks.append(build_page_html(i, total, content))

    pages_html = "\n<hr class=\"page-divider\">\n".join(page_blocks)

    wrapper = _load_template("output_page.html")
    css = _load_template("output_page.css")
    js = _load_template("output_page.js")

    html = wrapper.replace("{{ title }}", title)
    html = html.replace("{{ css }}", css)
    html = html.replace("{{ js }}", js)
    html = html.replace("{{ toc }}", toc)
    html = html.replace("{{ pages }}", pages_html)

    return html


_EDIT_CSS = """
.inline-edit-input { border: 1px solid #4f8cff; border-radius: 4px; padding: 2px 4px; font: inherit; width: 100%; box-sizing: border-box; resize: vertical; white-space: pre-wrap; }
.inline-edit-input:focus { outline: 2px solid #4f8cff; outline-offset: 1px; }
[contenteditable="true"]:focus { outline: 2px solid #4f8cff; outline-offset: 1px; }
.edited { background: #fff7e6; }
.save-btn { position: fixed; bottom: 24px; right: 24px; padding: 8px 16px; background: #4f8cff; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; z-index: 1000; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
.save-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.save-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: #16a34a; color: #fff; padding: 10px 18px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); animation: fadeInOut 2.5s ease forwards; }
.save-error { background: #dc2626; }
@keyframes fadeInOut { 0% { opacity: 0; } 10% { opacity: 1; } 90% { opacity: 1; } 100% { opacity: 0; } }
"""


def write_page_files(session_id, pages_data, title, output_root=None):
    """Write per-page HTML files and an index for a session."""
    if output_root is None:
        output_root = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "crop_app", "static", "extracted",
        )
    output_root = os.path.realpath(output_root)
    session_dir = os.path.realpath(os.path.join(output_root, session_id))
    if not session_dir.startswith(output_root + os.sep):
        raise ValueError(f"Invalid session_id: {session_id}")
    os.makedirs(session_dir, exist_ok=True)

    total = len(pages_data)
    css = _load_template("output_page.css")
    js = _load_template("output_page_edit.js")

    for i, pdata in enumerate(pages_data):
        content = pdata.get("html", "")
        content = resolve_footnotes(content)
        page_body = build_page_html(i, total, content)

        prev_href = f"page-{i-1}.html" if i > 0 else "#"
        next_href = f"page-{i+1}.html" if i < total - 1 else "#"
        prev_style = 'style="visibility:hidden"' if i == 0 else ""
        next_style = 'style="visibility:hidden"' if i == total - 1 else ""
        page_nav = (
            f'<nav class="page-nav">'
            f'<a href="{prev_href}" class="nav-btn" {prev_style}>← Prev</a>'
            f'<span class="page-indicator">Page {i+1} of {total}</span>'
            f'<a href="{next_href}" class="nav-btn" {next_style}>Next →</a>'
            f'</nav>'
        )

        page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)} — Page {i+1}</title>
  <style>
{css}
{page_nav_css()}
{_EDIT_CSS}
  </style>
</head>
<body>
{page_nav}
<main class="document-canvas">
{page_body}
</main>
<script>
{js}
</script>
</body>
</html>"""
        out_path = os.path.join(session_dir, f"page-{i}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page_html)

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)} — Pages</title>
  <style>
{css}
.page-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; padding: 16px; }}
.page-card {{ display: flex; align-items: center; justify-content: center; padding: 24px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; color: #4f8cff; text-decoration: none; font-weight: 600; }}
.page-card:hover {{ border-color: #4f8cff; }}
  </style>
</head>
<body>
<main class="document-canvas">
  <h1>{html.escape(title)}</h1>
  <p class="page-indicator">{total} page(s)</p>
  <div class="page-grid">
"""
    for i in range(total):
        index_html += f'    <a href="page-{i}.html" class="page-card">Page {i+1}</a>\n'
    index_html += """  </div>
</main>
<script>
  // No special JS needed for index page
</script>
</body>
</html>"""
    with open(os.path.join(session_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)


def page_nav_css() -> str:
    """Return CSS rules for per-page prev/next navigation."""
    return """\
.page-nav { display: flex; justify-content: center; align-items: center; gap: 16px; padding: 16px; }
.nav-btn { color: #4f8cff; text-decoration: none; font-weight: 600; }
.nav-btn[style*="visibility:hidden"] { visibility: hidden; pointer-events: none; }
.page-indicator { font-size: 0.85rem; color: #64748b; }
"""
