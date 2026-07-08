# HTML Extraction Pipeline — Design Spec

## Overview

This spec defines a pipeline that takes annotated crops (or full pages for Simple-page sessions) and produces a single, flat HTML file. Each page is processed separately and appended in order. Tables preserve their layout including colspan, rowspan, section dividers, and symbols. No images are rendered. No commentary is added. Every detail in the source PDF/images is fully captured. Footnote symbols are matched with footnote text.

## Architecture

### Approach: Per-Region HTML Extraction (Approach 1)

Each crop (or full page for Simple-classified pages) is sent to a vision LLM with a unified HTML extraction prompt (master prompt + all type hints) that asks it to produce semantic HTML fragments directly. The fragments are assembled page-by-page into a flat HTML document.

### Component Structure

| Component | Location | Purpose |
|---|---|---|
| `html_extractor.py` | `table_extractor/` | Core orchestration: iterate pages/crops → LLM → assemble HTML |
| `html_assembler.py` | `table_extractor/` | Assemble page fragments, handle footnotes, produce final HTML |
| `prompts/html/` | `table_extractor/prompts/html/` | HTML extraction prompts (master + type hints) |
| Flask route | `crop_app/app.py` (new routes) | Trigger extraction, SSE progress, serve output |
| `extract_progress.html` | `crop_app/templates/` | Progress page shown during extraction; shows gauge, log, and result actions |
| `extract_progress.js` | `crop_app/static/js/` | Frontend SSE listener, progress gauge, and log rendering |

### Data Flow

```
[User clicks "Extract to HTML"]
        |
[Flask route: /extract-html/<session_id>]
        |
[html_extractor.py processes session]
  - For each page in meta.json:
    - If Simple: send full page image -> LLM -> HTML
    - If Complex: for each committed crop:
      - Load crop image
      - Send to LLM with unified HTML extraction prompt -> HTML fragment
      - Validate HTML fragment (basic sanity check)
  - Assemble page HTML fragments
  - Resolve footnote markers
  - Add page dividers and CSS styling
        |
[Save to static/extracted/<session_id>/extraction.html]
        |
[Open in new browser tab]
```

### Parallel Paths

HTML extraction is a parallel path to the existing Markdown pipeline. Both live in `table_extractor/` but share no state:

```
Existing:  crops -> detect -> extract (JSON) -> reconcile -> extraction.md
New:       crops -> html_extract (HTML per crop) -> assemble -> extraction.html
```

## Prompt Strategy

### Master Prompt

All extraction calls share a base master prompt stored in `table_extractor/prompts/html/_master.txt`. Constraints:

1. **Page-by-page processing**: Output is a single flat HTML string (handled externally by per-crop workflow).
2. **Table reconstitution**: Valid HTML (`<table>`, `<tr>`, `<td>`, `<th>`). Preserve original layout, header styling, row/column merges (rowspan, colspan), symbols, alignment, and text.
3. **No image rendering**: No `<img>` tags or placeholders. Extract text/data/labels embedded within images, grids, or diagrams.
4. **100% data coverage**: Do not summarize, truncate, or omit any text. Every word, number, and symbol captured.
5. **Zero commentary**: Output only raw HTML code. No markdown fences, no explanation. No added information or interpretation.
6. **Footnote mapping**: Preserve footnote symbols (e.g., `*`, `†`, `1`) in `<sup>` tags and match them to corresponding footnote text.

### Region Type Hints

Each region type has a lightweight hint file in `table_extractor/prompts/html/`:

| Region Type | Hint File | Guidance |
|---|---|---|
| `ruled_table` | `extract_ruled_table.txt` | Focus on accurate row/col alignment. Preserve all row spans, column spans, section divider rows, and cell content exactly. |
| `section_grouped_table` | `extract_section_grouped_table.txt` | Grouped column headers share cells via colspan. Section dividers use full-width rows. Preserve all merges. |
| `bullet_panel` | `extract_bullet_panel.txt` | Render as `<ul>` with nested `<ul>` for sub-tiers. Preserve bullet symbols. |
| `swatch_grid` | `extract_swatch_grid.txt` | Each row is a trim/color. Preserve column alignment and exact color names. |
| `stat_cards` | `extract_stat_cards.txt` | Render each as `<dl><dt>...<dd>...` pair. |
| `technical_drawing` | `extract_technical_drawing.txt` | Extract all visible labels and corresponding measurement values as `<dl>` key-value pairs. |
| `icon_badge` | `extract_icon_badge.txt` | Extract as `<strong>name</strong>: value`. |
| `footnote_block` | `extract_footnote_block.txt` | Each entry starts with a marker. Wrap each in `<p><sup>marker</sup> text</p>`. |
| `section_heading` | `extract_section_heading.txt` | Use appropriate heading level based on font size. |
| `other` | `extract_other.txt` | Best-effort extraction as paragraphs and semantic elements. |

Note: Crops do not have a stored `region_type` — the user draws bounding boxes without classifying content. The LLM infers the content type from the image itself. All type hints are loaded into the extraction prompt so the LLM has guidance for every content type it may encounter.

For Simple pages (full page, no crops), only the master prompt is used.

### Call Structure

```python
def extract_crop_as_html(crop_image, model):
    system = load_full_prompt()  # master + all type hints concatenated
    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "image_url", ...}]}
        ],
    )
    html_fragment = clean_up(response.choices[0].message.content)
    return html_fragment

def load_full_prompt():
    master = load_prompt("prompts/html/_master.txt")
    hints = [load_prompt(f"prompts/html/{f}") for f in sorted_type_hint_files()]
    return master + "\n\n" + "\n\n".join(hints)
```

No JSON schemas. No tool calling. No pre-classification. The LLM receives guidance for all content types and infers the type from the image. Direct HTML output.

### Output Cleanup

Post-processing strips: Markdown fences (```` ```html ... ``` ````), any non-HTML prefix/suffix text. Ensures result is a valid HTML fragment or empty string if malformed.

## HTML Assembly & Footnote Resolution

### Page Structure

```html
<!-- Page container -->
<div class="page" id="page-1">
  <div class="page-label">Page 1</div>
  
  <div class="region region-1">
    <!-- region_type: section_heading -->
    <h2>Features & Specifications</h2>
  </div>
  
  <div class="region region-2">
    <!-- region_type: ruled_table -->
    <table>...</table>
  </div>
  
  <div class="region region-3">
    <!-- region_type: footnote_block -->
    <aside class="footnotes">...</aside>
  </div>
</div>

<hr class="page-divider">

<div class="page" id="page-2">
  ...
</div>
```

### Assembly Logic (`html_assembler.py`)

1. Iterate `meta.json` pages in order.
2. For **Simple** pages: the single HTML fragment is the page content.
3. For **Complex** pages: sort crops by `bbox[1]` (y0, top coordinate) for top-to-bottom order, then concatenate fragments.
4. Wrap each page in `<div class="page" id="page-N">` with a `<div class="page-label">Page N</div>` header.
5. Insert `<hr class="page-divider">` between pages.

### Footnote Resolution

Two-phase process:

*Phase 1 — Collection:* After all region fragments for a page are assembled, scan all HTML for:
- Superscript markers: `<sup>*</sup>`, `<sup>**</sup>`, `<sup>†</sup>`, `<sup>1</sup>`, etc.
- The `footnote_block` region content — identified by a `class="footnotes"` attribute or similar (the assembler searches for common wrappers the LLM may produce: `<aside>`, `<div>`, `<ol>`, etc.).

*Phase 2 — Linking:* If a `footnote_block` exists on the page:
- Parse each footnote entry and extract its leading marker.
- For each `<sup>marker</sup>` found in other regions on the same page, wrap them as interactive references: `<a href="#fn-X" class="footnote-ref" data-footnote="marker text"><sup>marker</sup></a>`. The `data-footnote` attribute stores the footnote text so the client-side JS can show it in a hover tooltip without DOM lookups.
- In the footnote block, wrap each entry with an `id` and a back-link: `<p id="fn-X"><sup>marker</sup> Footnote text… <a href="#src-fn-X" class="footnote-back">↩</a></p>`.
- Duplicate footnote reference links on the same page point to the same anchor target cleanly.
- If no `footnote_block` exists but footnotes are inline (e.g., `*Note: ...` within a table), they remain as-is — markers are already preserved via `<sup>` tags.

*Phase 3 — Client-Side Interactivity (embedded JS):*
- **Hover**: On hover over `.footnote-ref`, show a positioned tooltip with the text from `data-footnote`.
- **Click**: On click, smooth-scroll to `#fn-X` and trigger a CSS `@keyframes flash` animation on the target (background `#fef9c3` → transparent over 1s). The tooltip is suppressed during/after click.
- **Back-link**: Click on `.footnote-back` scrolls back to the calling `.footnote-ref` anchor and briefly flashes it.

**Footnote Resolution Scope:**
Footnote resolution operates within a single page only. Cross-page footnotes are out of scope.

## Flask Integration & User Workflow

### Button Placement

An "Extract to HTML" button is added to the annotation page (`annotate.html`), positioned next to the existing "Commit All Crops" button.

### Commit-State Button Logic

The button state is driven by a commit state tracker:

```
Button ENABLED  <->  crops.length > 0  AND  draft.length === 0
Button DISABLED <->  NO crops committed  OR  draft.length > 0 (uncommitted changes)
```

| User Action | crops | draft | Button State |
|---|---|---|---|
| Initial (no crops) | 0 | 0 | Disabled |
| Draw new crop box | 0 | 1+ | Disabled |
| Click "Commit All Crops" | N | 0 | Enabled |
| Draw new box (after commit) | N | 1+ | Disabled |
| Move existing crop | N | 1+ | Disabled |
| Resize existing crop | N | 1+ | Disabled |
| Delete a crop | N-1 or N | 1+ | Disabled |
| Click "Commit All Crops" again | N | 0 | Enabled |

### Implementation

**Frontend (`annotate.js`):**
- After every crop operation (draw/move/resize/delete), the draft is saved via `/save-draft/<session_id>` and button is disabled.
- After `POST /commit/<session_id>` succeeds, reload `meta.json` and button is enabled.
- Button has a `disabled` attribute and visual cue (greyed out, `cursor: not-allowed`).

**Tooltips (disabled state):**
- Uncommitted changes: "Commit your changes to enable HTML extraction."
- No crops: "Commit at least one crop region first."

**Backend safety check:**
- `/extract-html/<session_id>` route verifies `draft.length === 0` before proceeding. If not, returns an error: "You have uncommitted changes. Please commit them before extracting HTML."

### Three-Step Extraction UX

**Step 1 — Extraction Page** (`templates/extract_progress.html`):
- Browser navigates to `/extract-html/<session_id>`.
- Page shows session name, page count, and crop count.
- Real-time progress via Server-Sent Events (SSE): animated progress gauge/bar transitions smoothly.
- **Live action log ticker** below the progress bar — timestamped lines scroll in as SSE events arrive:
  ```
  [11:02:14] Loading crop segments for Page 3...
  [11:02:15] Extracting ruled_table on Page 3 (Crop 2 of 4)...
  ```
- On completion, auto-redirects to the result page.

**Step 2 — Result Page** (`templates/extract_progress.html`):
- Animated success checkmark on completion.
- **Extraction Metrics Card**: processing time, page count, crops processed, errors (if any).
- Primary CTA: "Open HTML Document" (opens `/static/extracted/<session_id>/extraction.html` in a **new browser tab**, prominent brand color button).
- Secondary actions: "Back to Annotations", "Download HTML".

**Step 3 — HTML Output** (`static/extracted/<session_id>/extraction.html`):
- Self-contained HTML file with embedded CSS/JS.
- Opens in a new browser tab.
- User can save/save-as from the browser.
- Generated markup is sanitized and scripts are emitted only from the trusted application templates, so arbitrary model-generated script cannot execute when the file is opened in the browser.

### Progress Updates via SSE

Flask uses SSE (`text/event-stream`) to push page-level progress to the progress page. Each event: `{page: 2, totalPages: 8, crop: 3, totalCrops: 4, status: "extracting"|"done"|"error"}`.

### Error Handling

- If a single crop extraction fails: log error, insert `<div class="error">Extraction failed for this region</div>`, continue processing.
- Page-level failures: isolated with `<div class="error">Page extraction failed</div>`.
- Error summary shown on the result page before opening the HTML.

### CSS Styling (embedded in output HTML)

The output HTML is a premium, interactive document reader. All styling is embedded in the HTML file.

**Document-Viewer Layout:**
- Each page renders as a "sheet": white background (`#fff`), rounded corners (`border-radius: 8px`), subtle card shadow, centered on a light-grey canvas background (`#f1f5f9`).
- Fixed/sticky **Sidebar Table of Contents (TOC)** on the left — lists all pages (e.g., `Page 1`, `Page 2`, ... `Page N`) with click-to-scroll navigation via anchor links.
- Two-column layout: TOC (left, ~200px, collapsible on small screens) + document canvas (right, scrollable).

**Typography:**
- Inter font stack via `font-family: 'Inter', system-ui, -apple-system, sans-serif`.
- Body: ~14px, line-height 1.6.
- Table font: 13px. Footnotes: 12px.

**Interactive Tables:**
- `border-collapse`, single-pixel grey borders, header row slightly shaded (`#f5f5f5`).
- Row hover effect: `tr:hover { background-color: #f8fafc; }` for scanning wide tables.
- Floating "Copy Table" button in top-right corner of each table on hover — clicks copy the clean HTML table or CSV format to the clipboard via client-side JS.
- Responsive container: `overflow-x: auto` with custom-styled scrollbar so tables scroll horizontally instead of breaking layout on small viewports.
- Section divider rows styled distinctly (bold, shaded background).

**Smart Footnotes:**
- Superscript markers (`<sup>*</sup>`) in body text are clickable/hoverable.
- **Hover**: A small absolute-positioned tooltip appears with the corresponding footnote text.
- **Click**: Smooth-scrolls to the footnote block at the bottom of the page, and triggers a CSS animation that **flashes the footnote highlight** (background light yellow `#fef9c3` for 1 second via `@keyframes flash`).
- A back-link indicator next to the footnote text returns to the caller's position.

**Page Divider & Label:**
- Page divider: subtle light-grey dashed horizontal rule between sheets.
- Page label: small muted text (`Page 1 of N`) centered above each page sheet.

**Stat Card `<dl>`:** Grid layout with term/value pairs, each card in a subtle bordered box.

**Error Blocks:** Light red background (`#fef2f2`), left red border, warning text.

**Client-Side JS (embedded in output HTML):**
- Tooltip positioning for footnote hover.
- Copy-table button visibility toggle + clipboard logic.
- Flash animation trigger on footnote click.

### CSS Styling for Process Pages

These apply to the Flask app progress page (`extract_progress.html`), not the generated HTML.

**Progress Page (`extract_progress.html`):**
- Animated circular progress gauge or modern linear progress bar with smooth transitions.
- Live action log ticker below the progress bar:
  ```
  [11:02:14] Loading crop segments for Page 3...
  [11:02:15] Extracting ruled_table on Page 3 (Crop 2 of 4)...
  ```
- Timestamped log lines scroll in as SSE events arrive.
- Visual state: idle → extracting (pulsing progress) → complete (checkmark) or error (red X).

**Result Page (`extract_progress.html`):**
- Animated success checkmark on completion.
- **Extraction Metrics Card**: processing time, page count, crops processed, errors (if any).
- Primary CTA: "Open HTML Document" (opens in new tab, brand color button).
- Secondary actions: "Back to Annotations", "Download HTML".
- Button hierarchy: primary actions are visually prominent, secondary actions are subdued.

## Caching

Reuse existing cache mechanism from `table_extractor/cache.py`. Cache keys for `html_extract` include the SHA256 hash of the crop image, stage name (`html_extract`), model name, and a hash of the loaded prompt bundle (`load_full_prompt()`). This ensures prompt edits invalidate stale cached HTML. Cached HTML fragments are stored as plain text files in `.stage_cache/`. Re-running extraction on unchanged crops and prompts is near-instant.

## Model Configuration

- `qwen/qwen3.7-plus` — Generative vision LLM for HTML extraction (configured in `.env` as `DATA_EXTRACTION_MODEL_ID`).
- `openai/gpt-5-mini` — Classification only (configured as `PAGE_ANALYSIS_MODEL_ID`). Not used for extraction.

## New Files to Create

| File | Purpose |
|---|---|
| `table_extractor/html_extractor.py` | Core orchestration |
| `table_extractor/html_assembler.py` | Assemble fragments, footnotes, final HTML |
| `table_extractor/prompts/html/_master.txt` | Base master prompt |
| `table_extractor/prompts/html/extract_ruled_table.txt` | Type hint |
| `table_extractor/prompts/html/extract_section_grouped_table.txt` | Type hint |
| `table_extractor/prompts/html/extract_bullet_panel.txt` | Type hint |
| `table_extractor/prompts/html/extract_swatch_grid.txt` | Type hint |
| `table_extractor/prompts/html/extract_stat_cards.txt` | Type hint |
| `table_extractor/prompts/html/extract_technical_drawing.txt` | Type hint |
| `table_extractor/prompts/html/extract_icon_badge.txt` | Type hint |
| `table_extractor/prompts/html/extract_footnote_block.txt` | Type hint |
| `table_extractor/prompts/html/extract_section_heading.txt` | Type hint |
| `table_extractor/prompts/html/extract_other.txt` | Type hint |
| `crop_app/templates/extract_progress.html` | Progress page |
| `crop_app/static/js/extract_progress.js` | Frontend SSE + progress UI |
| `crop_app/static/extracted/` | Output directory (gitignored) |

## Existing Files to Modify

| File | Change |
|---|---|
| `crop_app/app.py` | Add `/extract-html/<session_id>`, `/extract-progress/<session_id>` (SSE), `/extracted/<session_id>` routes |
| `crop_app/templates/annotate.html` | Add "Extract to HTML" button |
| `crop_app/static/annotate.js` | Wire up commit-state button enable/disable logic |
