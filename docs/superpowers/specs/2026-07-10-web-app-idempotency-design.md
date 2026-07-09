# Web App Idempotency Design

**Date:** 2026-07-10
**Scope:** crop_app (web app) only. CLI pipeline (`table_extractor/main.py`) is already cache-idempotent and is out of scope.

## Problem Statement

When the user loses internet connection during an analysis or HTML extraction run, there is no way to recover the pipeline from where it left off. The current behavior:

1. **Analysis**: LLM calls for page classification are not cached. The orchestration partially skips already-classified pages, but failed calls permanently mark pages as `"Complex"` with no way to distinguish failures from real classifications.
2. **HTML Extraction**: Individual crop extractions are cached to `.stage_cache/` by content hash, but all results accumulate in memory. The final HTML assembly (`write_page_files`) only runs after **all** crops complete. If the SSE connection drops before assembly, the user must start over — even though cache files exist on disk.
3. **No extraction state tracking**: `meta.json` has no fields for analysis or extraction status, so the app cannot determine if a pipeline was interrupted.
4. **No retry logic**: LLM API calls have no retry/backoff for transient failures (timeouts, 5xx, 429 rate limits).
5. **No resume UX**: The `/sessions` list shows no pipeline status indicators, so the user cannot identify or resume interrupted workflows.

## Design Goals

1. **Disk-based partial progress**: Every completed unit of work (classification, crop fragment) is persisted to disk immediately, not held in memory.
2. **Resume from any state**: After connection loss, server error, API key rotation, or credit exhaustion, the pipeline resumes from exactly where it left off.
3. **Error tracking**: Distinguish transient failures (retryable) from permanent failures (auth/credits) and surface actionable messages to the user.
4. **Backward compatible**: Existing sessions without new meta fields continue to work. New fields default to "pending" status.

---

## 1. Analysis Idempotency

### 1.1 New `analysis_status` Field

Each page in `meta.json` gains an `analysis_status` field:

| Value | Meaning |
|---|---|
| `"pending"` | Default. Not yet analyzed. |
| `"done"` | Successfully classified. `classification` is set. |
| `"error"` | LLM call failed. `analysis_error` contains the message. `classification` remains `null`. |

### 1.2 Error-Proof Analysis Flow

**Current bug**: `analyze_page()` catches exceptions and returns `{"classification": "Complex", "error": str(e)}`. This permanently marks failed pages as Complex — they can never be retried.

**Fix**:
- `analyze_page()` returns `{"classification": None, "error": str(e)}` on failure instead of falling back to "Complex"
- The `/analyze/<session_id>` endpoint:
  - Skips pages where `analysis_status == "done"`
  - Processes pages where `analysis_status` is `"pending"` or `"error"`
  - On success: sets `analysis_status = "done"`, `classification = result`
  - On failure: sets `analysis_status = "error"`, `analysis_error = message`, leaves `classification = null`

### 1.3 Analysis Call Caching

Wrap `analyze_page()` LLM calls with `cached_call` using:
- `stage = "analyze"`
- `model = MODEL_ID`
- `image_bytes` = page image bytes

This means:
- If a page was successfully analyzed before (even in a previous session of the same image), the cache hits — no API call needed
- Transient failures are NOT cached (success only) — so retry re-calls the API
- After 3 successful retries of the same page, subsequent resumes hit cache

### 1.4 `analyze_page()` Changes

The function signature stays the same:
```python
def analyze_page(image_path: str) -> dict:
    """Returns {"classification": "Simple"|"Complex"|None, "error": str|None}"""
```

The implementation changes:
- Wrap the API call body in `cached_call(stage="analyze", model=MODEL_ID, fn=lambda: _call_analysis_api(img_bytes), force=False)`
- On exception, return `{"classification": None, "error": str(e)}` (not "Complex")
- Apply the retry wrapper (Section 4) around `_call_analysis_api`

---

## 2. HTML Extraction Idempotency

### 2.1 Disk Fragment Layout

Fragments are saved under each session's upload directory:
```
uploads/<session_id>/
  extraction_fragments/
    page-0_crop-0.html     # Complex page 0, crop 0
    page-0_crop-1.html     # Complex page 0, crop 1
    page-1.html            # Simple page 1 (no crops)
```

Simple pages (classified as "Simple" or Complex with no crops) produce a single fragment at `page-{idx}.html`.

### 2.2 Per-Crop Status in `meta.json`

Each crop in `meta.json` gains:
```json
{
  "bbox": [0, 0, 1000, 1000],
  "filename": "crop_000.png",
  "extraction_status": "pending" | "extracted" | "failed",
  "extraction_error": null | "error message",
  "extraction_error_type": null | "retryable" | "auth" | "credits" | "other",
  "fragment_path": "extraction_fragments/page-0_crop-0.html"
}
```

Each page also gains:
```json
{
  "extraction_status": "pending" | "done",
  "extraction_error": null | "error message"
}
```

### 2.3 Extraction Retry Flow

When the SSE endpoint `/extract-progress/<session_id>` is called:

1. Read `meta.json` to determine current state
2. Build the task list of all crops that need extraction (complex crops) or whole-page extraction (simple pages)
3. For each task:
   - If `extraction_status == "extracted"` AND the fragment file exists on disk → **skip** (count it as already done)
   - Otherwise → submit to thread pool
4. Each worker:
   - Calls `extract_crop_as_html()` (with retry logic from Section 4)
   - Writes HTML fragment to `extraction_fragments/` using atomic rename (write to `.tmp`, rename to final)
   - Updates `extraction_status` and `fragment_path` in `meta.json` (under mutex)
   - Yields SSE progress event
5. After all workers complete:
   - If any crops have `extraction_status == "failed"` → yield `{"status": "error", "failed_crops": [...]}`
   - Otherwise → run assembly reading all fragments from disk → write output to `crop_app/static/extracted/<session_id>/` → yield `{"status": "done"}`

### 2.4 Assembly Reads from Disk

The assembly step no longer uses an in-memory results dict. Instead:
- Read fragment files from `extraction_fragments/` in `(page_idx, crop_idx)` order
- For each page, concatenate its crop fragments (or use the single page fragment for simple pages)
- Call `write_page_files()` with the assembled data
- The assembly step is idempotent: re-running it with the same fragments on disk produces the same output

### 2.5 Worker Write Protocol (Crash-Safe)

The write sequence per worker:
```
1. Extract HTML from LLM
2. Write to extraction_fragments/page-N_crop-M.html.tmp
3. os.replace(.tmp → final path)  # atomic on POSIX
4. Acquire meta_lock
5. Update meta.json (crop status = "extracted", fragment_path = "...")
6. Release meta_lock
7. Yield progress to SSE
```

The `meta_lock` is a `threading.Lock` attached to the `SessionManager` instance (or a module-level lock in the SSE endpoint handler).

---

## 3. SSE Reconnection

### 3.1 Reconnection Behavior

When a client reconnects to `/extract-progress/<session_id>` after disconnection:

- **Fresh extraction** (no fragments on disk): Normal extraction from scratch
- **Partial extraction** (some fragments exist, assembly not done):
  - Progress counter starts from existing completed count
  - SSE emits `{"status": "progress", "page": N, "totalPages": M, "log": "Resuming extraction: N of M already extracted..."}`
  - Only pending/failed crops are submitted to the thread pool
- **Assembly ready** (all fragments exist, output files don't):
  - Jump straight to assembly
  - No LLM calls needed
- **Complete** (output files exist in `crop_app/static/extracted/<session_id>/`):
  - Yield `{"status": "done"}` immediately

### 3.2 Progress Counter Logic

The progress counter reflects **total** completed work (pre-existing + just-completed), not just the current session's work. This gives the user a true picture of how close the pipeline is to completion.

### 3.3 Existing `cancel_event` Pattern Preserved

The existing `GeneratorExit → cancel_event.set()` pattern is unchanged. On disconnect:
- Running workers are NOT killed (they still write to disk)
- Only unstarted tasks are cancelled
- The meta.json state accurately reflects what completed

---

## 4. LLM Retry Logic

### 4.1 Shared Retry Utility

New module: `table_extractor/retry.py`

```python
def retry_with_backoff(fn, max_retries=3, base_delay=1.0, max_delay=30.0):
    """Execute fn() with exponential backoff retry on transient failures.
    
    Retries on: APITimeoutError, 5xx, 429, ConnectError, ReadError
    Does NOT retry on: 400, 401, 402, 403 (auth/credits/invalid)
    Respects Retry-After header on 429 responses.
    """
```

### 4.2 Error Classification

| Error Type | Status Code | Retry? | Error Type Tag |
|---|---|---|---|
| Timeout | N/A | Yes (backoff) | `"retryable"` |
| Server error | 5xx | Yes (backoff) | `"retryable"` |
| Rate limit | 429 | Yes (Retry-After) | `"retryable"` |
| Auth expired | 401 | No | `"auth"` |
| Insufficient credits | 402 | No | `"credits"` |
| Forbidden | 403 | No | `"auth"` |
| Bad request | 400 | No | `"other"` |

### 4.3 Error Type Propagation

When a crop extraction fails after all retries are exhausted:
- `meta.json` stores `extraction_error_type` alongside `extraction_error`
- The error type tag flows from `retry_with_backoff` exceptions up through the extraction pipeline
- The SSE error event includes the error type for the frontend to display actionable messages

### 4.4 Application Points

1. **`crop_app/llm.py` — `analyze_page()`**: Wraps `_call_analysis_api()` in `retry_with_backoff()`
2. **`table_extractor/html_extractor.py` — `extract_crop_as_html()`**: Wraps the inner `_call()` function (the lambda passed to `cached_call`) in `retry_with_backoff()`

### 4.5 `cached_call` Interaction

`cached_call` only writes to cache when `fn()` returns successfully. If all retries fail:
- No cache entry is written
- The exception propagates to the caller
- On resume, `cached_call` will retry the API call fresh (correct behavior)

### 4.6 Early Termination for Non-Retryable Errors

If the first crop extraction in a batch hits a non-retryable error (401, 402):
- The SSE stream immediately yields `{"status": "error", "type": "auth"|"credits", "message": "..."}`
- Remaining pending tasks are NOT submitted (no point hitting the same wall)
- The frontend displays the actionable message prominently

---

## 5. Non-Retryable Errors & Resume-After-Fix

### 5.1 Resume After Key Rotation / Credit Top-Up

The pipeline fully resumes from disk state after the underlying issue is resolved:

**Key rotation scenario**:
1. User edits `.env` with new API key
2. User restarts the Flask app (new key loaded from environment)
3. User navigates to the session's extraction page
4. Backend reads `meta.json`, finds crops with `extraction_status == "failed"` and `extraction_error_type == "auth"`
5. Those crops are re-submitted to the thread pool — new API key is used
6. Already-extracted crops: fragments on disk, skipped (no API call)
7. Assembly reads all fragments → complete output

**Credit top-up scenario** (key unchanged):
1. User tops up OpenRouter account
2. User clicks "Retry" on the extraction page (no restart needed)
3. Backend re-runs `run_extraction()` — failed crops get another chance
4. Same flow as above

### 5.2 Why This Works

All state lives on disk and persists across app restarts:
- **Fragments** in `extraction_fragments/` — the extracted HTML is durable
- **Status** in `meta.json` — tracks what's done, failed, or pending
- **Cache** in `.stage_cache/` — content-addressed LLM response cache

The `run_extraction()` function is designed to be called repeatedly. Each call:
1. Reads current state from `meta.json`
2. Determines what needs to run
3. Runs only what's needed
4. Persists all progress to disk immediately

---

## 6. Resume UX

### 6.1 Sessions List Page

Each session card shows two status indicators derived from `meta.json`:

**Analysis status**:
- "Not classified" — no pages have `analysis_status == "done"`
- "N of M pages classified" — some pages done
- "Done" — all pages have `analysis_status == "done"`
- "Partial (X errors)" — some pages have `analysis_status == "error"`

**Extraction status**:
- "No crops extracted" — no crops have `extraction_status == "extracted"`
- "N of M regions extracted" — partial extraction
- "Done" — all outputs exist on disk
- "Interrupted — N of M extracted, click to resume" — has partial fragments but no final output

Sessions with interrupted extraction show an amber badge: "Resume extraction".

### 6.2 Annotation Page

- The "Analyze" button remains clickable until all pages have `analysis_status == "done"`
- Pages with `analysis_status == "error"` show an error badge with the error message
- Clicking "Analyze" sends the request; server processes only `pending`/`error` pages
- No new UI elements needed beyond error badges

### 6.3 Extraction Page

On re-entry to `/extract-html/<session_id>`:

- **All crops extracted + output exists**: Redirect to extracted HTML viewer
- **All crops extracted + output missing**: Run assembly only, yield progress
- **Partial extraction**: Show progress page starting from completed count, resume remaining crops
- **No extraction started**: Normal fresh extraction
- **All failed with non-retryable error**: Show error banner with actionable message and "Retry" button

Progress bar shows "Resuming extraction: 12 of 20 regions already extracted..." when resuming.

---

## 7. Backward Compatibility

### 7.1 Missing Fields

Existing sessions without `analysis_status`, `extraction_status`, or crop status fields:
- Missing `analysis_status` → treated as `"pending"` if `classification` is null, or `"done"` if `classification` is set
- Missing `extraction_status` on page → treated as `"pending"`
- Missing `extraction_status` on crop → treated as `"pending"`

No migration script is needed. The new code reads old meta.json files and fills in status fields as work progresses.

### 7.2 No Schema Migration

The meta.json structure is additive. New fields are optional and fall through to defaults. Old sessions continue to render correctly.

---

## 8. File Change Summary

| File | Change |
|---|---|
| `crop_app/llm.py` | Wrap API call in `cached_call` + `retry_with_backoff()`. Return `None` classification on failure. |
| `crop_app/app.py` | Update `/analyze/` to use `analysis_status`. Update `/extract-progress/` for resume flow. Update `/sessions` for status display. |
| `crop_app/session_manager.py` | Add `get_extraction_fragments_dir()` helper. Add `meta_lock` for thread-safe meta writes. |
| `table_extractor/retry.py` | **NEW** — `retry_with_backoff()` utility with error classification. |
| `table_extractor/html_extractor.py` | Rewrite `run_extraction()` for disk-fragment-based resume flow. Wrap `extract_crop_as_html()` inner call in retry. |
| `crop_app/templates/sessions.html` | Add analysis/extraction status indicators to session cards. |
| `crop_app/templates/annotate.html` | Add error badges for analysis failures. |
| `crop_app/templates/extract_progress.html` | Handle resume states, error types, non-retryable error banners. |

---

## 9. Testing Approach

### 9.1 Unit Tests

- `test_retry.py`: Test `retry_with_backoff()` with mocked transient and non-retryable errors, verify Retry-After handling, verify error type tags
- `test_llm.py`: Test `analyze_page()` returns `None` on failure, not "Complex"
- `test_html_extractor.py`: Test fragment write protocol, test resume skipping extracted crops, test assembly from disk fragments

### 9.2 Integration Tests

- Mock LLM failure mid-extraction → verify partial state in meta.json → resume → verify complete output
- Mock 401 on first call → verify early termination + non-retryable error in SSE → "fix key" → resume → verify success
- Mock 429 rate limit → verify Retry-After delay → verify eventual success

### 9.3 Backward Compatibility Tests

- Load a pre-idempotency meta.json (no status fields) → verify analysis and extraction work correctly with defaults
