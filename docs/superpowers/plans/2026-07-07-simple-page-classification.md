# Simple Page Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the binary `complex: bool` / `labels: list[str]` page-analysis contract with `classification: "Simple" | "Complex"`, where "Simple" means the page can be processed confidently by a small/cheap vision LLM.

**Architecture:** Use one OpenRouter vision call per page. The LLM returns a minimal JSON object with a single `classification` field. The Flask app stores this field in session metadata and derives PDF upgrade behavior from it: only `"Complex"` pages are upgraded to high resolution; `"Simple"` pages remain at the original 150 DPI.

**Tech Stack:** Python, Flask, OpenRouter API, PIL/Pillow, pytest, vanilla JavaScript/CSS

## Global Constraints

- Page metadata uses `classification`, not `complex` or `labels`.
- Valid classifications: `"Simple"`, `"Complex"`, or `None` before analysis.
- Null classification before analysis renders as `Pending` in the upload page UI.
- Default on LLM exception, invalid JSON, missing field, or unknown classification value: `"Complex"`.
- A page is `"Simple"` only when both condition groups are satisfied:
  - **Positive Indicators (at least one must be true):**
    1. The page only contains images with no text (0% text).
    2. The page contains less than 60% text overall.
    3. The page contains simple tables with no sub-sections, row/column spans, or merges AND has less than 60% text overall.
    4. All text on the page is bold/large (approximately >18pt and high weight).
  - **Negative/General Constraints (both must be true):**
    5. The page does not contain too many or complex symbols (e.g., `^^#`, `**#`, `^^^`).
    6. The page can be easily and confidently scanned by a small/cheap vision LLM.
- If the model is unsure whether both groups are satisfied, it must classify as `"Complex"`.
- Simple pages remain at 150 DPI; Complex pages upgrade to 300 DPI.

---

## File Structure

### Files to Modify

| File | Responsibility |
|------|----------------|
| `crop_app/llm.py` | Update prompt, remove label filtering, parse minimal `classification` response, default errors to `Complex` |
| `crop_app/app.py` | Replace all `complex`/`labels` metadata usage with `classification`; keep upgrade behavior based on `Complex` |
| `crop_app/static/js/upload.js` | Render `Complex`, `Simple`, and `Pending` badges from `classification` |
| `crop_app/static/css/style.css` | Add/confirm badge styles for `Simple` and `Pending` |
| `crop_app/tests/test_llm.py` | Update parser and API-error tests for classification response |
| `crop_app/tests/test_analysis.py` | Update all analysis route fixtures, mocks, and assertions |
| `crop_app/tests/test_app.py` | Update test metadata fixtures |
| `crop_app/tests/test_crop_routes.py` | Update test metadata fixtures |
| `crop_app/tests/test_session_manager.py` | Update test metadata fixtures |
| `docs/superpowers/specs/2026-07-05-brochure-crop-tool-design.md` | Update design spec to document classification contract |

### New Files

None.

---

## Task 1: Update LLM Prompt and Response Schema

**Files:**
- Modify: `crop_app/llm.py`
- Test: `crop_app/tests/test_llm.py`

**Interfaces:**
- Consumes: `image_path: str`
- Produces from `analyze_page(image_path)`: `{"classification": "Simple" | "Complex", "error": str | None}`
- Produces from `_parse_response(raw)`: `{"classification": "Simple" | "Complex", "error": str | None}`

- [ ] **Step 1: Replace parser tests in `crop_app/tests/test_llm.py`**

Replace the existing parser tests with these classification-based tests:

```python
def test_parse_response_simple():
    raw = '{"classification": "Simple"}'
    result = _parse_response(raw)
    assert result["classification"] == "Simple"
    assert result["error"] is None


def test_parse_response_complex():
    raw = '{"classification": "Complex"}'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_parse_response_with_markdown_fences():
    raw = '```json\n{"classification": "Complex"}\n```'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None


def test_parse_response_invalid_json_defaults_complex():
    raw = "not json at all"
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is not None


def test_parse_response_invalid_classification_defaults_complex():
    raw = '{"classification": "Unknown"}'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"
    assert result["error"] is None
```

- [ ] **Step 2: Update `test_analyze_page_success` in `crop_app/tests/test_llm.py`**

```python
def test_analyze_page_success(tmp_path):
    from PIL import Image
    img_path = str(tmp_path / "page.png")
    Image.new("RGB", (100, 100), "white").save(img_path)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"classification": "Complex"}'

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("llm._get_client", return_value=mock_client):
        result = analyze_page(img_path)

    assert result["classification"] == "Complex"
    assert result["error"] is None
```

- [ ] **Step 3: Update `test_analyze_page_api_error` in `crop_app/tests/test_llm.py`**

```python
def test_analyze_page_api_error(tmp_path):
    from PIL import Image
    img_path = str(tmp_path / "page.png")
    Image.new("RGB", (100, 100), "white").save(img_path)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("API down")

    with patch("llm._get_client", return_value=mock_client):
        result = analyze_page(img_path)

    assert result["classification"] == "Complex"
    assert result["error"] is not None
```

- [ ] **Step 4: Run LLM tests to verify they fail before implementation**

Run: `pytest crop_app/tests/test_llm.py -v`
Expected: FAIL because `_parse_response` still returns `complex`/`labels` and `analyze_page` exception fallback still returns `complex`/`labels`.

- [ ] **Step 5: Replace `ANALYSIS_PROMPT` in `crop_app/llm.py`**

```python
ANALYSIS_PROMPT = """You are analyzing a single page of a product brochure / spec sheet.
Your job is to determine whether the page is "Simple" or "Complex" for processing by a small and cheap vision LLM.

Classify as "Simple" only when BOTH of the following condition groups are met:

Positive Indicators — at least ONE must be true:
1. The page only contains images with no text (0% text).
2. The page contains less than 60% text overall.
3. The page contains simple tables (no sub-sections, no row/column spans, no merges) with less than 60% text overall.
4. All text on the page is bold/large-font (approximately >18pt and high weight).

Negative / General Constraints — BOTH must be true:
5. The page does NOT contain too many or complex symbols (e.g., ^^#, **#, ^^^, etc.).
6. The page can be easily and confidently scanned by a small/cheap vision LLM.

If you cannot confidently satisfy both groups, classify as "Complex".

Respond with ONLY valid JSON in this exact format:
{
  "classification": "Simple" or "Complex"
}

Do NOT include any other text, explanation, or markdown formatting outside the JSON.
"""
```

- [ ] **Step 6: Remove `LABELS` from `crop_app/llm.py`**

Remove the `LABELS = [...]` constant entirely because labels are no longer returned or filtered.

- [ ] **Step 7: Update `analyze_page` docstring and exception fallback**

```python
def analyze_page(image_path: str) -> dict:
    """Send a page image to the LLM and return classification.

    Returns: {"classification": "Simple" | "Complex", "error": str|None}
    """
    try:
        ...
        raw_content = response.choices[0].message.content or ""
        return _parse_response(raw_content)

    except Exception as e:
        return {"classification": "Complex", "error": str(e)}
```

- [ ] **Step 8: Replace `_parse_response` in `crop_app/llm.py`**

```python
def _parse_response(raw: str) -> dict:
    """Parse LLM JSON response, stripping markdown fences if present."""
    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            classification = data.get("classification", "Complex")
            if classification not in ("Simple", "Complex"):
                classification = "Complex"
            return {"classification": classification, "error": None}
    except (json.JSONDecodeError, AttributeError):
        pass

    return {"classification": "Complex", "error": f"Failed to parse: {raw[:200]}"}
```

- [ ] **Step 9: Run LLM tests**

Run: `pytest crop_app/tests/test_llm.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add crop_app/llm.py crop_app/tests/test_llm.py
git commit -m "feat: classify pages as Simple or Complex"
```

---

## Task 2: Update Flask App Metadata and Analysis Flow

**Files:**
- Modify: `crop_app/app.py`
- Test: `crop_app/tests/test_analysis.py`

**Interfaces:**
- Consumes from `analyze_page`: `{"classification": "Simple" | "Complex", "error": str | None}`
- Produces in each page metadata object: `"classification": None | "Simple" | "Complex"`

- [ ] **Step 1: Replace metadata fixtures in `crop_app/tests/test_analysis.py`**

In both fixtures (`app_with_session` and `app_with_pdf_session`), replace:

```python
"complex": None,
"labels": [],
```

with:

```python
"classification": None,
```

- [ ] **Step 2: Update every mock result and assertion in `crop_app/tests/test_analysis.py`**

Use these replacements:

```python
# Complex mock result
mock_result = {"classification": "Complex", "error": None}

# Simple mock result
mock_result = {"classification": "Simple", "error": None}

# Complex assertion
assert data["pages"][0]["classification"] == "Complex"

# Simple assertion
assert data["pages"][0]["classification"] == "Simple"
```

Specific existing tests that must be updated:
- `test_analyze_endpoint_returns_updated_meta`
- `test_complex_non_pdf_page_does_not_trigger_upgrade`
- `test_simple_pdf_page_does_not_trigger_upgrade`
- `test_complex_pdf_page_triggers_upgrade`
- `test_upgrade_failure_records_error`

- [ ] **Step 3: Add missing-file fallback test in `crop_app/tests/test_analysis.py`**

```python
def test_missing_page_file_defaults_complex(app_with_session):
    client, sid = app_with_session

    app = client.application
    sm = app.session_manager
    meta = sm.load_meta(sid)
    page_path = os.path.join(sm.get_page_dir(sid), meta["pages"][0]["path"])
    os.remove(page_path)

    resp = client.post(f"/analyze/{sid}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["pages"][0]["classification"] == "Complex"
    assert data["pages"][0]["error"] == "Page file missing"
```

- [ ] **Step 4: Add already-analyzed skip test in `crop_app/tests/test_analysis.py`**

```python
def test_analyze_skips_pages_with_existing_classification(app_with_session):
    client, sid = app_with_session

    app = client.application
    sm = app.session_manager
    meta = sm.load_meta(sid)
    meta["pages"][0]["classification"] = "Simple"
    sm.save_meta(sid, meta)

    with patch("app.analyze_page") as mock_analyze:
        resp = client.post(f"/analyze/{sid}")

    assert resp.status_code == 200
    mock_analyze.assert_not_called()
    data = resp.get_json()
    assert data["pages"][0]["classification"] == "Simple"
```

- [ ] **Step 5: Run analysis tests to verify they fail before implementation**

Run: `pytest crop_app/tests/test_analysis.py -v`
Expected: FAIL because `app.py` still uses `complex` and `labels`.

- [ ] **Step 6: Update `/annotate` page mapping in `crop_app/app.py`**

In `annotate_page`, replace:

```python
"complex": p["complex"],
"labels": p["labels"],
```

with:

```python
"classification": p.get("classification"),
```

Use `.get()` here so old sessions created before this change do not crash the annotation route.

- [ ] **Step 7: Update upload metadata in `crop_app/app.py` for PDF pages**

In the PDF upload branch, replace:

```python
"complex": None,
"labels": [],
```

with:

```python
"classification": None,
```

- [ ] **Step 8: Update upload metadata in `crop_app/app.py` for image pages**

In the image upload branch, replace:

```python
"complex": None,
"labels": [],
```

with:

```python
"classification": None,
```

- [ ] **Step 9: Update already-analyzed skip check in `crop_app/app.py`**

In `analyze_session`, replace:

```python
if page_info["complex"] is not None:
    continue
```

with:

```python
if page_info.get("classification") is not None:
    continue
```

This avoids `KeyError` for migrated or older session metadata.

- [ ] **Step 10: Update missing-page fallback in `crop_app/app.py`**

Replace:

```python
page_info["complex"] = False
page_info["labels"] = []
page_info["error"] = "Page file missing"
```

with:

```python
page_info["classification"] = "Complex"
page_info["error"] = "Page file missing"
```

Missing files default to `Complex` because errors and uncertain pages should not be silently treated as simple.

- [ ] **Step 11: Update result storage in `crop_app/app.py`**

Replace:

```python
page_info["complex"] = result["complex"]
page_info["labels"] = result["labels"]
```

with:

```python
page_info["classification"] = result.get("classification", "Complex")
```

Using `.get(..., "Complex")` prevents a crash if an older/mock analyzer response lacks the field.

- [ ] **Step 12: Update PDF high-resolution upgrade condition in `crop_app/app.py`**

Replace:

```python
if result["complex"] and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:
```

with:

```python
if page_info["classification"] == "Complex" and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:
```

Use `page_info["classification"]`, not `result["classification"]`, because Step 11 normalizes missing/invalid analyzer results to `Complex`.

- [ ] **Step 13: Run analysis tests**

Run: `pytest crop_app/tests/test_analysis.py -v`
Expected: PASS.

- [ ] **Step 14: Commit**

```bash
git add crop_app/app.py crop_app/tests/test_analysis.py
git commit -m "feat: store page analysis classification"
```

---

## Task 3: Update Frontend Rendering for Classification States

**Files:**
- Modify: `crop_app/static/js/upload.js`
- Modify: `crop_app/static/css/style.css`

**Interfaces:**
- Consumes page metadata field: `classification: null | "Simple" | "Complex"`
- Produces UI badges: `Pending`, `Simple`, or `Complex`

- [ ] **Step 1: Update badge logic in `crop_app/static/js/upload.js`**

Replace the existing `if (page.complex === true) { ... } else { ... }` block with:

```javascript
if (page.classification === "Complex") {
  badge.classList.add("badge-complex");
  badge.textContent = "Complex";
  card.classList.add("page-complex");
  card.addEventListener("click", () => {
    window.location = `/annotate/${sessionId}?page=${index}`;
  });
} else if (page.classification === "Simple") {
  badge.classList.add("badge-simple");
  badge.textContent = "Simple";
} else {
  badge.classList.add("badge-pending");
  badge.textContent = "Pending";
}
```

Do not leave a fallthrough state with no badge.

- [ ] **Step 2: Add CSS classes to `crop_app/static/css/style.css` if missing**

If `.badge.badge-simple` is already present, keep existing styling. Ensure these classes exist:

```css
.badge.badge-simple {
  background: #e6f4ea;
  color: #1e7e34;
}

.badge.badge-pending {
  background: #f0f0f0;
  color: #666;
}
```

- [ ] **Step 3: Verify no frontend references to `page.complex` remain**

Run: `rg "page\.complex|page\.labels" crop_app/static crop_app/templates`
Expected: No matches.

- [ ] **Step 4: Commit**

```bash
git add crop_app/static/js/upload.js crop_app/static/css/style.css
git commit -m "feat: render page classification badges"
```

---

## Task 4: Update Remaining Test Fixtures and Metadata Shapes

**Files:**
- Modify: `crop_app/tests/test_app.py`
- Modify: `crop_app/tests/test_crop_routes.py`
- Modify: `crop_app/tests/test_session_manager.py`

**Interfaces:**
- Test metadata pages use `classification`, not `complex` or `labels`.

- [ ] **Step 1: Update `crop_app/tests/test_app.py`**

Replace page fixture dictionaries like:

```python
{"path": "page_000.png", "complex": True, "labels": ["table"], "crops": []}
```

with:

```python
{"path": "page_000.png", "classification": "Complex", "crops": []}
```

- [ ] **Step 2: Update `crop_app/tests/test_crop_routes.py`**

Replace:

```python
{"path": "page_000.png", "complex": True, "labels": ["table"], "crops": []}
```

with:

```python
{"path": "page_000.png", "classification": "Complex", "crops": []}
```

- [ ] **Step 3: Update `crop_app/tests/test_session_manager.py`**

Replace:

```python
data = {"pages": [{"path": "p0.png", "complex": True, "labels": ["table"]}]}
```

with:

```python
data = {"pages": [{"path": "p0.png", "classification": "Complex"}]}
```

- [ ] **Step 4: Run all crop_app tests**

Run: `pytest crop_app/tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crop_app/tests/test_app.py crop_app/tests/test_crop_routes.py crop_app/tests/test_session_manager.py
git commit -m "test: update fixtures for classification metadata"
```

---

## Task 5: Update Documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-07-05-brochure-crop-tool-design.md`

**Interfaces:**
- Documentation describes `classification: "Simple" | "Complex"`, not `complex`/`labels`.

- [ ] **Step 1: Update analysis contract in the design spec**

Replace the old prompt contract:

```markdown
- Prompt instructs the model to return structured JSON: `{"complex": bool, "labels": [...]}`
- Labels include: `table`, `swatch_grid`, `image_grid`, `text_grid`, `feature_matrix`, `stat_cards`, `technical_drawing`, `none`
- Pages where `complex == true` are flagged
```

with:

```markdown
- Prompt instructs the model to return structured JSON: `{"classification": "Simple" | "Complex"}`
- A page is `Simple` only when at least one positive indicator is true and both negative/general constraints are true.
- Positive indicators: image-only with no text, less than 60% text overall, simple tables without sub-sections/spans/merges and less than 60% text, or all text bold/large (>18pt approximately).
- Negative/general constraints: no excessive/complex symbols and confidently scannable by a small/cheap vision LLM.
- Pages where `classification == "Complex"` are upgraded to 300 DPI and highlighted for manual cropping.
- Pages where `classification == "Simple"` remain at 150 DPI.
```

- [ ] **Step 2: Run documentation grep checks**

Run: `rg '"complex"|"labels"|complex == true|complexity labels' docs/superpowers/specs/2026-07-05-brochure-crop-tool-design.md`
Expected: No stale API-contract references. General prose using "complex" as an adjective is acceptable only if it does not describe the old JSON schema.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-05-brochure-crop-tool-design.md
git commit -m "docs: document Simple Complex classification contract"
```

---

## Task 6: Final Integration Verification

**Files:**
- Verify repository state only.

- [ ] **Step 1: Verify no stale runtime references remain**

Run: `rg 'page_info\["complex"\]|result\["complex"\]|p\["complex"\]|p\["labels"\]|page\.complex|page\.labels|"labels": \[|"complex":' crop_app`
Expected: No matches, except inside comments only if they are explaining removed legacy code. Prefer removing such comments too.

- [ ] **Step 2: Run full crop app test suite**

Run: `pytest crop_app/tests/ -v`
Expected: PASS.

- [ ] **Step 3: Run table extractor tests to ensure unrelated package was not broken**

Run: `pytest table_extractor/tests/ -v`
Expected: PASS.

- [ ] **Step 4: Run lint/type checks if configured**

Check available commands first:

Run: `ls`
Expected: inspect whether this repo has tooling files such as `pyproject.toml`, `setup.cfg`, `tox.ini`, or `Makefile`.

If a lint/typecheck command is configured, run it. If none is configured, record "No configured lint/typecheck command found" in the implementation summary.

- [ ] **Step 5: Commit only if verification caused file changes**

If no files changed during verification, do not create an empty commit.

---

## Verification Checklist

- [ ] LLM parser defaults invalid responses to `Complex`.
- [ ] LLM API exception fallback returns `classification: "Complex"`.
- [ ] New upload metadata initializes `classification: None`.
- [ ] Analyze route skips already-classified pages using `page_info.get("classification")`.
- [ ] Missing page files default to `classification: "Complex"`.
- [ ] PDF pages upgrade only when normalized page classification is `"Complex"`.
- [ ] Upload frontend renders `Pending`, `Simple`, and `Complex` states.
- [ ] No runtime references remain to `complex`/`labels` as metadata keys.
- [ ] `pytest crop_app/tests/ -v` passes.
- [ ] `pytest table_extractor/tests/ -v` passes.
- [ ] Lint/typecheck commands were run if configured, or absence was recorded.
