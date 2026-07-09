# Per-Page Editable HTML Extraction

## Overview

The extraction pipeline currently produces a single merged `extraction.html` per session. This spec changes that to a **per-page file structure** where each page's HTML is individually editable, auditable, and savable.

**Approach:** The LLM output is unchanged. A frontend script identifies text-bearing DOM elements (table cells, list items, headings, paragraphs) and makes them editable inline (Approach C). The user edits values directly in the rendered layout; a Save button persists changes.

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

## 2. LLM Prompt / Schema Changes

The existing `_master.txt` prompt structure is **mostly unchanged** — the LLM continues to output raw HTML fragments. Two targeted extraction-side changes are included:

- The page classification schema now includes a `"plan"` view enum value.
- Technical-drawing prompts require contextual labels for numeric measurements (e.g. `"Dimension: 120 mm"` instead of bare `"120 mm"`).

These changes keep extraction quality stable while improving downstream readability. No field wrapping or broad semantic markup injection is introduced.

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
| `templates/output_page.css` | Styles for per-page view, editable regions, save button |
| `templates/output_page.js` | Edit injection, edit observability, save logic, page navigation |

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

## 5. Frontend: Editable DOM + Save Button

### Edit injection

On page load, JavaScript scans the page content for text-bearing elements and replaces their text nodes with editable inputs. The selector targets:

| Element | Why editable |
|---|---|
| `td`, `th` | Table cells — symbols, specs, model names, prices |
| `li` | List items — feature names, descriptions |
| `h1`–`h6` | Headings — page titles, section headings |
| `p`, `dd`, `dt` | Paragraphs and definition terms — body text, stat card values |
| `span.field` (if any appear from future prompt changes) | Explicitly-wrapped values — preserved as-is |

For each matched element:
- If it contains only a single text node, that text node is replaced with an `<input type="text">` styled to match the original text appearance (font size, color, weight, background transparent, border none until focus).
- If it contains mixed content (text + child elements like `<sup>`, `<a>`, `<br>`), the element gets `contenteditable="true"` instead — preserving the child structure while allowing inline editing.
- The original text content is stored in a `data-original` attribute for change detection.

### Edit feedback

- Edited elements get `class="edited"` → CSS: `outline: 2px solid #4f8cff; outline-offset: 1px;`.
- The input/editable region is visually seamless: default state is indistinguishable from static text. On focus, a subtle border appears. On edit, the blue outline marks it as dirty.

### Save button

A sticky button in the **bottom-right** corner (z-index above page content):

- **Hidden by default.**
- A `MutationObserver` watches all editable regions for `input` / `DOMSubtreeModified` events. Any change from `data-original` → button appears with text **"Save Changes"**.
- Clicking it sends `POST /save-page/<sid>/<page_idx>` with the full serialized document (`<!DOCTYPE html>` + `document.documentElement.outerHTML`), after stripping injected editing UI (save button, toast, error spans), converting inline `<input>` values back to text nodes, and removing `contenteditable` attributes and `.edited` / `.inline-edit-input` classes.
- On success → button hides, brief success toast ("Saved") fades out after 2s, `edited` classes are removed, `data-original` values are updated to the saved content.
- On failure → button stays, error text appears below the button label.

### Page navigation

`index.html` front-matter:

- Grid of page thumbnails (page label + page number).
- Each links to `page-N.html`.
- Active page is highlighted.

`page-N.html` footer:

- Prev/Next links with disabled state at boundaries (page 0 hides Prev; last page hides Next).

---

## 6. Preserved Behavior

- Existing extraction behavior (table reconstitution, footnote mapping, 100% coverage) is unchanged.
- All existing CSS styling is preserved. Editable regions inherit existing font/color/spacing — no layout shift.
- The existing `assemble_full_document` function and its templates are preserved for any future use.
- Existing `output_page.js` footnote interactivity, table Copy buttons, and TOC active-state tracking are all preserved.

---

## 7. Success Criteria

| Criterion | Verification |
|---|---|
| Each page produces its own `page-N.html` file | Output directory contains `page-0.html` … `page-N.html` |
| Crops merge only within their parent page's file | Each file contains only that page's crop fragments |
| Table cells, list items, headings, paragraphs are editable in the browser | Frontend script injects inputs/contenteditable into these elements |
| Editing any region shows a sticky "Save Changes" button | MutationObserver triggers → button appears |
| Clicking save overwrites the original file | `POST /save-page` → file on disk is updated |
| `index.html` allows navigation between pages | Front-matter loads, links to `page-N.html` resolve correctly |

---

## 8. Non-Goals (Out of Scope)

- No semantic field classification (price vs feature vs spec — the audit UI treats all values uniformly).
- No structural HTML validation on save (raw HTML overwrite).
- No version history or diff view.
- No cross-page footnote resolution.
