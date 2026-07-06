# Branch-Level Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement final branch-level fixes for the Brochure Table Extraction codebase, including handling nested column headers, improving footnote regex correctness, tracking detection cost metrics, verifying technical drawings, skipping container region extraction, and standardizing file I/O encodings.

**Architecture:** Use modular helpers in `reconcile.py` to recursively flatten columns and resolve cell values safely. Update regex patterns to strip markdown formatting and match boundaries. Update `detect.py` and `extract.py` with process-level usage dictionaries. Update `main.py` control flow and environment validations.

**Tech Stack:** Python 3.x, PIL, OpenAI API, Pydantic, Pytest

## Global Constraints

- Preserve all existing comments and docstrings that are unrelated to your code changes.
- Specify `encoding="utf-8"` in all built-in `open()` calls.
- Track OpenRouter API costs using exact or estimated metrics.

---

### Task 1: Reconcile Module Improvements (Column Headers, Footnote Regex, Technical Drawings, Determinism)

**Files:**
- Modify: `table_extractor/reconcile.py`
- Test: `table_extractor/tests/test_reconcile.py`

**Interfaces:**
- Consumes: `Region`, `RegionType`, `ExtractedContent` from `table_extractor.schemas`
- Produces: `flatten_columns(columns: list) -> list[str]`, `resolve_footnotes`, `self_consistency_check`, `_table_to_pipe_table`

- [ ] **Step 1: Implement column flattening and retrieval helpers in `reconcile.py`**
  Add `flatten_columns(columns: list) -> list[str]` and `_get_row_value(row: dict, col: str) -> str` to handle nested headers and dictionary traversal safely. Update `_table_to_pipe_table` and `resolve_footnotes` to use them.

- [ ] **Step 2: Refine footnote marker regex in `_extract_markers_from_value`**
  Modify `_extract_markers_from_value` to strip markdown bolding `**`, italics `*`, and links `[...]` before finding matches, and use `(?<=[a-zA-Z0-9])([\*\^\#]+)(?![a-zA-Z0-9])` to match markers attached to words/numbers.

- [ ] **Step 3: Update `self_consistency_check` to include technical drawings and deterministic sampling**
  Add `drawing_json` to `primary_data` and `verify_data` checks, and replace `random.sample` with `random.Random(42).sample`.

- [ ] **Step 4: Update `test_reconcile.py`**
  Add tests for column flattening, the refined footnote regex, and technical drawing checks in consistency verification.

---

### Task 2: Cost and Token Usage Tracking for Detection and Extraction

**Files:**
- Modify: `table_extractor/detect.py`
- Modify: `table_extractor/extract.py`
- Modify: `table_extractor/main.py`
- Test: `table_extractor/tests/test_detect.py`, `table_extractor/tests/test_extract.py`

**Interfaces:**
- Consumes: API response object `response.usage`
- Produces: `get_total_usage() -> dict` in both `detect.py` and `extract.py`

- [ ] **Step 1: Add usage tracking in `detect.py`**
  Add `total_detection_usage` module-level dict. Accumulate prompt, completion tokens, and cost inside `_api_call`. Implement `get_total_usage()`.

- [ ] **Step 2: Add usage tracking in `extract.py`**
  Add `total_extraction_usage` module-level dict. Accumulate prompt, completion tokens, and cost inside `_call_extraction_api`. Implement `get_total_usage()`.

- [ ] **Step 3: Print totals in `main.py`**
  Retrieve and print total detection and extraction usages at the end of `main.py`.

---

### Task 3: Ingestion, Extraction Skipping, Startup Checks, and Encodings

**Files:**
- Modify: `table_extractor/main.py`
- Modify: `table_extractor/cache.py`
- Modify: `table_extractor/detect.py`
- Modify: `table_extractor/extract.py`
- Modify: `table_extractor/tests/test_main.py`

- [ ] **Step 1: Check environment variables on startup**
  In `main.py`, check `OPENROUTER_API_KEY` at the start of `main()` and raise `ValueError` if missing.

- [ ] **Step 2: Skip extraction for container regions**
  In `main.py`'s `run_pipeline`, skip content extraction for regions where `may_contain_subregions` is `True`.

- [ ] **Step 3: Specify `encoding="utf-8"` for all python `open()` calls**
  Update `main.py`, `detect.py`, `extract.py`, `cache.py`, and `test_main.py` to specify `encoding="utf-8"`.

- [ ] **Step 4: Update test assertions in `test_main.py`**
  Adjust the expected call count of `mock_extract_content` because container regions are skipped.

---
