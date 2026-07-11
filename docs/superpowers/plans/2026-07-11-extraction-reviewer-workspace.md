# Extraction Reviewer Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a split-pane reviewer that compares each original brochure page to editable rendered extraction and persists corrections.

**Architecture:** A protected Flask reviewer route renders a full-height shell with page thumbnails, a reusable image canvas, and an iframe of the existing editable per-page HTML. The annotation image viewport is split into a shared image-viewer module; generated pages receive an embedded mode which hides standalone chrome while keeping their save endpoint and in-place editor intact.

**Tech Stack:** Flask, Jinja2, vanilla JavaScript, CSS, pytest.

## Global Constraints

- Preserve the current in-place rendered-content editing and `/save-page/<session_id>/<page_idx>` persistence contract.
- Reuse existing annotation fit, zoom, pan, controls, and resize behavior; review mode exposes no crop editing.
- Thumbnail selection changes source and extraction together and is represented by zero-based `?page=` in the URL.
- The extracted pane scrolls vertically only; paragraphs, headings, list items, and table cells wrap at all permitted divider widths.
- Standalone generated pages work unchanged when `embed` is absent.
- Do not add a raw HTML editor, automated discrepancy detection, or model/prompt changes.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `crop_app/app.py` | Reviewer access, completion gating, page validation, and extraction redirect. |
| `crop_app/templates/review.html` | Thumbnail strip, source canvas, divider, and extraction iframe. |
| `crop_app/static/css/review.css` | Full-height, resizable, responsive reviewer layout. |
| `crop_app/static/js/image_viewer.js` | Shared canvas load/fit/zoom/pan/resize API. |
| `crop_app/static/js/annotate.js` | Crop-specific behavior layered on the shared viewer. |
| `crop_app/static/js/review.js` | Page synchronization and accessible persisted divider. |
| `table_extractor/templates/output_page.css` | Embedded-page containment and wrapping. |
| `table_extractor/templates/output_page_edit.js` | `embed=1` detection while retaining editor/save logic. |
| `crop_app/tests/test_extract_routes.py` | Reviewer route and redirect coverage. |
| `crop_app/tests/test_templates.py` | Viewer and review shell contract coverage. |
| `table_extractor/tests/test_html_assembler.py` | Generated embedded-page CSS coverage. |
| `table_extractor/tests/test_output_page_edit_js.py` | Embedded editor/save contract coverage. |

## Task 1: Add reviewer access and extraction redirect

**Files:**

- Modify: `crop_app/app.py`
- Modify: `crop_app/tests/test_extract_routes.py`

**Consumes:** `SessionManager.session_exists`, `_output_complete`, and existing `/pages` plus `/extracted` routes.

**Produces:** `GET /review/<session_id>?page=<int>` returns `review.html` only for an existing session with a complete extraction; completed extraction opens page zero in reviewer mode.

- [ ] **Step 1: Add the failing route tests**

```python
def _mark_extraction_complete(app, sid):
    output = os.path.join(app.config["EXTRACTED_DIR"], sid)
    os.makedirs(output, exist_ok=True)
    open(os.path.join(output, ".complete"), "w", encoding="utf-8").close()

def test_review_workspace_requires_completed_extraction(client_ready_session):
    client, sid, _ = client_ready_session
    assert client.get(f"/review/{sid}").status_code == 404

def test_review_workspace_renders_current_page(client_ready_session):
    client, sid, _ = client_ready_session
    _mark_extraction_complete(client.application, sid)
    response = client.get(f"/review/{sid}?page=0")
    assert response.status_code == 200
    assert b"review-canvas" in response.data
    assert b'"initialPage": 0' in response.data

def test_review_workspace_rejects_out_of_range_page(client_ready_session):
    client, sid, _ = client_ready_session
    _mark_extraction_complete(client.application, sid)
    assert client.get(f"/review/{sid}?page=1").status_code == 404
```

- [ ] **Step 2: Verify the tests fail**

Run: `python3 -m pytest crop_app/tests/test_extract_routes.py -k review -v`

Expected: FAIL because `/review/<session_id>` does not exist.

- [ ] **Step 3: Implement the route and redirect**

```python
@app.route("/review/<session_id>", methods=["GET"])
def review_workspace(session_id):
    _sm = app.session_manager
    if not _sm.session_exists(session_id) or not _output_complete(session_id):
        return "Extraction not found. Please run extraction first.", 404
    meta = _sm.load_meta(session_id)
    page_idx = request.args.get("page", 0, type=int)
    pages = meta.get("pages", [])
    if page_idx is None or page_idx < 0 or page_idx >= len(pages):
        return "Page not found", 404
    return render_template("review.html", session_id=session_id,
                           pages=[{"path": page["path"]} for page in pages],
                           initial_page=page_idx)
```

In `extract_html_page`, replace the completed-output redirect with:

```python
return redirect(f"/review/{session_id}?page=0")
```

- [ ] **Step 4: Verify the tests pass**

Run: `python3 -m pytest crop_app/tests/test_extract_routes.py -k review -v`

Expected: PASS.

- [ ] **Step 5: Commit the task**

```bash
git add crop_app/app.py crop_app/tests/test_extract_routes.py
git commit -m "feat: add extraction reviewer route"
```

## Task 2: Extract the annotation canvas into a shared image viewer

**Files:**

- Create: `crop_app/static/js/image_viewer.js`
- Modify: `crop_app/static/js/annotate.js`
- Modify: `crop_app/templates/annotate.html`
- Modify: `crop_app/tests/test_templates.py`

**Consumes:** the current annotation `ZOOM_LEVELS`, `resizeCanvas`, `fitToViewport`, `zoomTo`, wheel, middle/space drag, and `ResizeObserver` behavior.

**Produces:** `window.ImageViewer.create(options)`, returning `{ state, setImage(url), resize({refit}), destroy() }`; annotation supplies crop drawing callbacks while review mode has no crop behavior.

- [ ] **Step 1: Add failing shared-viewer contract tests**

```python
def test_annotation_loads_shared_image_viewer():
    template = open("crop_app/templates/annotate.html", encoding="utf-8").read()
    assert '/static/js/image_viewer.js' in template

def test_shared_image_viewer_supports_review_mode():
    script = open("crop_app/static/js/image_viewer.js", encoding="utf-8").read()
    assert "ImageViewer.create" in script
    assert 'mode === "review"' in script
    assert "ResizeObserver" in script
```

- [ ] **Step 2: Verify failure**

Run: `python3 -m pytest crop_app/tests/test_templates.py -k image_viewer -v`

Expected: FAIL because the shared module does not exist.

- [ ] **Step 3: Move viewport behavior into the module**

```javascript
window.ImageViewer = window.ImageViewer || {};
window.ImageViewer.create = function createImageViewer(options) {
  const state = { image: null, imgW: 0, imgH: 0, canvasW: 0, canvasH: 0,
    zoom: 1, panX: 0, panY: 0, isPanning: false, panStartX: 0,
    panStartY: 0, panOriginX: 0, panOriginY: 0, spaceHeld: false };
  function resize({ refit = false } = {}) {
    resizeCanvasForDevicePixelRatio();
    if (refit && state.image) fitToViewport();
    render();
  }
  function setImage(url) {
    const image = new Image();
    image.onload = function () { state.image = image; resize({ refit: true }); };
    image.src = url;
  }
  new ResizeObserver(() => resize()).observe(options.container);
  setImage(options.imageUrl);
  return { state, setImage, resize, destroy() {} };
};
```

Copy these named functions from `annotate.js` into `image_viewer.js` without changing their coordinate formulas: `getCanvasPos`, `resizeCanvas`, `fitToViewport`, `render`, `updateZoomDisplay`, `getZoomIndex`, `zoomTo`, `zoomIn`, `zoomOut`, and `onWheel`. Copy the middle-button, space+left-drag, and out-of-image panning branches from `onMouseDown`, plus the `state.isPanning` branch from `onMouseMove` and `onMouseUp`. Register those only in the shared module. Preserve `ZOOM_LEVELS`, `PADDING`, `DPR`, and `ZOOM_SPEED` values exactly. `onWheel` must retain this rule: Ctrl-wheel calls `zoomTo` around the cursor; ordinary wheel changes `panX` by `-deltaX` and `panY` by `-deltaY`.

Keep `getCursorZone`, crop drawing, crop interaction, commits, trimming, and draft persistence in `annotate.js`. After the shared panning handler returns `false`, annotation's existing left-button handling runs. In review mode, `onPointerDown` always starts panning for left, middle, and space+left drag and never calls any crop callback.

- [ ] **Step 4: Load the module before the annotation script**

```html
<script src="/static/js/image_viewer.js"></script>
<script src="/static/js/annotate.js"></script>
```

- [ ] **Step 5: Verify annotation regressions pass**

Run: `python3 -m pytest crop_app/tests/test_templates.py crop_app/tests/test_crop_routes.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the task**

```bash
git add crop_app/static/js/image_viewer.js crop_app/static/js/annotate.js crop_app/templates/annotate.html crop_app/tests/test_templates.py
git commit -m "refactor: share annotation image viewer"
```

## Task 3: Create the full-height synchronized reviewer workspace

**Files:**

- Create: `crop_app/templates/review.html`
- Create: `crop_app/static/css/review.css`
- Create: `crop_app/static/js/review.js`
- Modify: `crop_app/tests/test_templates.py`

**Consumes:** `window.REVIEW_DATA`, `ImageViewer.create`, `/pages/<session>/<path>`, and `/extracted/<session>/page-<index>.html?embed=1`.

**Produces:** Full-height panels, a thumbnail strip, read-only image viewer, accessible divider, and iframe page selection synchronized to the URL.

- [ ] **Step 1: Add the failing review-shell test**

```python
def test_review_template_has_viewer_divider_and_editable_iframe():
    template = open("crop_app/templates/review.html", encoding="utf-8").read()
    assert 'id="review-canvas"' in template
    assert 'id="review-divider"' in template
    assert 'id="extracted-frame"' in template
    assert '/static/js/image_viewer.js' in template
    assert '/static/js/review.js' in template
```

- [ ] **Step 2: Verify failure**

Run: `python3 -m pytest crop_app/tests/test_templates.py -k review_template -v`

Expected: FAIL because `review.html` is absent.

- [ ] **Step 3: Create the template and layout**

```html
<main class="review-workspace">
  <nav id="review-thumbnails" aria-label="Brochure pages"></nav>
  <div id="review-split" class="review-split">
    <section class="review-source"><div class="review-toolbar"><strong id="review-source-label">Source</strong><div class="zoom-controls"><button id="review-zoom-out" type="button" aria-label="Zoom out">−</button><span id="review-zoom-level">100%</span><button id="review-zoom-in" type="button" aria-label="Zoom in">+</button><button id="review-reset" type="button">Reset</button></div></div><div id="review-canvas-container"><canvas id="review-canvas"></canvas></div></section>
    <div id="review-divider" role="separator" tabindex="0" aria-label="Resize source and extraction panels" aria-orientation="vertical" aria-valuemin="25" aria-valuemax="75" aria-valuenow="50"></div>
    <section class="review-extraction"><div class="review-toolbar">Extracted HTML</div><iframe id="extracted-frame" title="Editable extracted HTML"></iframe></section>
  </div>
</main>
<script>window.REVIEW_DATA = {{ {"sessionId": session_id, "pages": pages, "initialPage": initial_page}|tojson }};</script>
<script src="/static/js/image_viewer.js"></script><script src="/static/js/review.js"></script>
```

```css
.review-body { height:100vh; overflow:hidden; display:flex; flex-direction:column; }
.review-workspace { min-height:0; flex:1; display:flex; flex-direction:column; padding:12px 16px; }
.review-split { min-height:0; flex:1; display:grid; grid-template-columns:minmax(260px,var(--source-width,50%)) 12px minmax(260px,1fr); }
.review-source,.review-extraction { min-width:0; min-height:0; display:flex; flex-direction:column; overflow:hidden; }
#extracted-frame { width:100%; min-width:0; flex:1; border:0; }
#review-divider { touch-action:none; cursor:col-resize; }
@media (max-width:700px) { .review-body { height:auto; overflow:auto; } .review-split { display:flex; flex-direction:column; } #review-divider { display:none; } .review-source,.review-extraction { height:70vh; min-height:420px; } }
```

- [ ] **Step 4: Implement page selection and divider controls**

```javascript
function selectPage(index, push = true) {
  viewer.setImage(`/pages/${encodeURIComponent(sessionId)}/${pages[index].path}`);
  frame.src = `/extracted/${encodeURIComponent(sessionId)}/page-${index}.html?embed=1`;
  if (push) history.pushState({ page: index }, "", `?page=${index}`);
}
function setSplit(percent) {
  const value = Math.max(25, Math.min(75, percent));
  split.style.setProperty("--source-width", `${value}%`);
  divider.setAttribute("aria-valuenow", String(Math.round(value)));
  localStorage.setItem("extraction-review-split", String(value));
  viewer.resize({ refit: false });
}
```

Use pointer capture for divider dragging. On `ArrowLeft`/`ArrowRight`, read `Number(divider.getAttribute("aria-valuenow"))` and call `setSplit(value - 5)`/`setSplit(value + 5)`. Render thumbnail buttons with the selected page marked `aria-current="page"`; on `popstate`, read `new URLSearchParams(location.search).get("page")`, convert with `Number`, validate `0 <= index < pages.length`, then call `selectPage(index, false)`.

- [ ] **Step 5: Verify the route and template tests pass**

Run: `python3 -m pytest crop_app/tests/test_extract_routes.py crop_app/tests/test_templates.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the task**

```bash
git add crop_app/templates/review.html crop_app/static/css/review.css crop_app/static/js/review.js crop_app/tests/test_templates.py
git commit -m "feat: add split extraction reviewer workspace"
```

## Task 4: Make generated pages iframe-safe without changing saving

**Files:**

- Modify: `table_extractor/templates/output_page.css`
- Modify: `table_extractor/templates/output_page_edit.js`
- Modify: `table_extractor/tests/test_html_assembler.py`
- Modify: `table_extractor/tests/test_output_page_edit_js.py`

**Consumes:** Existing per-page HTML, `.page-nav`, `.document-canvas`, and `getSaveUrl()`.

**Produces:** Pages recognize exactly `?embed=1`; embedded pages hide standalone navigation and reflow inside the iframe while keeping the existing editor and save URL.

- [ ] **Step 1: Write failing tests**

```python
def test_per_page_html_includes_embedded_mode_and_wrapping_rules():
    with tempfile.TemporaryDirectory() as tmp:
        write_page_files("sid", [{"html": "<p>longword</p>"}], "Title", output_root=tmp)
        page = open(os.path.join(tmp, "sid", "page-0.html"), encoding="utf-8").read()
    assert ".embedded-review .page-nav" in page
    assert "overflow-wrap: anywhere" in page
    assert "word-break: break-word" in page
    assert "table-layout: fixed" in page

def test_edit_script_detects_embedded_mode_without_changing_save_path():
    js = open(js_path, encoding="utf-8").read()
    assert 'searchParams.get("embed") === "1"' in js
    assert 'classList.add("embedded-review")' in js
    assert '"/save-page/"' in js
```

- [ ] **Step 2: Verify failure**

Run: `python3 -m pytest table_extractor/tests/test_html_assembler.py table_extractor/tests/test_output_page_edit_js.py -v`

Expected: FAIL because embedded mode does not exist.

- [ ] **Step 3: Append these exact CSS rules to `output_page.css`**

```css
.embedded-review .page-nav { display: none; }
.embedded-review .document-canvas { width: auto; margin-left: 0; padding: 16px; overflow: visible; }
.embedded-review .page { width: 100%; max-width: none; margin: 0; padding: 0; box-shadow: none; border-radius: 0; }
.embedded-review .page-label { display: none; }
.page, .page * { min-width: 0; }
p, li, dd, dt, h1, h2, h3, h4, h5, h6, th, td { overflow-wrap: anywhere; word-break: break-word; }
.table-scroll-wrap { max-width: 100%; }
table { table-layout: fixed; }
```

Do not add `overflow: hidden` to `body`, `.document-canvas`, or `.page`.

- [ ] **Step 4: Add these statements as the first statements inside the current `DOMContentLoaded` callback**

```javascript
var searchParams = new URLSearchParams(window.location.search);
if (searchParams.get("embed") === "1") {
  document.documentElement.classList.add("embedded-review");
  document.body.classList.add("embedded-review");
}
```

Do not modify `getSaveUrl()`: it must use `window.location.pathname`, so `?embed=1` cannot alter the `/save-page/<session_id>/<page_idx>` request.

- [ ] **Step 5: Verify pass**

Run: `python3 -m pytest table_extractor/tests/test_html_assembler.py table_extractor/tests/test_output_page_edit_js.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the task**

Run: `git add table_extractor/templates/output_page.css table_extractor/templates/output_page_edit.js table_extractor/tests/test_html_assembler.py table_extractor/tests/test_output_page_edit_js.py && git commit -m "feat: embed editable extraction pages in reviewer"`

## Task 5: Verify the complete workflow and repair only observed defects

**Files:**

- Modify only files from Tasks 1–4 when a check below exposes a specific defect.

**Consumes:** The complete reviewer workspace.

**Produces:** Evidence that reviewer interactions, save behavior, responsive layout, and annotation regressions have been checked.

- [ ] **Step 1: Run the full suite**

Run: `python3 -m pytest crop_app/tests table_extractor/tests -v`

Expected: PASS. If `table_extractor/tests/test_snap.py::test_get_normalized_ocr_boxes` alone fails with `No module named 'numpy'`, install the declared `rapidocr-onnxruntime` dependency in the active environment and rerun the exact command. Do not skip that test or report the suite as passing before it passes.

- [ ] **Step 2: Start Flask for manual verification**

Run: `python3 crop_app/app.py`

Expected: App starts without import errors and remains running for Steps 3–12.

- [ ] **Step 3: Verify completion routing**

Complete an extraction containing at least two pages. Expected URL: `/review/<session_id>?page=0`. Failure if redirected to `/extracted/<session_id>/extraction.html`.

- [ ] **Step 4: Verify synchronized page selection**

Click page 2 then page 1. Each click must change the source image and iframe to the same zero-based index; the active thumbnail must have `aria-current="page"`. Use browser Back then Forward; both panes must return to the matching page.

- [ ] **Step 5: Verify left viewer behavior**

Use +, −, Ctrl+mouse-wheel/trackpad pinch, and space+left-drag. Each must change only the source image viewport. Attempting normal left-drag must not create a crop rectangle in review mode.

- [ ] **Step 6: Verify pointer divider bounds and reflow**

Drag the divider fully in both directions. It must stop at exactly 25% and 75% source-pane width. At both limits, source remains visible, iframe remains loaded, and all text/table cells in the iframe are readable without a horizontal page scrollbar.

- [ ] **Step 7: Verify keyboard divider behavior and persistence**

Tab to the divider. ArrowLeft decreases `aria-valuenow` by exactly 5 and ArrowRight increases it by exactly 5, subject to the 25–75 limits. Reload: the divider position must restore from `localStorage` key `extraction-review-split`.

- [ ] **Step 8: Verify successful edit persistence**

Edit one paragraph and one table cell in the iframe. Both must show the existing edited highlight. Click Save Changes. Expected: success notification, no edited highlights, and changed values still present after iframe reload.

- [ ] **Step 9: Verify failed save retention**

In a test environment, make `/save-page/<session_id>/<page_idx>` return HTTP 500. Edit a value and save. Expected: value and highlight remain present, and an error is visible. Failure if the page reverts or discards the unsaved value.

- [ ] **Step 10: Verify narrow-screen layout**

At a viewport width below 700 CSS pixels, source and extraction must stack vertically, the divider must be hidden, and each pane must be at least 420 CSS pixels high.

- [ ] **Step 11: Verify annotation regression**

Open `/annotate/<session_id>?page=0`; draw, move, resize, delete, and commit a crop. All actions must work exactly as before, including draft persistence and extraction navigation.

- [ ] **Step 12: Commit only a concrete repair**

If one of Steps 3–11 found a defect and it has been fixed, run: `git add crop_app table_extractor && git commit -m "fix: polish extraction reviewer workflow"`. If no defect is found, do not create an empty commit.
