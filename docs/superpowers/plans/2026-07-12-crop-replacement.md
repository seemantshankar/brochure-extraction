# Crop Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recommitting a resized or moved crop replaces its prior HTML segment instead of retaining a duplicate.

**Architecture:** Treat the existing filename included by the annotation client as a stable crop identity. The commit route regenerates that crop's file and replaces its metadata record; existing reconciliation removes its stale fragment and schedules a fresh extraction. Crops with no filename keep the append-only creation path.

**Tech Stack:** Flask, Pillow, pytest.

## Global Constraints

- Preserve the current crop filename for a committed crop that is resized or moved.
- Reject a supplied update filename unless it belongs to the selected page.
- Do not change behavior for newly drawn crops.

---

### Task 1: Add a crop-replacement regression test

**Files:**

- Modify: `crop_app/tests/test_crop_routes.py`
- Test: `crop_app/tests/test_crop_routes.py`

**Interfaces:**

- Consumes: `POST /commit/<session_id>` with `{page_index: int, crops: [{bbox: list[float], filename: str | null}]}`.
- Produces: a persisted crop with its original `filename` and an updated `bbox` when `filename` identifies an existing crop on the page.

- [ ] **Step 1: Write the failing test**

```python
def test_commit_replaces_existing_crop_when_filename_is_supplied(client_with_session):
    client, sid = client_with_session
    first = client.post(f"/commit/{sid}", json={"page_index": 0, "crops": [{"bbox": [0, 0, 1, 1], "filename": None}]}).get_json()["crops"][0]
    response = client.post(f"/commit/{sid}", json={"page_index": 0, "crops": [{"bbox": [0, 0, 0.5, 1], "filename": first["filename"]}, {"bbox": [0.5, 0, 1, 1], "filename": None}]})
    assert response.status_code == 200
    crops = client.application.session_manager.load_meta(sid)["pages"][0]["crops"]
    assert len(crops) == 2
    assert crops[0]["filename"] == first["filename"]
    assert crops[0]["bbox"] == [0, 0, 0.5, 1]
    assert {crop["filename"] for crop in crops} == {first["filename"], "crop_001.png"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest crop_app/tests/test_crop_routes.py::test_commit_replaces_existing_crop_when_filename_is_supplied -v`

Expected: FAIL because the endpoint appends the modified crop and leaves the original crop record present.

- [ ] **Step 3: Commit the failing test**

Run: `git add crop_app/tests/test_crop_routes.py && git commit -m "test: cover replacing a committed crop"`

### Task 2: Replace committed crops in the commit route

**Files:**

- Modify: `crop_app/app.py:321-343`
- Test: `crop_app/tests/test_crop_routes.py`

**Interfaces:**

- Consumes: a crop object that optionally contains `filename` and always contains `bbox`.
- Produces: updated `page_info["crops"]` records and regenerated crop images without changing the crop filename.

- [ ] **Step 1: Implement the minimal update branch**

```python
requested_filename = item.get("filename")
existing_record = next((crop for crop in existing if (crop.get("filename") or crop.get("path")) == requested_filename), None)
if requested_filename:
    if existing_record is None:
        return jsonify({"error": "Crop not found in page metadata"}), 404
    crop_path = cm.save_crop(session_id, page_path, bbox, filename=requested_filename)
    existing_record.update({"path": os.path.basename(crop_path), "filename": os.path.basename(crop_path), "bbox": bbox})
    newly_saved.append(existing_record)
    continue
```

Keep the current rounded-bounding-box duplicate check and new `crop_NNN.png` creation in the no-filename branch.

- [ ] **Step 2: Run the regression test to verify it passes**

Run: `pytest crop_app/tests/test_crop_routes.py::test_commit_replaces_existing_crop_when_filename_is_supplied -v`

Expected: PASS.

- [ ] **Step 3: Run the focused route suite**

Run: `pytest crop_app/tests/test_crop_routes.py -v`

Expected: PASS with zero failures.

- [ ] **Step 4: Commit the implementation**

Run: `git add crop_app/app.py crop_app/tests/test_crop_routes.py && git commit -m "fix: replace adjusted committed crops"`

### Task 3: Make inline text editing wrap

**Files:**

- Modify: `table_extractor/templates/output_page_edit.js`
- Modify: `table_extractor/templates/output_page.css`
- Modify: `table_extractor/tests/test_output_page_edit_js.py`

**Interfaces:**

- Consumes: a leaf editable element such as `<p>paragraph text</p>`.
- Produces: a wrapping `<textarea class="inline-edit-input">`; saving restores its text as a text node.

- [ ] **Step 1: Write the failing template test**

```python
def test_edit_js_uses_wrapping_textareas_for_leaf_text():
    js = open(js_path, encoding="utf-8").read()
    assert 'document.createElement("textarea")' in js
    assert 'querySelectorAll("textarea.inline-edit-input")' in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest table_extractor/tests/test_output_page_edit_js.py::test_edit_js_uses_wrapping_textareas_for_leaf_text -v`

Expected: FAIL because leaf text currently uses a single-line input.

- [ ] **Step 3: Implement textarea editing and save cleanup**

```javascript
var input = document.createElement("textarea");
input.rows = 1;
input.className = "inline-edit-input";
// On save, select all .inline-edit-input textareas and restore input.value as text.
```

Add `.inline-edit-input { resize: vertical; white-space: pre-wrap; }` to the page CSS while retaining its width and box-sizing constraints.

- [ ] **Step 4: Run template tests**

Run: `pytest table_extractor/tests/test_output_page_edit_js.py table_extractor/tests/test_html_assembler.py -v`

Expected: PASS with zero failures.

- [ ] **Step 5: Commit the text-wrapping fix**

Run: `git add table_extractor/templates/output_page_edit.js table_extractor/templates/output_page.css table_extractor/tests/test_output_page_edit_js.py docs/superpowers/plans/2026-07-12-crop-replacement.md && git commit -m "fix: wrap inline extraction edits"`
