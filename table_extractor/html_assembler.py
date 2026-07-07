import os
import re

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _load_template(name: str) -> str:
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
                f'data-footnote="{fn_text.strip()}"><sup>{marker}</sup></a>'
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
    label = f"Page {page_index + 1} of {total_pages}"
    return (
        '<div class="page" id="page-{idx}">\n'
        '  <div class="page-label">{label}</div>\n'
        "  {content}\n"
        "</div>\n"
    ).format(idx=page_index, label=label, content=content_html)


def build_toc(total_pages: int) -> str:
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
