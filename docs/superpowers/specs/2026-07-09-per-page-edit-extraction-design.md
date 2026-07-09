# Per-Page Editable HTML Extraction

## Overview

The extraction pipeline currently produces a single merged `extraction.html` per session. This spec changes that to a **per-page file structure** where each page's HTML is individually editable, auditable, and savable.

**Approach:** The LLM wraps extractable values in semantic `<span class="field" data-field="...">` tags at extraction time (Approach A). A browser UI makes these fields editable and provides a Save button.

---

## 1. Output File Structure

Each session's output directory (`crop_app/static/extracted/<session_id>/`) now contains:

```
├── index.html          # Landing page — page list + navigation
├── page-0.html         # Self-contained, inline CSS/JS, single page
├── page-1.html
├── page-2.html
└── ...
```

- Each `page-N.html` is fully self-contained (CSS and JS inlined). No external assets.
- `index.html` replaces the old single `extraction.html` as the browser entry point.
- Pages are navigable via prev/next buttons and a TOC in `index.html`.

The old `extraction.html` path returns `index.html` as a backward-compat fallback.

---

## 2. LLM Prompt Change

`table_extractor/prompts/html/_master.txt` gets one additional constraint appended after the existing Operational Constraints:

```
  8. Field Wrapping: Wrap every price, phone number, URL, email, date, address,
     vehicle model name, and feature-matrix symbol in
     <span class="field" data-field="PRICE|PHONE|URL|EMAIL|DATE|ADDRESS|MODEL|SYMBOL">…</span>.
     Feature-matrix symbols cover indicators for feature availability in a trim, such as
     checkmarks, dots, crosses, or similar glyphs used in comparison tables. Do not alter
     text, layout, or structure around the value. If a value is ambiguous between two
     field types, use data-field="TEXT".
```

### Field types

| `data-field` | Examples |
|---|---|
| `PRICE` | `₹ 5.99 Lakh`, `$29,999`, `Rs. 12,34,000` |
| `PHONE` | `+91 98765 43210`, `1800-123-456` |
| `URL` | `www.marutisuzuki.com`, `https://...` |
| `EMAIL` | `info@example.com` |
| `DATE` | `01 Jan 2026`, `2025-08-15` |
| `ADDRESS` | dealer addresses, showroom locations |
| `MODEL` | `WagonR`, `Vxi`, `ZXi Plus`, `Diesel` variants |
| `SYMBOL` | checkmarks, dots, crosses, or other glyphs in feature-matrix cells |
| `TEXT` | ambiguous values (fallback) |

This is purely additive — the existing extraction behavior (table reconstitution, footnote mapping, 100% coverage) is unchanged.

---

## 3. Backend: Extraction Pipeline Changes

### `html_extractor.py`

- Extraction and merging logic (per-crop LLM calls, footnote resolution, vertical concatenation) stays the same.
- The write phase changes: currently it calls `assemble_full_document` and writes a single `extraction.html`. It now calls a new `write_page_files(session_id, pages_data, title)` function.
- The task list building and `ThreadPoolExecutor` flow are untouched.

### `html_assembler.py`

New function `write_page_files(session_id, pages_data, title)`:

1. Creates `crop_app/static/extracted/<session_id>/` directory.
2. Renders each page using a new per-page template (same wrapper shell as `output_page.html`, but TOC is scoped to that one page with prev/next links, and the body contains only that page's `build_page_html` output).
3. Writes `page-N.html` for each page (0-indexed filenames).
4. Writes `index.html` using a new simple landing template showing a page list with links.

The existing `assemble_full_document` function and its templates (`output_page.html`, `output_page.css`, `output_page.js`) are preserved unchanged.

### New per-page templates

| Template | Role |
|---|---|
| `templates/output_page.html` | Per-page wrapper (same shell, different TOC/nav structure) |
| `templates/output_page.css` | Styles for per-page view, editable fields, save button |
| `templates/output_page.js` | Edit observability, save logic, page navigation |

The per-page template injects:

```html
<nav class="page-nav">
  <a href="page-{prev}.html" class="nav-btn">← Prev</a>
  <span class="page-indicator">Page {n} of {m}</span>
  <a href="page-{next}.html" class="nav-btn">Next →</a>
</nav>
```

### `app.py` route changes

- `GET /extract-html/<sid>` stays the same (triggers extraction via SSE).
- The SSE `done` event handler calls `write_page_files` instead of writing `extraction.html`.
- `GET /extracted/<sid>/extraction.html` → returns `index.html` (backward-compat).
- `GET /extracted/<sid>/page-<n>.html` → serves individual page files.
- `GET /extracted/<sid>/` → serves `index.html`.

Path-traversal guard stays unchanged.

---

## 4. New Backend Endpoint

```
POST /save-page/<session_id>/<page_idx>
```

**Request body:** full edited HTML string (text/html).

**Behavior:**

1. Validates session exists and `page_idx` is within bounds.
2. Writes to `crop_app/static/extracted/<session_id>/page-{page_idx}.html`, overwriting the original.
3. Returns `{ "status": "ok" }` on success, or `{ "status": "error", "message": "..." }` on failure.

**Failed-save handling:** The front-end keeps the save button visible and shows an inline error message. It does not retry automatically — the user must click again.

**No version history:** Single-user audit workflow. Last write wins. Re-running extraction regenerates all page files from scratch (edits are lost — this is expected).

---

## 5. Frontend: Editable Fields + Save Button

### Editability

- On page load, all `span.field` elements get `contenteditable="true"` automatically.
- Clicking a field highlights it with a subtle outline (`2px solid #4f8cff`).
- The user edits the text content directly in the rendered layout. Styling and structure are untouched because the editable layer is inside the existing span.

### Save button

A sticky button in the **bottom-right** corner (z-index above page content):

- **Hidden by default.**
- A `MutationObserver` watches all `.field` elements for text content changes.
- On first change from original value → button appears with text **"Save Changes"**.
- Clicking it sends `POST /save-page/<sid>/<page_idx>` with `document.body.innerHTML`.
- On success → button hides, brief success toast ("Saved") fades out after 2s, field highlights are cleared.
- On failure → button stays, error text appears below the button label.

### Edit feedback

- Edited `.field` elements get `class="field edited"` → CSS: `outline: 2px solid #4f8cff; outline-offset: 1px;`.
- On successful save, the `edited` class is removed and original text content is updated to the saved value (so further edits are detected accurately).

### Page navigation

`index.html` front-matter:

- Grid of page thumbnails (page label + page number).
- Each links to `page-N.html`.
- Active page is highlighted.

`page-N.html` footer:

- Prev/Next links with disabled state at boundaries (page 0 hides Prev; last page hides Next).

---

## 6. Success Criteria

| Criterion | Verification |
|---|---|
| Each page produces its own `page-N.html` file | Output directory contains `page-0.html` … `page-N.html` |
| Crops merge only within their parent page's file | Each file contains only that page's crop fragments |
| Editable values wrapped in `<span class="field" data-field="...">` | LLM output contains `span.field` with correct `data-field` attribute |
| Editing any field shows a sticky "Save Changes" button | `MutationObserver` triggers → button appears |
| Clicking save overwrites the original file | `POST /save-page` → file on disk is updated |
| `index.html` allows navigation between pages | Front-matter loads, links to `page-N.html` resolve correctly |

---

## 7. Non-Goals (Out of Scope)

- No structural HTML validation on save (raw HTML overwrite).
- No version history or diff view.
- No cross-page footnote resolution.
- No approach B or C exploration in this implementation (can be revisited if Approach A is unreliable).
