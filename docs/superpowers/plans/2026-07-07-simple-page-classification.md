# Simple Page Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the binary `complex: bool` classification with `classification: "Simple" | "Complex"` using 6 specific criteria that determine whether a page can be processed by a small/cheap vision LLM.

**Architecture:** LLM-only approach where the vision model evaluates all 6 criteria holistically in a single pass and returns the classification.

**Tech Stack:** Python, Flask, OpenRouter API, PIL/Pillow, pytest

## Global Constraints

- Page classified as "Simple" when BOTH conditions are satisfied:
  - **Positive Indicators (at least one must be true):**
    1. The page only contains images with no text (0% text)
    2. The page contains less than 60% text overall
    3. The page contains simple tables (no sub-sections, row/column spans, or merges) AND has less than 60% text overall
    4. All text on the page is bold/large (approximately >18pt and high weight)
  - **Negative/General Constraints (both must be true):**
    5. The page does not contain too many or complex symbols (e.g., ^^#, **#, ^^^, etc.)
    6. The page can be easily and confidently scanned by a small/cheap vision LLM
- Response format: minimal - just `classification` value
- Confidence-based: only return "Simple" when all constraints are confidently met; otherwise "Complex"
- Simple pages remain at 150 DPI; Complex pages upgrade to 300 DPI
- Frontend shows all pages with Simple/Complex markers; null classification before analysis shows "Pending" badge
- Default on error/parse-failure: classify as "Complex"

---

## File Structure

### Files to Modify

| File | Responsibility |
|------|----------------|
| `crop_app/llm.py` | Update `ANALYSIS_PROMPT`, remove `LABELS` constant, update parsing |
| `crop_app/app.py` | Update `analyze_session()` to use new classification field |
| `crop_app/static/js/upload.js` | Update badge logic for Simple/Complex |
| `crop_app/static/css/style.css` | Update badge classes |
| `crop_app/tests/test_llm.py` | Add tests for new classification parsing and response handling |
| `crop_app/tests/test_analysis.py` | Update tests to use classification field |
| `crop_app/tests/test_app.py` | Update test data |
| `crop_app/tests/test_crop_routes.py` | Update test data |
| `crop_app/tests/test_session_manager.py` | Update test data |

### New Files

None - using existing test infrastructure.

---

## Task 1: Update LLM Prompt and Response Schema

**Files:**
- Modify: `crop_app/llm.py:22-53` (ANALYSIS_PROMPT, LABELS constant removal, related constants)
- Modify: `crop_app/llm.py:56-126` (analyze_page function and _parse_response)

**Interfaces:**
- Consumes: Image file path
- Produces: `{"classification": "Simple" | "Complex", "error": str|None}`

- [ ] **Step 1: Write the failing test**

```python
# crop_app/tests/test_llm.py
def test_parse_response_simple():
    raw = '{"classification": "Simple"}'
    result = _parse_response(raw)
    assert result["classification"] == "Simple"

def test_parse_response_complex():
    raw = '{"classification": "Complex"}'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"

def test_parse_response_invalid():
    raw = 'not json'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"  # Default to Complex on error
    assert result["error"] is not None
```

- [ ] **Step 2: Update existing tests in test_llm.py**

Replace existing tests that check `complex` and `labels` with new tests for `classification`:

```python
# Replace test_parse_response_complex_true and test_parse_response_complex_false
def test_parse_response_complex_true():
    raw = '{"classification": "Complex"}'
    result = _parse_response(raw)
    assert result["classification"] == "Complex"

def test_parse_response_complex_false():
    raw = '{"classification": "Simple"}'
    result = _parse_response(raw)
    assert result["classification"] == "Simple"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest crop_app/tests/test_llm.py::test_parse_response_simple -v`
Expected: FAIL with "classification" key not found

- [ ] **Step 4: Update ANALYSIS_PROMPT**

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

- [ ] **Step 5: Update _parse_response function**

```python
def _parse_response(raw: str) -> dict:
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
            return {
                "classification": classification,
                "error": None,
            }
    except (json.JSONDecodeError, AttributeError):
        pass

    return {"classification": "Complex", "error": f"Failed to parse: {raw[:200]}"}
```

- [ ] **Step 5: Update analyze_page exception fallback**

The `except` block in `analyze_page` at line 95 currently returns `{"complex": False, "labels": [], "error": str(e)}`.
Update it to:

```python
    except Exception as e:
        return {"classification": "Complex", "error": str(e)}
```

- [ ] **Step 6: Update analyze_page docstring**

```python
def analyze_page(image_path: str) -> dict:
    """Send a page image to the LLM and return classification.

    Returns: {"classification": "Simple" | "Complex", "error": str|None}
    """
```

The `return _parse_response(raw_content)` call in the happy path remains unchanged.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest crop_app/tests/test_llm.py -v`
Expected: All tests PASS

- [ ] **Step 8: Commit**

```bash
git add crop_app/llm.py crop_app/tests/test_llm.py
git commit -m "feat: update LLM classification to Simple/Complex with criteria"
```

---

## Task 2: Update App Session Analysis Logic

**Files:**
- Modify: `crop_app/app.py:144-185` (analyze_session function)

**Interfaces:**
- Consumes: `classification` from analyze_page
- Produces: Updated page metadata with classification field

- [ ] **Step 1: Write the failing test**

Add to `crop_app/tests/test_analysis.py`:

```python
def test_analyze_endpoint_returns_classification(app_with_session):
    client, sid = app_with_session

    mock_result = {"classification": "Simple", "error": None}
    with patch("app.analyze_page", return_value=mock_result):
        resp = client.post(f"/analyze/{sid}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["pages"][0]["classification"] == "Simple"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest crop_app/tests/test_analysis.py::test_analyze_endpoint_returns_classification -v`
Expected: FAIL - classification key not found

- [ ] **Step 3: Update analyze_session in app.py with ALL required changes**

Replace all references to `complex` and `labels` with `classification`:

**Line 72-73** (annotate_page route — maps page data to template):
```python
# Replace:
"complex": p["complex"],
"labels": p["labels"],

# With:
"classification": p["classification"],
```

**Line 156** (analyze_session — skip already-analyzed pages):
```python
# Replace:
if page_info["complex"] is not None:

# With:
if page_info.get("classification") is not None:
```

**Line 161-162** (analyze_session — missing file fallback):
```python
# Replace:
page_info["complex"] = False
page_info["labels"] = []

# With:
page_info["classification"] = "Complex"
```

**Line 167-168** (analyze_session — store analysis result):
```python
# Replace:
page_info["complex"] = result["complex"]
page_info["labels"] = result["labels"]

# With:
page_info["classification"] = result["classification"]
```

**Line 172** (analyze_session — PDF upgrade trigger):
```python
# Replace:
if result["complex"] and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:

# With:
if result["classification"] == "Complex" and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:
```

**Upload route** (lines 111, 128 — metadata schema for new pages):
```python
# Replace:
"complex": None,
"labels": [],

# With:
"classification": None,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest crop_app/tests/test_analysis.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add crop_app/app.py crop_app/tests/test_analysis.py
git commit -m "feat: update app to use classification field"
```

---

## Task 3: Update Frontend Integration

**Files:**
- Modify: `crop_app/static/js/upload.js`
- Modify: `crop_app/static/css/style.css`

**Interfaces:**
- Consumes: `classification` from page metadata
- Produces: UI showing Simple/Complex markers

- [ ] **Step 1: Update upload.js badge logic**

Replace the badge logic at `upload.js:153-163` to handle all three states (null/Complex/Simple):

```javascript
// Replace:
if (page.complex === true) {
  badge.classList.add("badge-complex");
  badge.textContent = "Complex";
  card.classList.add("page-complex");
  card.addEventListener("click", () => {
    window.location = `/annotate/${sessionId}?page=${index}`;
  });
} else {
  badge.classList.add("badge-simple");
  badge.textContent = "Simple";
}

// With:
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
  // classification is null — analysis not yet run
  badge.classList.add("badge-pending");
  badge.textContent = "Pending";
}
```

- [ ] **Step 2: Add CSS classes for Simple and Pending badges**

```css
/* Add to crop_app/static/css/style.css */
.badge.badge-simple {
  background: #e6f4ea;
  color: #1e7e34;
}

.badge.badge-pending {
  background: #f0f0f0;
  color: #666;
}

.page-card.page-simple {
  border-left: 3px solid #1e7e34;
}
```

- [ ] **Step 3: Commit**

```bash
git add crop_app/static/js/upload.js crop_app/static/css/style.css
git commit -m "feat: update frontend for Simple/Complex classification"
```

---

## Task 4: Update Test Data Files

**Files:**
- Modify: `crop_app/tests/test_app.py`
- Modify: `crop_app/tests/test_crop_routes.py`
- Modify: `crop_app/tests/test_session_manager.py`
- Modify: `crop_app/tests/test_analysis.py`

- [ ] **Step 1: Update test_analysis.py existing tests**

Replace all existing test assertions on `complex` and `labels` with `classification`:

**test_analyze_endpoint_returns_updated_meta** (line 94-100):
```python
# Replace:
mock_result = {"complex": True, "labels": ["table"], "error": None}
# ...
assert data["pages"][0]["complex"] is True
assert "table" in data["pages"][0]["labels"]

# With:
mock_result = {"classification": "Complex", "error": None}
# ...
assert data["pages"][0]["classification"] == "Complex"
```

**test_complex_non_pdf_page_does_not_trigger_upgrade** (line 129-136):
```python
# Replace:
mock_result = {"complex": True, "labels": ["table"], "error": None}
# ...
assert data["pages"][0]["complex"] is True

# With:
mock_result = {"classification": "Complex", "error": None}
# ...
assert data["pages"][0]["classification"] == "Complex"
```

**test_simple_pdf_page_does_not_trigger_upgrade** (line 143-150):
```python
# Replace:
mock_result = {"complex": False, "labels": [], "error": None}
# ...
assert data["pages"][0]["complex"] is False

# With:
mock_result = {"classification": "Simple", "error": None}
# ...
assert data["pages"][0]["classification"] == "Simple"
```

**test_complex_pdf_page_triggers_upgrade** (line 156-167):
```python
# Replace:
mock_result = {"complex": True, "labels": ["table"], "error": None}
# ...
assert data["pages"][0]["complex"] is True

# With:
mock_result = {"classification": "Complex", "error": None}
# ...
assert data["pages"][0]["classification"] == "Complex"
```

**test_upgrade_failure_records_error** (line 174-180):
```python
# Replace:
mock_result = {"complex": True, "labels": ["table"], "error": None}
# ...
assert data["pages"][0]["complex"] is True

# With:
mock_result = {"classification": "Complex", "error": None}
# ...
assert data["pages"][0]["classification"] == "Complex"
```

**Fixture data** (lines 33-34, 79-80 — page metadata in app_with_session and app_with_pdf_session):
```python
# Replace:
"complex": None,
"labels": [],

# With:
"classification": None,
```

- [ ] **Step 2: Update test_app.py**

Replace `"complex": True` with `"classification": "Complex"` and `"complex": False` with `"classification": "Simple"` in test fixtures.

- [ ] **Step 3: Update test_crop_routes.py**

Replace `"complex": True, "labels": ["table"]` with `"classification": "Complex"`.

- [ ] **Step 4: Update test_session_manager.py**

Replace `"complex": True, "labels": ["table"]` with `"classification": "Complex"`.

- [ ] **Step 5: Commit**

```bash
git add crop_app/tests/test_app.py crop_app/tests/test_crop_routes.py crop_app/tests/test_session_manager.py crop_app/tests/test_analysis.py
git commit -m "test: update test data to use classification field"
```

---

## Task 5: Update Documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-07-05-brochure-crop-tool-design.md`

- [ ] **Step 1: Update design spec**

Document the new classification criteria and response format.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-05-brochure-crop-tool-design.md
git commit -m "docs: update spec with Simple/Complex classification"
```

---

## Task 6: Integration Testing

**Files:**
- Run: Full test suite

- [ ] **Step 1: Run all tests**

```bash
pytest crop_app/tests/ -v
```

- [ ] **Step 2: Verify no regressions**

- [ ] **Step 3: Commit**

```bash
git commit -m "test: add integration tests for Simple/Complex classification"
```

---

## Verification Checklist

- [ ] All unit tests pass
- [ ] Integration tests pass
- [ ] Manual testing confirms correct behavior
- [ ] Documentation updated
- [ ] Branch pushed to remote