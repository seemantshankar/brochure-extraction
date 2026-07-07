# HTML Extraction Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract all information from annotated PDF pages/images and present it as a single, flat HTML file with premium interactive document-reader UX — preserving all table layouts, symbols, footnotes, and data exactly as they appear in the source.

**Architecture:** Per-region HTML extraction via vision LLM (`qwen/qwen3.7-plus` on OpenRouter). Each committed crop (or full page for Simple-classified pages) is sent to the LLM with a unified prompt (master + all type hints). The LLM produces semantic HTML directly. Fragments are assembled page-by-page with footnote resolution (3-phase: collect markers → link `<a>` refs with `data-footnote` → client-side tooltip/flash interactivity). The output is a self-contained HTML file with embedded CSS/JS for a two-column TOC + sheet-card layout, interactive tables with copy-to-clipboard, and smart footnotes.

**Tech Stack:** Python 3.10+, Flask (SSE via `Response` + `text/event-stream`), OpenAI SDK → OpenRouter, Pillow, Pillow/Jinja2, vanilla HTML/CSS/JS (output), Server-Sent Events.

## Global Constraints

- Extraction model: `qwen/qwen3.7-plus` via OpenRouter (`DATA_EXTRACTION_MODEL_ID` in `.env`)
- Existing OpenAI client pattern: `openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=...)`
- Cache: Reuse `table_extractor/cache.py` pattern (JSON-staged cache in `.stage_cache/`, stage name `html_extract`)
- Output HTML: Self-contained, no external dependencies, embedded CSS + JS
- No images rendered in output HTML (`<img>` tags forbidden)
- No commentary/description added to output
- Existing dark theme CSS variables: `--bg-primary: #1a1a2e`, `--bg-secondary: #16213e`, `--accent: #e94560`, `--border: #2a2a4a` (defined in `crop_app/static/css/style.css:12-28`)
- Button state: "Extract to HTML" disabled when `draft.length > 0` OR `crops.length === 0`
- Footnote resolution: per-page only; cross-page footnotes out of scope
- HTML fragments: LLM may wrap in markdown fences — must clean them up

---

### Task 1: HTML Extraction Prompt Files

**Files:**
- Create: `table_extractor/prompts/html/_master.txt`
- Create: `table_extractor/prompts/html/extract_ruled_table.txt`
- Create: `table_extractor/prompts/html/extract_section_grouped_table.txt`
- Create: `table_extractor/prompts/html/extract_bullet_panel.txt`
- Create: `table_extractor/prompts/html/extract_swatch_grid.txt`
- Create: `table_extractor/prompts/html/extract_stat_cards.txt`
- Create: `table_extractor/prompts/html/extract_technical_drawing.txt`
- Create: `table_extractor/prompts/html/extract_icon_badge.txt`
- Create: `table_extractor/prompts/html/extract_footnote_block.txt`
- Create: `table_extractor/prompts/html/extract_section_heading.txt`
- Create: `table_extractor/prompts/html/extract_other.txt`

**Interfaces:**
- Consumes: (nothing — standalone prompt text files)
- Produces: Plain text files loaded at runtime by `html_extractor.load_full_prompt()`

- [ ] **Step 1: Create prompts/html directory**

Run: `mkdir -p table_extractor/prompts/html/`
Expected: directory created

- [ ] **Step 2: Write the master prompt**

Write `table_extractor/prompts/html/_master.txt`:

```
Role: You are an expert document-to-HTML parser. Your task is to extract all data from the attached image and convert it into semantic HTML fragments.

Strict Operational Constraints:

1. Table Reconstitution: Tables must be meticulously rendered in valid HTML (<table>, <tr>, <td>, <th>). Perfectly preserve the original layout, header styling, row/column merges (rowspan, colspan), symbols, alignment, and text.
2. No Image Rendering: Do not include <img> tags or placeholders. Extract any text, data, or labels embedded within images, grids, or diagrams.
3. 100% Data Coverage: Do not summarize, truncate, or omit any text. Every single word, number, and symbol must be captured.
4. Zero Commentary: Output only the raw HTML code. Do not write introductory text, concluding text, meta-descriptions, or code fences. Do not add any information or interpretations not explicitly present in the source material.
5. Footnote Mapping: Preserve all footnote symbols (e.g., *, †, **, ¹, #) within the body text using <sup> tags and ensure they correctly match their corresponding footnote text.
6. Symbols: Preserve all special characters (●, ✓, ★, →, ±, µ, etc.) exactly as they appear.
7. Output Format: Return only clean HTML fragments. Do NOT wrap output in ```html code fences. Do NOT add markdown formatting.

The guidance below describes common content types you may encounter in a vehicle brochure. Infer the content type from the image and apply the corresponding guidance.
```

- [ ] **Step 3: Write ruled_table type hint**

Write `table_extractor/prompts/html/extract_ruled_table.txt`:

```
Type: Ruled Table. Focus on accurate row/column alignment. Preserve all row spans (rowspan) and column spans (colspan). Section divider rows — full-width rows with bold text that group specification categories — must be rendered as <tr class="section-divider"><td colspan="N">Section Name</td></tr>. Empty cells render as <td>&nbsp;</td>. Footnote markers attached to any cell value must be preserved as <sup>*</sup>, <sup>**</sup>, etc. inside the <td>.
```

- [ ] **Step 4: Write section_grouped_table type hint**

Write `table_extractor/prompts/html/extract_section_grouped_table.txt`:

```
Type: Grouped Specification Table. Column headers may span multiple sub-columns — use colspan on the group header <th>. Section dividers are full-width rows rendered with class="section-divider". Example structure: <tr><th colspan="3">1.0L</th></tr><tr><th></th><th>LXi</th><th>VXi</th></tr>. Section divider rows: <tr class="section-divider"><td colspan="3">ENGINE</td></tr>. Preserve every cell and merge exactly.
```

- [ ] **Step 5: Write bullet_panel type hint**

Write `table_extractor/prompts/html/extract_bullet_panel.txt`:

```
Type: Bullet Panel (trim/feature list). Render as nested <ul>. Top-level items are major features; sub-bullets are sub-features. Preserve bullet characters (●, ✓, -) as they appear in the image — do not convert between types. Trim names should be rendered as <strong>Trim Name</strong> at the start of a <li>.
```

- [ ] **Step 6: Write swatch_grid type hint**

Write `table_extractor/prompts/html/extract_swatch_grid.txt`:

```
Type: Swatch Grid (color specification table). Each row represents a trim/color option. Preserve column alignment and exact color names. Use a standard <table> with headers matching the brochure's column structure (e.g., "Variant", "Colour", "Roof", "Body").
```

- [ ] **Step 7: Write stat_cards type hint**

Write `table_extractor/prompts/html/extract_stat_cards.txt`:

```
Type: Stat Cards. Render each stat as a <div class="stat-card"> containing <dl><dt>LABEL</dt><dd>VALUE UNIT</dd></dl>. If the cards were arranged together, wrap them in a <div class="stat-cards"> container. Preserve exact values and units as shown in the image.
```

- [ ] **Step 8: Write technical_drawing type hint**

Write `table_extractor/prompts/html/extract_technical_drawing.txt`:

```
Type: Technical Drawing / Diagram. Extract all visible measurement labels and their corresponding numeric values with units (mm, cm, degrees, etc.). Render as a <dl> key-value list. Do not reproduce the drawing or diagram itself — capture only the text data (dimension labels, values, units).
```

- [ ] **Step 9: Write icon_badge type hint**

Write `table_extractor/prompts/html/extract_icon_badge.txt`:

```
Type: Icon Badge (warranty, certification, rating badges). Render as <div class="badge"><strong>Badge Name</strong>: value</div>. If the badge contains multiple fields (e.g., "5 Years Standard Warranty" with sub-text "or 1,00,000 km"), include all text within the <div>.
```

- [ ] **Step 10: Write footnote_block type hint**

Write `table_extractor/prompts/html/extract_footnote_block.txt`:

```
Type: Footnote Block (disclaimers, notes). Wrap the entire block in <aside class="footnotes">. Each individual footnote entry must be wrapped in a <p> tag starting with its marker as <sup>marker</sup> followed by the footnote text. Example:
<aside class="footnotes">
  <p><sup>*</sup> Standard across all variants.</p>
  <p><sup>**</sup> Optional, dealer-installed accessory.</p>
</aside>
Preserve every marker exactly as shown (* † ‡ ** ¹ ² # etc.).
```

- [ ] **Step 11: Write section_heading type hint**

Write `table_extractor/prompts/html/extract_section_heading.txt`:

```
Type: Section Heading. Infer the heading level (<h1>, <h2>, <h3>) from the visual prominence and position of the text in the layout. Large, bold, prominent section titles use lower heading numbers. Return just the <hN>...</hN> element.
```

- [ ] **Step 12: Write other type hint**

Write `table_extractor/prompts/html/extract_other.txt`:

```
Type: General / Other. Extract as semantic HTML using the most appropriate elements: <p> for paragraphs, <ul>/<ol> for lists, <table> for any tabular content not clearly matched to the other types, <strong> for emphasized terms. Preserve all text exactly.
```

- [ ] **Step 13: Verify all 11 prompt files exist**

Run: `ls -1 table_extractor/prompts/html/`
Expected output (11 files):
```
_master.txt
extract_bullet_panel.txt
extract_footnote_block.txt
extract_icon_badge.txt
extract_other.txt
extract_ruled_table.txt
extract_section_grouped_table.txt
extract_section_heading.txt
extract_stat_cards.txt
extract_swatch_grid.txt
extract_technical_drawing.txt
```

- [ ] **Step 14: Commit**

Run:
```bash
git add table_extractor/prompts/html/
git commit -m "feat: add HTML extraction prompt files"
```

---

### Task 2: html_extractor.py — LLM Calls, Cleanup, and Caching

**Files:**
- Create: `table_extractor/html_extractor.py`
- Create: `table_extractor/tests/test_html_extractor.py`

**Interfaces:**
- Consumes: `table_extractor/prompts/html/*.txt` (Task 1), `PIL.Image`, OpenRouter/`qwen/qwen3.7-plus`
- Produces:
  - `load_prompt(filename: str) -> str`
  - `load_full_prompt() -> str`
  - `clean_up_html_fragment(raw: str) -> str`
  - `extract_crop_as_html(crop_image: PIL.Image, model: str) -> str`
  - `run_extraction(session_id: str, sm: SessionManager, crop_root: str, model: str) -> str` (full end-to-end orchestration; consumed by Flask route in Task 5)

- [ ] **Step 1: Write tests for load_full_prompt and clean_up_html_fragment**

Write `table_extractor/tests/test_html_extractor.py`:

```python
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_extractor import (
    load_full_prompt,
    load_prompt,
    clean_up_html_fragment,
)


class TestLoadPrompt:
    def test_load_master_returns_content(self):
        content = load_prompt("_master.txt")
        assert "Table Reconstitution" in content
        assert "Zero Commentary" in content

    def test_load_full_prompt_includes_master_and_all_hints(self):
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
    def test_strips_backtick_fences_multiline(self):
        raw = "```html\n<table><tr><td>X</td></tr></table>\n```"
        result = clean_up_html_fragment(raw)
        assert "<table>" in result
        assert "```" not in result

    def test_strips_plain_backtick_block_no_language(self):
        raw = "```\n<p>Hello</p>\n```"
        result = clean_up_html_fragment(raw)
        assert result == "<p>Hello</p>"

    def test_preserves_plain_html_unchanged(self):
        raw = "<table><tr><td>A</td></tr></table>"
        assert clean_up_html_fragment(raw) == raw

    def test_strips_leading_trailing_whitespace(self):
        raw = "  <p>Hi</p>  "
        assert clean_up_html_fragment(raw) == "<p>Hi</p>"

    def test_empty_string_returns_empty(self):
        assert clean_up_html_fragment("") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest table_extractor/tests/test_html_extractor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'table_extractor.html_extractor'`

- [ ] **Step 3: Write html_extractor.py**

Write `table_extractor/html_extractor.py`:

```python
import io
import os
import re
import base64
import logging
from PIL import Image
from openai import OpenAI
from table_extractor.cache import cached_call

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts", "html")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=os.environ.get("OPENROUTER_API_KEY", "mock_key"),
            timeout=120.0,
        )
    return _client


def load_prompt(filename: str) -> str:
    filepath = os.path.join(PROMPTS_DIR, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def load_full_prompt() -> str:
    master = load_prompt("_master.txt")
    hint_files = sorted(
        f for f in os.listdir(PROMPTS_DIR)
        if f.startswith("extract_") and f.endswith(".txt")
    )
    hints = [load_prompt(hf) for hf in hint_files]
    return master + "\n\n" + "\n\n".join(hints)


def clean_up_html_fragment(raw: str) -> str:
    """Strip markdown fences, backtick blocks, and surrounding whitespace from raw LLM output."""
    text = raw.strip()
    if not text:
        return ""

    fenced_pattern = re.search(
        r"^```\s*(?:html|HTML)?\s*\n(.*?)\n```\s*$", text, re.DOTALL
    )
    if fenced_pattern:
        text = fenced_pattern.group(1).strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:]
        while text.startswith("\n"):
            text = text[1:]
        if text.endswith("\n```"):
            text = text[:-4]
        text = text.strip()

    return text


def extract_crop_as_html(crop_image: Image.Image, model: str) -> str:
    """Send one crop image to the LLM and return an HTML fragment string."""
    system_prompt = load_full_prompt()

    buf = io.BytesIO()
    crop_image.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    def _call():
        response = _get_client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        }
                    ],
                },
            ],
            max_tokens=8192,
        )

        raw_content = response.choices[0].message.content or ""
        html_fragment = clean_up_html_fragment(raw_content)

        usage_meta = {}
        if response.usage:
            usage_meta = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }

        return [html_fragment, usage_meta]

    try:
        result = cached_call(
            image_bytes=img_bytes,
            stage="html_extract",
            model=model,
            fn=_call,
            force=False,
        )
    except Exception as e:
        logger.error(f"LLM extraction failed for crop (model={model}): {e}")
        raise

    return result[0]


def run_extraction(session_id: str, sm, crop_root: str, model: str):
    """Run full HTML extraction, yielding progress dicts and the final HTML document.

    Yields:
        {"status": "progress", "page": int, "totalPages": int, "log": str} — during extraction
        {"status": "done", "html": str} — final assembled HTML document
    """
    from table_extractor.html_assembler import assemble_full_document

    meta = sm.load_meta(session_id)
    if not meta:
        raise ValueError(f"Session {session_id} not found")

    pages = meta.get("pages", [])
    page_dir = sm.get_page_dir(session_id)
    session_files = meta.get("files", [])
    title = session_files[0] if session_files else f"Session {session_id[:8]}"
    total_pages = len(pages)

    pages_data = []

    for page_idx, page_info in enumerate(pages):
        yield {
            "status": "progress",
            "page": page_idx,
            "totalPages": total_pages,
            "log": f"Processing Page {page_idx + 1} of {total_pages}...",
        }

        parts = []
        classification = page_info.get("classification")
        if classification is None:
            classification = "Complex" if page_info.get("complex") else "Simple"

        crops = page_info.get("crops") or []
        is_complex = classification == "Complex"

        if not is_complex or len(crops) == 0:
            page_path = os.path.join(page_dir, page_info["path"])
            if os.path.exists(page_path):
                try:
                    page_img = Image.open(page_path)
                    fragment = extract_crop_as_html(page_img, model)
                    if fragment:
                        parts.append(fragment)
                except Exception as e:
                    logger.error(f"Simple page extraction failed: {e}")
                    parts.append(f'<div class="error-region">Extraction failed: {e}</div>')
            else:
                parts.append(f'<div class="error-region">Page file not found: {page_info["path"]}</div>')
        else:
            sorted_crops = sorted(crops, key=lambda c: c.get("bbox", [0, 0, 0, 0])[1])
            total_crops = len(sorted_crops)
            for crop_idx, crop_info in enumerate(sorted_crops):
                crop_filename = (
                    crop_info.get("filename")
                    or crop_info.get("path")
                    or crop_info.get("crop_filename")
                )
                yield {
                    "status": "progress",
                    "page": page_idx,
                    "totalPages": total_pages,
                    "crop": crop_idx + 1,
                    "totalCrops": total_crops,
                    "log": f"  - Extracting crop {crop_idx + 1}/{total_crops} (Page {page_idx + 1})...",
                }
                if not crop_filename:
                    parts.append('<div class="error-region">Crop missing filename reference</div>')
                    continue
                crop_path = os.path.join(crop_root, session_id, crop_filename)
                if not os.path.exists(crop_path):
                    parts.append(f'<div class="error-region">Crop file not found: {crop_filename}</div>')
                    continue
                try:
                    crop_img = Image.open(crop_path)
                    fragment = extract_crop_as_html(crop_img, model)
                    if fragment:
                        parts.append(fragment)
                except Exception as e:
                    logger.error(f"Crop extraction failed for {crop_filename}: {e}")
                    parts.append(
                        f'<div class="error-region">Extraction failed for {crop_filename}: {e}</div>'
                    )

        pages_data.append({"html": "\n".join(parts)})

    yield {
        "status": "progress",
        "page": total_pages,
        "totalPages": total_pages,
        "log": "Assembling final HTML document...",
    }

    result_html = assemble_full_document(pages_data, title)

    yield {"status": "done", "html": result_html}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest table_extractor/tests/test_html_extractor.py -v`
Expected: 7 PASS (5 load_prompt tests + 5 cleanup tests — 10 total, but note the structure: `TestLoadPrompt` has 2 tests, `TestCleanUpHtmlFragment` has 5. Total 7.)

- [ ] **Step 5: Commit**

Run:
```bash
git add table_extractor/html_extractor.py table_extractor/tests/test_html_extractor.py
git commit -m "feat: add html_extractor with LLM calls, cleanup, caching, and run_extraction"
```

---

### Task 3: html_assembler.py + Output Templates

**Files:**
- Create: `table_extractor/html_assembler.py`
- Create: `table_extractor/templates/output_page.css`
- Create: `table_extractor/templates/output_page.js`
- Create: `table_extractor/templates/output_page.html`
- Create: `table_extractor/tests/test_html_assembler.py`

**Interfaces:**
- Consumes: `pages_data: list[{"html": str}]`, title `str` from Task 2's `run_extraction`
- Produces: `assemble_full_document(pages_data, title) -> str`, `resolve_footnotes(page_html) -> str`, `build_page_html(page_index, total_pages, content_html) -> str`

- [ ] **Step 1: Create templates directory**

Run: `mkdir -p table_extractor/templates/`
Expected: directory created

- [ ] **Step 2: Write output_page.css**

Write `table_extractor/templates/output_page.css`:

```css
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #f1f5f9;
  color: #1e293b;
  line-height: 1.6;
  display: flex;
  min-height: 100vh;
}

.toc-sidebar {
  position: fixed;
  top: 0; left: 0;
  width: 200px;
  height: 100vh;
  background: #fff;
  border-right: 1px solid #e2e8f0;
  padding: 20px 12px;
  overflow-y: auto;
  z-index: 100;
}
.toc-sidebar h3 {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
  color: #94a3b8; margin-bottom: 12px;
}
.toc-sidebar a {
  display: block; padding: 6px 10px; font-size: 0.85rem;
  color: #475569; text-decoration: none; border-radius: 4px; transition: background 0.15s;
}
.toc-sidebar a:hover { background: #f1f5f9; color: #1e293b; }
.toc-sidebar a.active { background: #e2e8f0; font-weight: 600; }

.document-canvas { margin-left: 200px; padding: 40px; flex: 1; min-width: 0; }

.page {
  background: #fff; border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1), 0 1px 2px rgba(0,0,0,0.06);
  padding: 48px; margin-bottom: 32px; position: relative;
}
.page-label { font-size: 0.75rem; color: #94a3b8; text-align: center; margin-bottom: 24px; }
.page-divider { border: none; border-top: 2px dashed #e2e8f0; margin: 0 0 32px; }

h1, h2, h3, h4, h5, h6 { margin-bottom: 12px; color: #0f172a; }

table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 20px; }
.table-scroll-wrap { overflow-x: auto; margin-bottom: 20px; scrollbar-width: thin; }
.table-scroll-wrap::-webkit-scrollbar { height: 6px; }
.table-scroll-wrap::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
.table-container { position: relative; }
.copy-table-btn {
  position: absolute; top: 4px; right: 4px; z-index: 10;
  background: #fff; border: 1px solid #e2e8f0; border-radius: 4px;
  padding: 4px 8px; font-size: 0.75rem; color: #64748b; cursor: pointer;
  opacity: 0; transition: opacity 0.2s;
}
.table-container:hover .copy-table-btn { opacity: 1; }
.copy-table-btn:hover { background: #f8fafc; color: #1e293b; }

th, td { border: 1px solid #d1d5db; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #f5f5f5; font-weight: 600; white-space: nowrap; }
tr:hover td { background-color: #f8fafc; }
tr.section-divider td { font-weight: 700; background: #eef2ff; color: #334155; }

.footnotes, aside.footnotes {
  font-size: 12px; color: #64748b; border-left: 3px solid #e2e8f0;
  padding-left: 16px; margin-top: 24px;
}
.footnotes p { margin-bottom: 4px; }
.footnotes .fn-flash { animation: fn-flash 1s ease-out; }

a.footnote-ref { text-decoration: none; color: #6366f1; cursor: pointer; }
a.footnote-ref:hover { text-decoration: underline; }
a.footnote-back { text-decoration: none; color: #6366f1; margin-left: 4px; font-size: 0.85em; }

dl { margin-bottom: 16px; }
.stat-cards { display: flex; gap: 12px; flex-wrap: wrap; }
.stat-card { border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px 16px; flex: 1; min-width: 140px; }
.stat-card dt { font-weight: 600; color: #334155; font-size: 0.85rem; }
.stat-card dd { color: #1e293b; font-size: 1.1rem; font-weight: 600; margin-top: 2px; }

.badge { border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px 16px; display: inline-block; }

ul, ol { margin-bottom: 16px; padding-left: 24px; }
li { margin-bottom: 4px; }

.footnote-tooltip {
  position: absolute; background: #1e293b; color: #f8fafc;
  padding: 6px 10px; border-radius: 4px; font-size: 12px;
  max-width: 300px; z-index: 1000; pointer-events: none;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}

.error-region {
  background: #fef2f2; border-left: 3px solid #ef4444;
  padding: 12px 16px; color: #991b1b; margin-bottom: 12px;
  border-radius: 0 4px 4px 0;
}

@keyframes fn-flash { 0% { background: #fef9c3; } 100% { background: transparent; } }
.fn-flash { animation: fn-flash 1s ease-out; }

@media (max-width: 768px) {
  .toc-sidebar { width: 100%; height: auto; position: relative; border-right: none; border-bottom: 1px solid #e2e8f0; }
  .document-canvas { margin-left: 0; padding: 16px; }
  .page { padding: 24px; }
}
```

- [ ] **Step 3: Write output_page.js**

Write `table_extractor/templates/output_page.js`:

```javascript
document.addEventListener("DOMContentLoaded", function () {
  var tooltip = null;

  function getTooltip() {
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "footnote-tooltip";
      tooltip.style.display = "none";
      document.body.appendChild(tooltip);
    }
    return tooltip;
  }

  function positionTooltip(ref) {
    if (!tooltip) return;
    var rect = ref.getBoundingClientRect();
    var ttRect = tooltip.getBoundingClientRect();
    var top = rect.bottom + 6;
    var left = rect.left;
    if (left + ttRect.width > window.innerWidth - 16) {
      left = Math.max(8, window.innerWidth - ttRect.width - 16);
    }
    if (top + ttRect.height > window.innerHeight - 16) {
      top = rect.top - ttRect.height - 6;
    }
    tooltip.style.top = top + "px";
    tooltip.style.left = left + "px";
  }

  document.addEventListener("mouseover", function (e) {
    var ref = e.target.closest(".footnote-ref");
    if (!ref) return;
    var fnText = ref.getAttribute("data-footnote");
    if (!fnText) return;
    var tt = getTooltip();
    tt.textContent = fnText;
    tt.style.display = "block";
    positionTooltip(ref);
  });

  document.addEventListener("mouseout", function (e) {
    var ref = e.target.closest(".footnote-ref");
    if (!ref) return;
    if (tooltip) tooltip.style.display = "none";
  });

  document.addEventListener("mousemove", function (e) {
    if (tooltip && tooltip.style.display !== "none") {
      var ref = e.target.closest(".footnote-ref");
      if (ref) positionTooltip(ref);
    }
  });

  document.addEventListener("click", function (e) {
    var ref = e.target.closest(".footnote-ref");
    if (ref) {
      e.preventDefault();
      var targetId = ref.getAttribute("href");
      if (targetId && targetId.startsWith("#")) {
        var target = document.getElementById(targetId.slice(1));
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.remove("fn-flash");
          void target.offsetWidth;
          target.classList.add("fn-flash");
          setTimeout(function () { target.classList.remove("fn-flash"); }, 1000);
        }
      }
      if (tooltip) tooltip.style.display = "none";
      return;
    }

    var back = e.target.closest(".footnote-back");
    if (back) {
      e.preventDefault();
      var href = back.getAttribute("href");
      if (href && href.startsWith("#")) {
        var srcRef = document.getElementById(href.slice(1));
        if (srcRef) {
          srcRef.scrollIntoView({ behavior: "smooth", block: "center" });
          srcRef.style.transition = "background 0.3s";
          srcRef.style.background = "#fef9c3";
          setTimeout(function () { srcRef.style.background = ""; }, 1000);
        }
      }
      return;
    }
  });

  var tables = document.querySelectorAll("table");
  tables.forEach(function (table) {
    var wrap = document.createElement("div");
    wrap.className = "table-scroll-wrap";
    var container = document.createElement("div");
    container.className = "table-container";

    var btn = document.createElement("button");
    btn.className = "copy-table-btn";
    btn.textContent = "Copy";
    btn.type = "button";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      navigator.clipboard.writeText(table.outerHTML).then(function () {
        btn.textContent = "Copied!";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      }).catch(function () {
        var ta = document.createElement("textarea");
        ta.value = table.outerHTML;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (err) {}
        document.body.removeChild(ta);
        btn.textContent = "Copied!";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      });
    });

    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(container);
    container.appendChild(btn);
    container.appendChild(table);
  });

  var tocLinks = document.querySelectorAll(".toc-sidebar a");
  tocLinks.forEach(function (link) {
    link.addEventListener("click", function () {
      tocLinks.forEach(function (l) { l.classList.remove("active"); });
      link.classList.add("active");
    });
  });

  var pageObserver = null;
  if (window.IntersectionObserver) {
    var pages = document.querySelectorAll(".page");
    pageObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var href = "#" + entry.target.id;
          var link = document.querySelector('.toc-sidebar a[href="' + href + '"]');
          if (link) {
            tocLinks.forEach(function (l) { l.classList.remove("active"); });
            link.classList.add("active");
          }
        }
      });
    }, { threshold: 0.4 });
    pages.forEach(function (p) { pageObserver.observe(p); });
  }
});
```

- [ ] **Step 4: Write output_page.html wrapper**

Write `table_extractor/templates/output_page.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ title }}</title>
  <style>
{{ css }}
  </style>
</head>
<body>
  <nav class="toc-sidebar">
    <h3>Contents</h3>
{{ toc }}
  </nav>
  <main class="document-canvas">
{{ pages }}
  </main>
  <script>
{{ js }}
  </script>
</body>
</html>
```

- [ ] **Step 5: Write tests for html_assembler**

Write `table_extractor/tests/test_html_assembler.py`:

```python
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_assembler import (
    resolve_footnotes,
    build_page_html,
    build_toc,
    assemble_full_document,
)


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
        assert toc.count("page-") == 6  # 3 in href, 3 in link text
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
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest table_extractor/tests/test_html_assembler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'table_extractor.html_assembler'`

- [ ] **Step 7: Write html_assembler.py**

Write `table_extractor/html_assembler.py`:

```python
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
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest table_extractor/tests/test_html_assembler.py -v`
Expected: 9 PASS

- [ ] **Step 9: Run all table_extractor tests to confirm no regressions**

Run: `python -m pytest table_extractor/tests/ -v`
Expected: All existing tests + new tests PASS

- [ ] **Step 10: Commit**

Run:
```bash
git add table_extractor/html_assembler.py table_extractor/templates/ table_extractor/tests/test_html_assembler.py
git commit -m "feat: add html_assembler with footnote resolution and premium output template"
```

---

### Task 4: "Extract to HTML" Button + Commit-State Tracking

**Files:**
- Create: `crop_app/static/css/extract.css`
- Modify: `crop_app/templates/annotate.html` — line 31 (after `<button id="commit-btn">`)
- Modify: `crop_app/static/js/annotate.js` — commit-state wiring (called from `updateCropPanel`)
- Modify: `crop_app/static/css/style.css` — add extract button styles (append after commit-btn rules, near line 554)

**Interfaces:**
- Consumes: `sessionId`, current draft/committed-boxes state in `annotate.js`'s `state` object
- Produces: `<button id="extract-btn">` with auto-update on every commit/draw/resize/delete

- [ ] **Step 1: Add the extract button HTML to annotate.html**

In `crop_app/templates/annotate.html`, find line 31:

```html
<button id="commit-btn" disabled class="commit-btn">Commit All Crops</button>
```

Insert immediately **after** that line (between lines 31 and 32):

```html
      <button id="extract-btn" disabled class="extract-btn" title="Commit at least one crop region first">Extract to HTML</button>
```

The crop-panel section now looks like:

```html
    <div class="crop-panel" id="crop-panel">
      <h3>Crops</h3>
      <div id="crop-list" class="crop-list">
        <p class="empty-hint">Draw bounding boxes to create crops</p>
      </div>
      <button id="commit-btn" disabled class="commit-btn">Commit All Crops</button>
      <button id="extract-btn" disabled class="extract-btn" title="Commit at least one crop region first">Extract to HTML</button>
    </div>
```

- [ ] **Step 2: Add extract button CSS to style.css**

In `crop_app/static/css/style.css`, find the commit-btn rules around line 554. Insert the following **after** the `.commit-btn:disabled` block (after line 554):

```css
.extract-btn {
  flex-shrink: 0;
  margin: 0 16px 12px;
  padding: 12px;
  font-size: 0.9rem;
  font-weight: 600;
  color: #fff;
  background: #2563eb;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background 0.2s ease, opacity 0.2s ease;
}

.extract-btn:hover:not(:disabled) {
  background: #1d4ed8;
}

.extract-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
```

- [ ] **Step 3: Add `updateExtractButton` function to annotate.js**

In `crop_app/static/js/annotate.js`, find the end of the `updateCropPanel` function. At the very beginning of `updateCropPanel` (line 366, right after `const commitBtn = document.getElementById("commit-btn");`), add a call to sync the extract button state:

Add this line at the **top** of `updateCropPanel` (right after the line `const cropList = document.getElementById("crop-list"); const commitBtn = document.getElementById("commit-btn");`):

```javascript
    updateExtractButton(boxes);
```

And add the function definition itself. Insert it just **before** the `function updateCropPanel(boxes) {` declaration (before line 366):

```javascript
  function updateExtractButton(boxes) {
    var extractBtn = document.getElementById("extract-btn");
    if (!extractBtn) return;
    var hasCommitted = boxes.some(function (b) { return b.committed; });
    var hasUncommitted = countUncommitted(boxes) > 0;
    if (hasCommitted && !hasUncommitted) {
      extractBtn.disabled = false;
      extractBtn.title = "";
    } else {
      extractBtn.disabled = true;
      extractBtn.title = hasUncommitted
        ? "Commit your changes to enable HTML extraction."
        : "Commit at least one crop region first.";
    }
  }
```

This ensures every call to `updateCropPanel` (which fires on every draw/move/resize/delete/commit) re-evaluates the button state.

- [ ] **Step 4: Add click handler for the extract button**

In `annotate.js`, find the `initEventListeners` function. Locate the commit-btn click listener (around line 637-642). Insert the following **after** the commit-btn listener block:

```javascript
    var extractBtn = document.getElementById("extract-btn");
    if (extractBtn) {
      extractBtn.addEventListener("click", function () {
        if (extractBtn.disabled) return;
        fetch("/session/" + sessionId)
          .then(function (r) { return r.json(); })
          .then(function (meta) {
            var hasDraft = (meta.pages || []).some(function (p) { return p.draft && p.draft.length > 0; });
            if (hasDraft) {
              showToast("You have uncommitted changes. Please commit first.", "error");
              updateExtractButton(state.boxes);
              return;
            }
            window.location = "/extract-html/" + sessionId;
          })
          .catch(function (err) {
            showToast("Could not verify commit state.", "error");
          });
      });
    }
```

- [ ] **Step 5: Manually verify in browser**

Start the app: `cd crop_app && python -m flask --app app run --debug --port 5000`
Open: http://localhost:5000/

Verify:
1. Upload a PDF with pages. Click through to annotate a Complex page.
2. Draw a bounding box. Observe: extract button is **disabled** (no commits yet).
3. Click "Commit All Crops". Observe: extract button is **enabled** (blue, no tooltip).
4. Draw another bounding box. Observe: extract button becomes **disabled** again.
5. Hover disabled button — tooltip shows "Commit your changes..."
6. Click Commit again — button re-enables.
7. Delete a crop — button becomes disabled.

- [ ] **Step 6: Commit**

Run:
```bash
git add crop_app/templates/annotate.html crop_app/static/js/annotate.js crop_app/static/css/style.css crop_app/static/css/extract.css
git commit -m "feat: add Extract to HTML button with commit-state tracking"
```

---

### Task 5: Flask Routes for HTML Extraction

**Files:**
- Create: `crop_app/static/extracted/` (empty directory — needs a `.gitkeep` file since git ignores empty dirs)
- Create: `crop_app/templates/error.html` (for the 400 commit-state error)
- Create: `crop_app/tests/test_extract_routes.py`
- Modify: `crop_app/app.py` (add routes and imports)

**Interfaces:**
- Consumes: `/extract-html/<session_id>` — renders progress page; checks for uncommitted draft first.
- Consumes: `GET /extract-progress/<session_id>` — SSE endpoint streaming extraction progress; calls `table_extractor.html_extractor.run_extraction`.
- Consumes: `GET /extracted/<session_id>/extraction.html` — serves the saved HTML file.
- Produces: SSE messages: `data: {"status": "starting"}` / `data: {"status": "done"}` / `data: {"status": "error", "message": "..."}`

- [ ] **Step 1: Create the output directory with a .gitkeep file**

Run:
```bash
mkdir -p crop_app/static/extracted/
touch crop_app/static/extracted/.gitkeep
```

Note: Add `crop_app/static/extracted/*.html` to `.gitignore` (so generated HTML files are not committed):

In the project root `.gitignore` file, near the existing rules for `crops/`, `uploads/`, add:

```
crop_app/static/extracted/*.html
```

- [ ] **Step 2: Write error.html template**

Write `crop_app/templates/error.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Error — Brochure Extraction</title>
  <link rel="stylesheet" href="/static/css/style.css">
</head>
<body class="annotate-body">
  <div style="display:flex;align-items:center;justify-content:center;min-height:100vh;">
    <div style="background:var(--bg-secondary);padding:40px;border-radius:12px;max-width:520px;text-align:center;border:1px solid var(--border);">
      <h2 style="color:#ef4444;margin-bottom:16px;">Cannot Extract HTML</h2>
      <p style="color:var(--text-primary);margin-bottom:24px;">{{ message }}</p>
      <a href="/sessions" style="color:#60a5fa;text-decoration:none;">&larr; Back to Sessions</a>
    </div>
  </div>
</body>
</html>
```

- [ ] **Step 3: Write tests for the new routes**

Write `crop_app/tests/test_extract_routes.py`:

```python
import os
import sys
import json
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager


@pytest.fixture
def client_ready_session(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")

    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm
    sid = sm.create_session()

    page_dir = sm.get_page_dir(sid)
    img = Image.new("RGB", (200, 300), "blue")
    img.save(os.path.join(page_dir, "page_000.png"))

    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    crop = Image.new("RGB", (50, 50), "red")
    crop.save(os.path.join(crop_dir, "crop_000.png"))

    sm.save_meta(sid, {
        "files": ["test.pdf"],
        "pages": [{
            "path": "page_000.png",
            "classification": "Complex",
            "crops": [{"path": "crop_000.png", "filename": "crop_000.png", "bbox": [0.1, 0.1, 0.5, 0.5]}],
        }],
    })

    with app.test_client() as client:
        yield client, sid


def test_extract_html_progress_page_renders(client_ready_session):
    client, sid = client_ready_session
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 200
    assert b"Initializing extraction" in resp.data


def test_extract_html_block_when_draft_present(client_ready_session):
    client, sid = client_ready_session
    sm = client.application.session_manager
    meta = sm.load_meta(sid)
    meta["pages"][0]["draft"] = [{"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}]
    sm.save_meta(sid, meta)

    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert b"uncommitted changes" in resp.data


def test_extract_progress_sse_streams_starting(client_ready_session):
    client, sid = client_ready_session

    with pytest.MonkeyPatch.context() as m:
        import table_extractor.html_extractor as hx
        # Mock run_extraction as an iterable generator of event dicts
        m.setattr(
            hx,
            "run_extraction",
            lambda **kw: iter([
                {"status": "starting"},
                {"status": "progress", "page": 0, "totalPages": 1, "log": "Processing..."},
                {"status": "done", "html": "<html><body>ok</body></html>"},
            ]),
        )

        resp = client.get(f"/extract-progress/{sid}")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")

        data = b"".join(resp.iter_encoded()).decode()
        assert "starting" in data
        assert "done" in data


def test_extracted_html_serving_requires_file(client_ready_session):
    client, sid = client_ready_session
    resp = client.get(f"/extracted/{sid}/extraction.html")
    assert resp.status_code == 404
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd crop_app && python -m pytest tests/test_extract_routes.py -v`
Expected: FAIL — routes not defined in `app.py`.

- [ ] **Step 5: Modify app.py to add the new routes**

In `crop_app/app.py`, make these changes:

**a) Update the `from flask import ...` line (line 4).** Change:

```python
from flask import Flask, request, jsonify, redirect, url_for, send_file, render_template
```

to:

```python
from flask import Flask, request, jsonify, redirect, url_for, send_file, render_template, Response
```

**b) Add missing imports at the top.** After line 4, insert:

```python
import json
import sys
```

**c) Add the three new routes.** Just before the `return app` statement (currently line 407), insert:

```python
    def _check_session_ready(session_id):
        meta = _sm.load_meta(session_id)
        if not meta:
            return None, "Session not found"
        for page in meta.get("pages", []):
            if page.get("draft") and len(page["draft"]) > 0:
                return None, "You have uncommitted changes. Please commit them before extracting HTML."
        if not any(page.get("crops") for page in meta.get("pages", [])):
            return None, "No crops have been committed. Please commit at least one crop region before extracting HTML."
        return meta, None

    @app.route("/extract-html/<session_id>", methods=["GET"])
    def extract_html_page(session_id):
        nonlocal_meta, err = _check_session_ready(session_id)
        if err:
            return render_template("error.html", message=err), 400
        return render_template("extract_progress.html", session_id=session_id)

    @app.route("/extract-progress/<session_id>", methods=["GET"])
    def extract_progress_sse(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        def generate():
            yield f"data: {json.dumps({'status': 'starting'})}\n\n"

            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            from table_extractor.html_extractor import run_extraction

            try:
                for event in run_extraction(
                    session_id=session_id,
                    sm=_sm,
                    crop_root=app.config["CROP_DIR"],
                    model=os.environ.get("DATA_EXTRACTION_MODEL_ID", "qwen/qwen3.7-plus"),
                ):
                    if event["status"] == "done":
                        result_html = event["html"]
                        out_dir = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "static", "extracted", session_id,
                        )
                        os.makedirs(out_dir, exist_ok=True)
                        out_path = os.path.join(out_dir, "extraction.html")
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(result_html)

                        yield f"data: {json.dumps({'status': 'done'})}\n\n"
                    else:
                        yield f"data: {json.dumps(event)}\n\n"

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error("HTML extraction failed for session %s: %s\n%s", session_id, e, tb)
                yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

        return Response(generate(), mimetype="text/event-stream")

    @app.route("/extracted/<session_id>/extraction.html", methods=["GET"])
    def serve_extracted_html(session_id):
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "extracted", session_id, "extraction.html",
        )
        if not os.path.exists(out_path):
            return "Extraction not found. Please run extraction first.", 404
        return send_file(out_path, mimetype="text/html")
```

**d) Fix the variable shadowing issue in `/extract-html/<session_id>`.** The helper references `_sm` before it's bound inside the route. Replace the helper with a local function that takes `_sm` as an argument:

Replace the helper above with this inline version inside the route:

```python
    @app.route("/extract-html/<session_id>", methods=["GET"])
    def extract_html_page(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return render_template("error.html", message="Session not found"), 400
        meta = _sm.load_meta(session_id)
        for page in meta.get("pages", []):
            if page.get("draft") and len(page["draft"]) > 0:
                return render_template("error.html", message="You have uncommitted changes. Please commit them before extracting HTML."), 400
        if not any(page.get("crops") for page in meta.get("pages", [])):
            return render_template("error.html", message="No crops have been committed. Please commit at least one crop region before extracting HTML."), 400
        return render_template("extract_progress.html", session_id=session_id)
```

(Remove the `_check_session_ready` helper — use the inline check above instead.)

- [ ] **Step 6: Run all tests to verify PASS**

Run: `cd crop_app && python -m pytest tests/ -v`
Expected: All existing tests + new extract route tests PASS

- [ ] **Step 7: Commit**

Run:
```bash
git add crop_app/app.py crop_app/templates/error.html crop_app/static/extracted/.gitkeep crop_app/tests/test_extract_routes.py .gitignore
git commit -m "feat: add extract-html, SSE progress, and extracted-html-serving Flask routes"
```

---

### Task 6: Progress Page Template + SSE Frontend

**Files:**
- Create: `crop_app/templates/extract_progress.html`
- Create: `crop_app/static/js/extract_progress.js`

**Interfaces:**
- Consumes: `session_id` from Jinja, SSE from `/extract-progress/<session_id>`
- Produces: Progress UI — animated circular gauge + action log ticker. On done: success state + "Open HTML" button.

- [ ] **Step 1: Write extract_progress.html**

Write `crop_app/templates/extract_progress.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Extracting — Brochure Extraction</title>
  <link rel="stylesheet" href="/static/css/style.css">
  <style>
    .extract-page { display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100vh; padding:40px 16px; }
    .extract-card {
      background:var(--bg-secondary); border:1px solid var(--border); border-radius:var(--radius-lg);
      padding:40px; max-width:620px; width:100%; text-align:center;
    }
    .progress-gauge { width:140px; height:140px; margin:0 auto 24px; position:relative; }
    .progress-gauge svg { width:100%; height:100%; transform:rotate(-90deg); }
    .progress-gauge circle { fill:none; stroke-width:10; stroke-linecap:round; }
    .progress-gauge .bg-circle { stroke:var(--border); }
    .progress-gauge .fg-circle {
      stroke:var(--accent); stroke-dasharray:408.407; stroke-dashoffset:408.407;
      transition:stroke-dashoffset 0.5s ease;
    }
    .progress-gauge .pct-text {
      position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
      font-size:1.4rem; font-weight:700; color:var(--text-primary); font-variant-numeric:tabular-nums;
    }
    .status-text { font-size:1.05rem; color:var(--text-primary); margin-bottom:24px; min-height:1.6em; }
    .action-log {
      background:var(--bg-primary); border:1px solid var(--border); border-radius:var(--radius);
      padding:12px 16px; max-height:240px; overflow-y:auto; text-align:left;
      font-family:'Courier New', Consolas, monospace; font-size:0.82rem; color:var(--text-secondary);
      scrollbar-width:thin; scrollbar-color:var(--border) transparent;
    }
    .action-log::-webkit-scrollbar { width:6px; }
    .action-log::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
    .log-entry { margin-bottom:2px; line-height:1.4; }
    .log-time { color:var(--text-muted); margin-right:8px; }
    .success-block { text-align:center; }
    .success-icon { font-size:3.5rem; color:#22c55e; margin-bottom:8px; }
    .error-block { padding:16px; background:rgba(233,69,96,0.1); border:1px solid var(--accent); border-radius:var(--radius); color:var(--text-primary); margin-bottom:16px; text-align:left; }
    .extract-actions { margin-top:24px; display:flex; gap:12px; justify-content:center; flex-wrap:wrap; }
    .extract-actions a, .extract-actions button {
      padding:12px 24px; border-radius:var(--radius); font-size:0.9rem; font-weight:600;
      text-decoration:none; cursor:pointer; border:none; display:inline-flex; align-items:center; gap:6px;
    }
    .btn-open { background:var(--accent); color:#fff; }
    .btn-open:hover { background:var(--accent-hover); }
    .btn-secondary { background:var(--bg-surface); color:var(--text-primary); border:1px solid var(--border); }
    .btn-secondary:hover { background:var(--bg-surface-hover); }
    @keyframes spin-success { 0% { opacity:0; transform:scale(0.6); } 100% { opacity:1; transform:scale(1); } }
    .success-icon { animation:spin-success 0.5s ease-out; }
  </style>
</head>
<body class="annotate-body">
  <div class="extract-page">
    <div class="extract-card">
      <div id="progress-area">
        <div class="progress-gauge">
          <svg viewBox="0 0 140 140">
            <circle class="bg-circle" cx="70" cy="70" r="65"></circle>
            <circle class="fg-circle" id="gauge-circle" cx="70" cy="70" r="65"></circle>
          </svg>
          <div class="pct-text" id="pct-text">0%</div>
        </div>
        <p class="status-text" id="status-text">Initializing extraction...</p>
        <div class="action-log" id="action-log">
          <div class="log-entry"><span class="log-time">--:--:--</span> Waiting for server...</div>
        </div>
      </div>

      <div id="result-area" hidden>
        <div class="success-block">
          <div class="success-icon">&#10003;</div>
          <p class="status-text">Extraction complete!</p>
          <p style="color:var(--text-secondary); font-size:0.9rem; margin-bottom:24px;">
            Your HTML document is ready to view.
          </p>
        </div>
        <div class="extract-actions">
          <a class="btn-open" id="open-btn" href="#" target="_blank">Open HTML Document &rarr;</a>
          <a class="btn-secondary" href="/annotate/{{ session_id }}?page=0">&larr; Back to Annotations</a>
        </div>
      </div>

      <div id="error-area" hidden>
        <div class="error-block" id="error-msg">Extraction failed.</div>
        <div class="extract-actions">
          <a class="btn-secondary" href="/annotate/{{ session_id }}?page=0">&larr; Back to Annotations</a>
        </div>
      </div>
    </div>
  </div>

  <script>
    window.SESSION_ID = "{{ session_id }}";
  </script>
  <script src="/static/js/extract_progress.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write extract_progress.js**

Write `crop_app/static/js/extract_progress.js`:

```javascript
document.addEventListener("DOMContentLoaded", function () {
  var sessionId = window.SESSION_ID;
  var R = 65;
  var CIRCUMFERENCE = 2 * Math.PI * R;

  var gaugeCircle = document.getElementById("gauge-circle");
  var pctText = document.getElementById("pct-text");
  var statusText = document.getElementById("status-text");
  var actionLog = document.getElementById("action-log");
  var progressArea = document.getElementById("progress-area");
  var resultArea = document.getElementById("result-area");
  var errorArea = document.getElementById("error-area");
  var errorMsg = document.getElementById("error-msg");
  var openBtn = document.getElementById("open-btn");

  openBtn.href = "/extracted/" + sessionId + "/extraction.html";

  function setProgress(pct) {
    pct = Math.max(0, Math.min(100, pct));
    var offset = CIRCUMFERENCE - (pct / 100) * CIRCUMFERENCE;
    gaugeCircle.style.strokeDasharray = CIRCUMFERENCE;
    gaugeCircle.style.strokeDashoffset = offset;
    pctText.textContent = Math.round(pct) + "%";
  }

  function getTimestamp() {
    var d = new Date();
    function pad(n) { return n < 10 ? "0" + n : n; }
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function appendLog(message) {
    var entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = '<span class="log-time">[' + getTimestamp() + ']</span> ' + message;
    actionLog.appendChild(entry);
    actionLog.scrollTop = actionLog.scrollHeight;
  }

  function showResult() {
    progressArea.hidden = true;
    resultArea.hidden = false;
  }

  function showError(message) {
    progressArea.hidden = true;
    errorArea.hidden = false;
    errorMsg.textContent = message || "Extraction failed. Please check server logs.";
  }

  setProgress(3);
  appendLog("Starting extraction pipeline...");

  var source = new EventSource("/extract-progress/" + sessionId);

  source.onmessage = function (e) {
    var data;
    try { data = JSON.parse(e.data); } catch (_) { return; }

    if (data.status === "starting") {
      setProgress(10);
      statusText.textContent = "Processing pages...";
      appendLog("Extraction server connected. Working...");

    } else if (data.status === "progress") {
      var pct = 10 + ((data.page || 0) / Math.max(1, data.totalPages || 1)) * 80;
      setProgress(Math.min(pct, 90));
      statusText.textContent = "Page " + ((data.page || 0) + 1) + " of " + (data.totalPages || "?");
      if (data.log) appendLog(data.log);

    } else if (data.status === "done") {
      setProgress(100);
      pctText.textContent = "100%";
      statusText.textContent = "Complete";
      appendLog("HTML document generated successfully.");
      setTimeout(showResult, 700);
      source.close();

    } else if (data.status === "error") {
      setProgress(100);
      pctText.textContent = "✗";
      statusText.textContent = "Failed";
      appendLog("ERROR: " + (data.message || "unknown"));
      setTimeout(function () { showError(data.message); }, 500);
      source.close();
    }
  };

  source.onerror = function () {
    appendLog("EventSource error. Retrying...");
  };
});
```

- [ ] **Step 3: Commit**

Run:
```bash
git add crop_app/templates/extract_progress.html crop_app/static/js/extract_progress.js
git commit -m "feat: add progress page with SSE-driven gauge and action log ticker"
```

- [ ] **Step 4: End-to-end test in browser**

Start: `cd crop_app && python -m flask --app app run --debug --port 5000`

1. Upload a PDF. Analyze pages (creates classification).
2. Go to a Complex page. Draw several crop boxes around tables, features, footnotes.
3. Click **Commit All Crops**. Observe: "Extract to HTML" button becomes enabled.
4. Click **Extract to HTML**.
5. Progress page opens with animated gauge. Action log shows timestamps.
6. Extraction completes. Checkmark animates in. "Open HTML Document" button appears.
7. Click "Open HTML Document" — opens in a new tab.
8. Verify the output HTML:
   - Left sidebar TOC lists all pages
   - Each page is a sheet card with shadow
   - Tables render correctly with borders, headers, merges
   - Hover footnote `<sup>*</sup>` shows tooltip with footnote text
   - Click footnote `<sup>*</sup>` smooth-scrolls + flashes footnote row
   - Click ↩ back-link returns to footnote ref
   - Hover over a table reveals "Copy" button — copying yields clean HTML

- [ ] **Step 5: If all above looks good, commit final smoke test**

Run:
```bash
git status
# Nothing to commit if everything was committed incrementally.
# If any CSS tweaks or fixes were made:
git add -A
git commit -m "chore: post-integration cleanup"
```

---

## Self-Review Checklist (performed by plan author)

- [x] All spec requirements covered: page-by-page processing, table fidelity, no images, 100% data coverage, zero commentary, footnote preservation, render final merged HTML.
- [x] Button state: disabled until committed, re-disables on any draft change.
- [x] Output HTML includes TOC, sheet-card layout, interactive tables, smart footnotes, responsive.
- [x] No placeholders — all code provided in full.
- [x] TDD order verified for tasks 2 and 3.
- [x] Type consistency: `extract_crop_as_html(image, model)`, `run_extraction(session_id, sm, crop_root, model)`, `assemble_full_document(pages_data, title)` used consistently across tasks.
- [x] Cache stage name `"html_extract"` used consistently.
- [x] Commit-state button logic: `updateExtractButton(boxes)` called via `updateCropPanel`.
