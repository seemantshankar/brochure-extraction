# Per-Page Editable HTML Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single merged `extraction.html` per session with per-page editable HTML files, an index landing page, and a save endpoint.

**Architecture:** Backend writes one `page-N.html` per page using a per-page template with inlined CSS/JS. Frontend JavaScript makes text-bearing DOM elements editable inline. A sticky Save button persists edits via a new POST endpoint.

**Tech Stack:** Python (Flask), vanilla JavaScript (no framework changes), existing Jinja2 templates

## Global Constraints

- Extraction prompt updates are limited to:
  - Adding the `"plan"` view enum value for classification.
  - Requiring contextual labels for numeric measurements in technical drawings.
- Existing `assemble_full_document` and its templates are preserved unchanged.
- Each `page-N.html` is fully self-contained with inlined CSS and JS.
- Path-traversal guards stay in place for all new file-serving routes.
- The `crop_app/static/extracted/<session_id>/` directory is the sole output location.
- Raw HTML overwrite on save — no validation, no version history.

---

### Task 1: Add `write_page_files` to `html_assembler.py` and create per-page templates

**Files:**
- Modify: `table_extractor/html_assembler.py`
- Create: `table_extractor/templates/page.html`
- Create: `table_extractor/templates/index.html`

**Interfaces:**
- Consumes: `pages_data: list[dict]` (each with `{"html": str}`), `title: str`, `session_id: str`, `page_count: int`
- Produces: writes `page-0.html` … `page-{n}.html` and `index.html` to `crop_app/static/extracted/<session_id>/`

- [ ] **Step 1: Write the failing test**

Create `tests/test_html_assembler.py` (or use existing test location):

```python
import os
import tempfile
from table_extractor.html_assembler import write_page_files

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
        assert "editable" not in content.lower()  # no prompt change artifacts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_html_assembler.py -v`
Expected: FAIL with `ImportError: cannot import name 'write_page_files'`

- [ ] **Step 3: Write minimal implementation**

Add to `table_extractor/html_assembler.py`:

```python
def write_page_files(session_id, pages_data, title, output_root=None):
    if output_root is None:
        output_root = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "crop_app", "static", "extracted",
        )
    session_dir = os.path.join(output_root, session_id)
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
        page_nav = (
            f'<nav class="page-nav">'
            f'<a href="{prev_href}" class="nav-btn" {"style=visibility:hidden" if i == 0 else ""}>← Prev</a>'
            f'<span class="page-indicator">Page {i+1} of {total}</span>'
            f'<a href="{next_href}" class="nav-btn" {"style=visibility:hidden" if i == total - 1 else ""}>Next →</a>'
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
.page-nav {{ display: flex; justify-content: center; align-items: center; gap: 16px; padding: 16px; }}
.nav-btn {{ color: #4f8cff; text-decoration: none; font-weight: 600; }}
.nav-btn[style*="visibility:hidden"] {{ visibility: hidden; pointer-events: none; }}
.page-indicator {{ font-size: 0.85rem; color: #64748b; }}
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
```

Add `.page-grid` and `.page-card` CSS to `output_page.css` via inline `<style>` in the index page (or extend the inline block in `write_page_files`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_html_assembler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_assembler.py table_extractor/templates/page.html table_extractor/templates/index.html tests/test_html_assembler.py
git commit -m "feat: add write_page_files and per-page templates"
```

---

### Task 2: Update `html_extractor.py` to call `write_page_files`

**Files:**
- Modify: `table_extractor/html_extractor.py`

**Interfaces:**
- Consumes: same `run_extraction` signature, same `pages_data` assembly
- Produces: calls `write_page_files` instead of `assemble_full_document`; yields `{"status": "done", "page_files": ["page-0.html", ...], "index": "index.html"}`

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import patch, MagicMock
from table_extractor.html_extractor import run_extraction

def test_run_extraction_writes_page_files(tmp_path):
    sm = MagicMock()
    sm.load_meta.return_value = {
        "files": ["test.pdf"],
        "pages": [
            {"path": "page_000.png", "classification": "Simple", "crops": []},
        ],
    }
    sm.get_page_dir.return_value = str(tmp_path)

    # Create a fake page image
    img_path = tmp_path / "page_000.png"
    from PIL import Image
    Image.new("RGB", (100, 100)).save(img_path)

    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>hello</p>"):
        events = list(run_extraction("sid", sm, str(tmp_path), "mock-model", max_workers=1))
        done = [e for e in events if e.get("status") == "done"][0]

    assert "page_files" in done
    assert done["page_files"] == ["page-0.html"]
    assert done["index"] == "index.html"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_html_extractor.py -v`
Expected: FAIL — either `write_page_files` not imported, or `done` payload missing `page_files`/`index`.

- [ ] **Step 3: Write minimal implementation**

In `html_extractor.py`, replace the final assembly block:

```python
    pages_data = []
    for page_idx, page_info in enumerate(pages):
        classification = _get_classification(page_info)
        crops = _get_sorted_crops(page_info)
        is_complex = classification == "Complex"

        if not is_complex or len(crops) == 0:
            pages_data.append({"html": results.get((page_idx, None), "")})
        else:
            parts = []
            for crop_idx in range(len(crops)):
                fragment = results.get((page_idx, crop_idx), "")
                if fragment:
                    parts.append(fragment)
            pages_data.append({"html": "\n".join(parts)})

    from table_extractor.html_assembler import write_page_files
    out_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "crop_app", "static", "extracted",
    )
    write_page_files(session_id, pages_data, title, output_root=out_dir)

    yield {
        "status": "done",
        "page_files": [f"page-{i}.html" for i in range(len(pages_data))],
        "index": "index.html",
    }
```

Remove the old `assemble_full_document` call and the `result_html` variable.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_html_extractor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_extractor.py tests/test_html_extractor.py
git commit -m "feat: html_extractor writes per-page files via write_page_files"
```

---

### Task 3: Add `POST /save-page` route and update serving routes in `app.py`

**Files:**
- Modify: `crop_app/app.py`

**Interfaces:**
- Consumes: `session_id`, `page_idx` from URL path; raw HTML body from request
- Produces: writes file to `crop_app/static/extracted/<session_id>/page-{page_idx}.html`; returns JSON status

- [ ] **Step 1: Write the failing test**

```python
import json
import os
from crop_app.app import create_app

def test_save_page_endpoint(tmp_path):
    app = create_app()
    app.config["TESTING"] = True

    # Create a fake extracted directory with a page file
    session_id = "test-save-sid"
    extracted_dir = os.path.join(tmp_path, "extracted", session_id)
    os.makedirs(extracted_dir, exist_ok=True)
    with open(os.path.join(extracted_dir, "page-0.html"), "w", encoding="utf-8") as f:
        f.write("<p>original</p>")

    # Also need a session in uploads dir
    upload_dir = os.path.join(tmp_path, "uploads", session_id)
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, "meta.json"), "w") as f:
        json.dump({"files": [], "pages": [{"path": "page_000.png", "classification": "Simple", "crops": []}]}, f)

    with app.test_client() as client:
        resp = client.post(
            f"/save-page/{session_id}/0",
            data="<p>edited</p>",
            content_type="text/html",
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}

    with open(os.path.join(extracted_dir, "page-0.html"), "r", encoding="utf-8") as f:
        assert f.read() == "<p>edited</p>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py::test_save_page_endpoint -v`
Expected: FAIL — 404 `/save-page/...` not found.

- [ ] **Step 3: Write minimal implementation**

Add to `crop_app/app.py` before `return app`:

```python
    @app.route("/save-page/<session_id>/<int:page_idx>", methods=["POST"])
    def save_page(session_id, page_idx):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"status": "error", "message": "Session not found"}), 404

        meta = _sm.load_meta(session_id)
        total_pages = len(meta.get("pages", []))
        if page_idx < 0 or page_idx >= total_pages:
            return jsonify({"status": "error", "message": "Invalid page index"}), 400

        edited_html = request.get_data(as_text=True)
        if edited_html is None:
            return jsonify({"status": "error", "message": "Empty body"}), 400

        out_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "extracted",
        )
        session_dir = os.path.join(out_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        out_path = os.path.realpath(os.path.join(session_dir, f"page-{page_idx}.html"))

        base_dir = os.path.realpath(out_dir)
        if not out_path.startswith(base_dir):
            return jsonify({"status": "error", "message": "Invalid path"}), 400

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(edited_html)
        except OSError as e:
            return jsonify({"status": "error", "message": str(e)}), 500

        return jsonify({"status": "ok"})
```

Update `serve_extracted_html` to serve `index.html` when `extraction.html` is requested:

```python
    @app.route("/extracted/<session_id>/extraction.html", methods=["GET"])
    def serve_extracted_html(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        base_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "extracted",
        )
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not os.path.isdir(session_dir):
            return "Extraction not found. Please run extraction first.", 404

        index_path = os.path.join(session_dir, "index.html")
        if os.path.exists(index_path):
            return send_file(index_path, mimetype="text/html")

        return "Extraction not found. Please run extraction first.", 404
```

Add new file-serving route for individual pages:

```python
    @app.route("/extracted/<session_id>/page-<int:page_idx>.html", methods=["GET"])
    def serve_extracted_page(session_id, page_idx):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        base_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "extracted",
        )
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not os.path.isdir(session_dir):
            return "Extraction not found. Please run extraction first.", 404

        out_path = os.path.realpath(os.path.join(session_dir, f"page-{page_idx}.html"))
        if not out_path.startswith(session_dir):
            return "Invalid page index", 400
        if not os.path.exists(out_path):
            return "Page not found.", 404
        return send_file(out_path, mimetype="text/html")
```

Update `extract_progress_sse` `done` handler to use the new payload shape:

```python
                    if event["status"] == "done":
                        yield f"data: {json.dumps({'status': 'done'})}\n\n"
```
(The endpoint doesn't need the HTML string anymore; the extraction function writes files directly.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py -v`
Expected: PASS for `test_save_page_endpoint` and existing app tests.

- [ ] **Step 5: Commit**

```bash
git add crop_app/app.py tests/test_app.py
git commit -m "feat: add POST /save-page and update extracted serving routes"
```

---

### Task 4: Create per-page edit JavaScript (`output_page_edit.js`)

**Files:**
- Create: `table_extractor/templates/output_page_edit.js`

**Interfaces:**
- Runs inside `page-N.html` after DOMContentLoaded
- Makes text-bearing elements editable, watches for changes, shows Save button

- [ ] **Step 1: Write the failing test**

Use a Playwright or simple Selenium-style integration test, or for speed, a JS-runner:

```python
# tests/test_output_page_edit_js.py
import subprocess, json, os

def test_edit_js_injects_editable_regions():
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "table_extractor", "templates", "output_page_edit.js"
    )
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    assert "contenteditable" in js or "input" in js
    assert "MutationObserver" in js
    assert "Save Changes" in js
    assert "page-nav" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_output_page_edit_js.py -v`
Expected: FAIL — file doesn't exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `table_extractor/templates/output_page_edit.js`:

```javascript
document.addEventListener("DOMContentLoaded", function () {
  var EDITABLE_SELECTORS = [
    "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "dd", "dt", "span.field",
  ];
  var editedElements = new Set();
  var saveButton = null;
  var toast = null;

  function getTextNodes(el) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue.trim();
      if (text.length > 0 && !node.parentNode.closest("a, sup, script, style")) {
        nodes.push(node);
      }
    }
    return nodes;
  }

  function markEdited(el) {
    if (!el.classList.contains("edited")) {
      el.classList.add("edited");
      editedElements.add(el);
    }
    showSaveButton();
  }

  function showSaveButton() {
    if (saveButton) return;
    saveButton = document.createElement("button");
    saveButton.className = "save-btn";
    saveButton.textContent = "Save Changes";
    saveButton.type = "button";
    saveButton.addEventListener("click", function () {
      saveButton.disabled = true;
      saveButton.textContent = "Saving...";
      var match = window.location.pathname.match(/\/extracted\/([^/]+)\/page-(\d+)\.html$/);
      var saveUrl = match ? "/save-page/" + encodeURIComponent(match[1]) + "/" + match[2] : "";
      fetch(saveUrl, {
        method: "POST",
        headers: { "Content-Type": "text/html" },
        body: document.documentElement.outerHTML,
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === "ok") {
          editedElements.clear();
          document.querySelectorAll(".edited").forEach(function (el) {
            el.classList.remove("edited");
          });
          if (toast) toast.remove();
          toast = document.createElement("div");
          toast.className = "save-toast";
          toast.textContent = "Saved";
          document.body.appendChild(toast);
          setTimeout(function () { if (toast) toast.remove(); }, 2000);
          saveButton.remove();
          saveButton = null;
        } else {
          saveButton.disabled = false;
          saveButton.textContent = "Save Changes";
          var err = document.createElement("span");
          err.className = "save-error";
          err.textContent = data.message || "Save failed";
          saveButton.parentNode.insertBefore(err, saveButton.nextSibling);
        }
      })
      .catch(function () {
        saveButton.disabled = false;
        saveButton.textContent = "Save Changes";
      });
    });
    document.body.appendChild(saveButton);
  }

  EDITABLE_SELECTORS.forEach(function (sel) {
    document.querySelectorAll("." + sel).forEach(function (el) {
      if (el.querySelector("input, textarea, select")) return;
      var textNodes = getTextNodes(el);
      if (textNodes.length === 0) return;

      if (textNodes.length === 1 && el.children.length === 0) {
        var input = document.createElement("input");
        input.type = "text";
        input.value = textNodes[0].nodeValue;
        input.className = "inline-edit-input";
        textNodes[0].parentNode.replaceChild(input, textNodes[0]);
        input.addEventListener("input", function () { markEdited(input); });
        input.addEventListener("focus", function () { input.select(); });
      } else {
        el.setAttribute("contenteditable", "true");
        el.addEventListener("input", function () { markEdited(el); });
      }
    });
  });
});
```

Add corresponding CSS rules inline in `write_page_files` (Task 1) or in a new inline `<style>` block within the per-page template:

```css
.inline-edit-input {
  border: none;
  background: transparent;
  font: inherit;
  color: inherit;
  padding: 0;
  margin: 0;
  outline: none;
  width: auto;
  min-width: 1ch;
}
.inline-edit-input:focus {
  outline: 2px solid #4f8cff;
  outline-offset: 1px;
  background: #fff;
}
[contenteditable="true"]:focus {
  outline: 2px solid #4f8cff;
  outline-offset: 1px;
}
.edited {
  outline: 2px solid #4f8cff !important;
  outline-offset: 1px;
}
.save-btn {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 9999;
  background: #4f8cff;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 12px 24px;
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}
.save-btn:disabled {
  opacity: 0.7;
  cursor: not-allowed;
}
.save-toast {
  position: fixed;
  bottom: 80px;
  right: 24px;
  z-index: 9999;
  background: #22c55e;
  color: #fff;
  padding: 8px 16px;
  border-radius: 6px;
  font-weight: 600;
  animation: fadeInOut 2s ease-out forwards;
}
@keyframes fadeInOut {
  0% { opacity: 0; transform: translateY(10px); }
  20% { opacity: 1; transform: translateY(0); }
  80% { opacity: 1; }
  100% { opacity: 0; }
}
.save-error {
  position: fixed;
  bottom: 80px;
  right: 24px;
  z-index: 9999;
  background: #ef4444;
  color: #fff;
  padding: 8px 16px;
  border-radius: 6px;
  font-weight: 600;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_output_page_edit_js.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add table_extractor/templates/output_page_edit.js tests/test_output_page_edit_js.py
git commit -m "feat: add per-page editable field injection JS"
```

---

### Task 5: Wire up `index.html` and add page-grid CSS

**Files:**
- Modify: `table_extractor/html_assembler.py` (inline styles in `write_page_files`)
- Create: no new files

**Interfaces:**
- Ensures `index.html` has a styled grid of page links.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_html_assembler.py::test_index_page_has_page_grid -v`
Expected: FAIL — `page-grid` missing.

- [ ] **Step 3: Write minimal implementation**

Modify the `write_page_files` function from Task 1 to include `.page-grid` and `.page-card` CSS in the inline `<style>` of `index.html`:

```css
.page-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; margin-top: 24px; }
.page-card { display: block; padding: 24px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; text-align: center; text-decoration: none; color: #1e293b; font-weight: 600; transition: background 0.15s; }
.page-card:hover { background: #f1f5f9; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_html_assembler.py::test_index_page_has_page_grid -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_assembler.py tests/test_html_assembler.py
git commit -m "feat: add styled page-grid to index.html"
```

---

### Task 6: End-to-end verification

**Files:**
- No new files

- [ ] **Step 1: Run existing extraction on a known session and verify file layout**

```bash
# In a Python shell or fresh test:
python -c "
from table_extractor.html_assembler import write_page_files
write_page_files('e2e-test', [
    {'html': '<table><tr><td>√</td><td>24.43 km/l</td></tr></table>'},
    {'html': '<ul><li>ADAS Level 2</li></ul>'},
], 'E2E Doc', output_root='/tmp/extracted')
"
ls /tmp/extracted/e2e-test/
# Expected: index.html page-0.html page-1.html
```

- [ ] **Step 2: Open `page-0.html` in a browser (or headless) and verify editability**

```bash
python -m pytest tests/ -k "test_output_page_edit_js or test_write_page_files or test_save_page or test_run_extraction" -v
```

- [ ] **Step 3: Verify save round-trip**

```bash
python -c "
import requests
# Assuming Flask dev server running on :5000 with a test session
# or use test client as in Task 3.
"
```

- [ ] **Step 4: Lint check**

Run: `python -m py_compile table_extractor/html_assembler.py table_extractor/html_extractor.py crop_app/app.py`
Run: `python -m py_compile tests/test_*.py`

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: verify per-page extraction e2e"
```

---

## Spec Coverage Self-Review

| Spec requirement | Task |
|---|---|
| Per-page `page-N.html` files | Task 1 (`write_page_files`) |
| Crops merge only within parent page | Task 2 (`run_extraction` assembly unchanged, write split) |
| Editable DOM elements (table cells, list items, headings, paragraphs, def terms) | Task 4 (`output_page_edit.js` selectors) |
| Sticky Save Changes button with MutationObserver | Task 4 |
| Save overwrites original file via `POST /save-page` | Task 3 |
| `index.html` navigation with prev/next | Task 1 + Task 5 |
| No LLM prompt changes | Verified — no prompt files modified |
| Existing `assemble_full_document` preserved | Verified — only added new function |
| Path-traversal guards preserved | Task 3 |

**No gaps in spec coverage.**

**No placeholders found** — all code blocks are complete.

**Type consistency:** `write_page_files(session_id: str, pages_data: list, title: str, output_root: str)` signature matches across Tasks 1, 2, and 6. `POST /save-page/<session_id>/<page_idx>` path matches in Task 3 route definition, serving route, and JS `fetch("")` call (relative POST).

---

Plan complete and saved to `docs/superpowers/plans/2026-07-09-per-page-edit-extraction.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
