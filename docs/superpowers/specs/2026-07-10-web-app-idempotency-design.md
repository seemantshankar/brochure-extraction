# Web App Idempotency Design

**Date:** 2026-07-10 (revised)
**Scope:** crop_app (web app) only. CLI pipeline (`table_extractor/main.py`) is already cache-idempotent and is out of scope.

## Problem Statement

When the user loses internet connection during an analysis or HTML extraction run, there is no way to recover the pipeline from where it left off. The current behavior:

1. **Analysis**: LLM calls for page classification are not cached. Failed calls permanently mark pages as `"Complex"` with no way to distinguish failures from real classifications. Malformed model JSON is also returned as `"Complex"` — indistinguishable from a genuine classification.
2. **HTML Extraction**: Individual crop extractions are cached to `.stage_cache/` by content hash, but all results accumulate in memory. Final HTML assembly (`write_page_files`) only runs after **all** crops complete. If the SSE connection drops before assembly, the user must start over — even though cache files exist on disk.
3. **No extraction state tracking**: `meta.json` has no fields for analysis or extraction status, so the app cannot determine if a pipeline was interrupted.
4. **No retry logic**: LLM API calls have no retry/backoff for transient failures (timeouts, 5xx, 429 rate limits).
5. **Blank responses treated as valid**: Cheaper vision models occasionally return empty/blank HTML fragments. The pipeline caches these as successful results and assembles blank pages.
6. **Unsafe fragment identity**: Fragment file names derived from sorted crop index become stale when users add, delete, trim, or reposition crops.
7. **Crop mutations corrupt state**: Trim, delete, and commit operations change crop images and assembly structure but do not invalidate stale fragments or final output.
8. **No concurrent SSE protection**: Browser auto-reconnect can overlap extraction workers, causing duplicate LLM calls and competing assembly.
9. **Unsafe `meta.json` writes**: `save_meta()` writes directly to the file without atomic replace, risking corruption on crash. No session-scoped lock prevents thread/process races.
10. **No completion marker for output**: `write_page_files()` writes page files and `index.html` sequentially. A crash mid-write leaves a partial output directory that can be mistaken for complete.
11. **No resume UX**: The `/sessions` list shows no pipeline status indicators.
12. **Unbounded future submission**: All extraction futures are submitted to the executor at once, making cancellation ineffective and preventing clean abort on non-retryable errors.

## Design Goals

1. **Disk-based partial progress**: Every completed unit of work (classification, crop fragment) is persisted to disk immediately.
2. **Resume from any state**: After connection loss, server error, API key rotation, or credit exhaustion, the pipeline resumes from exactly where it left off.
3. **Error tracking**: Distinguish transient failures (retryable) from permanent failures (auth/credits) and surface actionable messages.
4. **Backward compatible**: Existing sessions without new meta fields continue to work. New fields default to "pending" status.
5. **Atomic state operations**: All `meta.json` mutations are crash-safe (temp file + `os.replace` under a session lock). Final output publication is atomic via a completion marker.
6. **Stable identity**: Fragment files use stable task IDs (crop filenames, page indices) that survive crop mutations.
7. **Lease-based concurrency**: Only one extraction job runs per session at a time. Concurrent/reconnected SSE requests are safe.

---

## 1. Task Identity & Schema

### 1.1 Stable Task IDs

Each extraction unit is a **task** with a stable ID that does not depend on sort order:

| Task Kind | `task_id` | Fragment Filename |
|---|---|---|
| Simple page (whole-page extraction) | `page-{page_idx}` | `page-{page_idx}.html` |
| Complex page crop | `crop_info["filename"]` without extension, e.g. `crop_003` | `crop_003.html` |

Crop filenames (`crop_000.png`, `crop_001.png`, ...) are **session-scoped unique counters** assigned during `/commit`. They survive bbox trimming (trim overwrites in place, filename unchanged) and only change when a crop is deleted and a new one is committed. Page indices (`page_idx`) are stable because page order is fixed by PDF page sequence.

**Display/assembly sort** is separate from identity:
- Fragments are stored by `task_id` (stable)
- Assembly reads `meta.json` to determine which tasks belong to each page and in what order, then reads the corresponding fragments
- Crop order within a page is determined by the user's crop list order in `meta.json` (vertical bbox order at annotation time), not by fragment filenames

### 1.2 Unified Task Schema in `meta.json`

Each page gains an `extraction_tasks` array. Every task — whether whole-page (Simple) or crop-level (Complex) — uses the same schema:

```json
{
  "extraction_tasks": [
    {
      "task_id": "crop_003",
      "page_idx": 0,
      "kind": "crop",
      "crop_filename": "crop_003.png",
      "extraction_status": "pending" | "extracted" | "failed",
      "extraction_error": null | "error message",
      "extraction_error_type": null | "retryable" | "auth" | "credits" | "malformed_output",
      "fragment_path": "extraction_fragments/crop_003.html"
    },
    {
      "task_id": "page-2",
      "page_idx": 2,
      "kind": "page",
      "extraction_status": "extracted",
      "extraction_error": null,
      "extraction_error_type": null,
      "fragment_path": "extraction_fragments/page-2.html"
    }
  ]
}
```

Each page *also* gains a page-level completion record:

```json
{
  "extraction_status": "pending" | "done",
  "extraction_error": null | "error message"
}
```

Page-level `extraction_status` transitions to `"done"` only after assembly writes `.complete` (see §5). A page with all tasks `"extracted"` but assembly not yet run remains `"pending"` at the page level.

### 1.3 Why Not Crop List Index

A previous draft used `page-N_crop-M.html` based on the crop's position in the sorted crop list. This is unsafe because:

- **Trim** modifies a crop in place — same filename, same index, but the fragment is stale.
- **Delete** removes a crop — all subsequent indices shift.
- **Add/Commit** inserts new crops — indices shift.
- **Reposition** (future) could reorder crops — indices change.

Using `crop_filename` (without `.png`) as the `task_id` ensures that the fragment path is a pure function of the crop's identity, not its position.

---

## 2. Crop Mutation Invalidation

### 2.1 Rules

Every crop mutation endpoint must invalidate affected extraction state **before** modifying meta.json:

| Endpoint | Fragment Action | Meta Action | Output Action |
|---|---|---|---|
| `/trim/<session_id>` | Delete fragment for the trimmed crop's `task_id` | Reset task: `extraction_status = "pending"`, clear `extraction_error`, `extraction_error_type`, `fragment_path` | Delete `.complete` marker in output dir |
| `/delete-crop/<session_id>` | Delete fragment for the deleted crop's `task_id` | Remove task from `extraction_tasks` | Delete `.complete` marker |
| `/commit/<session_id>` | No fragments to delete (new crops have no fragments) | Add new tasks with `extract_status = "pending"` | Delete `.complete` marker |

**Rationale**: The `.complete` marker is the sole signal of output readiness. Deleting it ensures the viewer route returns 404 until a fresh assembly runs. The fragment is deleted because its content was derived from the pre-mutation image.

### 2.2 Implementation Pattern

Each crop mutation handler follows this sequence (under the session lock from §5):

```python
with sm.session_lock(session_id):
    meta = sm.load_meta(session_id)
    _invalidate_task(meta, session_id, task_id)      # delete fragment, reset status
    _delete_output_marker(session_id)                 # delete .complete
    _apply_mutation(meta, ...)                        # trim/delete/commit
    sm.save_meta_atomic(session_id, meta)
```

---

## 3. Extraction Flow (Disk-Fragment Based)

### 3.1 Fragment Layout

```
uploads/<session_id>/
  extraction_fragments/
    page-0.html       # Simple page 0 (whole-page extraction)
    page-2.html       # Simple page 2
    crop_003.html     # Complex page 0, crop 3
    crop_007.html     # Complex page 1, crop 0
```

### 3.2 Extraction Execution

When the SSE endpoint `/extract-progress/<session_id>` is called:

1. **Acquire lease** (see §4). If another job is active for this session, poll its state from disk and yield status events without submitting new work.

2. **Read `meta.json`** and build the task list:
   - For each page:
     - If `classification == "Simple"` → one task of `kind: "page"` with `task_id: "page-{page_idx}"`
     - If `classification == "Complex"` and crops exist → one task per crop with `task_id: crop_filename`
     - If `classification == "Complex"` and no crops → one task of `kind: "page"` (treat as simple)
   - Tasks are matched against existing `extraction_tasks` in `meta.json` by `task_id`

3. **Determine what needs to run**:
   - For each task:
     - If a matching task exists in `meta.json` with `extraction_status == "extracted"` AND the fragment file exists on disk → **skip** (count as done)
     - Otherwise → needs extraction (submit to thread pool)

4. **Bounded submission with abort check** (see §7):
   - Submit tasks to the thread pool one at a time (or in small batches)
   - Before each submission, check: `cancel_event.is_set()` and `abort_flag`
   - If either is set, stop submitting, cancel any queued futures
   - Max in-flight tasks = `EXTRACTION_MAX_WORKERS` (default 4)

5. **Each worker**:
   - Calls `extract_crop_as_html()` wrapped in `retry_with_backoff()` (see §6)
   - If the task is `kind: "page"`, uses the whole page image; if `kind: "crop"`, uses the crop image
   - Writes HTML fragment atomically: write to `.tmp`, `os.replace` to `extraction_fragments/{task_id}.html`
   - Updates the matching task in `meta.json` under the session lock:
     - On success: `extraction_status = "extracted"`, `fragment_path = "extraction_fragments/{task_id}.html"`, clear errors
     - On failure: `extraction_status = "failed"`, `extraction_error = message`, `extraction_error_type = tag`
   - Yields SSE progress event

6. **After all workers complete**:
   - If `abort_flag` is set (non-retryable error detected): yield `{"status": "error", ...}` and return
   - If any tasks have `extraction_status == "failed"`, retryable: yield `{"status": "error", "failed_tasks": [...], "type": "retryable"}`
   - If all tasks are `"extracted"`: run assembly → write `.complete` marker → yield `{"status": "done"}`

7. **Release lease** when the generator exits (finally block).

### 3.3 Assembly Reads from Disk

Assembly no longer uses an in-memory results dict:

1. For each page in `meta.json` order:
   - Collect all tasks for this page in the order they appear in `meta.json` pages[i].extraction_tasks (or implicit crop order for crop tasks)
   - Read each task's fragment file from disk
   - For crop tasks: concatenate fragments in crop order (determined by `meta.json` crop list order / bbox y-sort)
   - For page tasks: use the single fragment as-is
2. Call `write_page_files()` with the assembled data (see §5.2 for atomic output)

### 3.4 Worker Write Protocol (Crash-Safe)

```
1. Extract HTML from LLM (with retry + blank detection)
2. Write to extraction_fragments/{task_id}.html.tmp
3. os.replace(.tmp → final path)  # atomic on POSIX
4. Acquire session lock
5. Update meta.json (task status = "extracted", fragment_path = "...")
6. Release session lock
7. Yield progress to SSE
```

---

## 4. Lease & Concurrent SSE Protection

### 4.1 Lease Model

A module-level dict `_active_jobs: dict[str, JobInfo]` tracks live extraction jobs:

```python
class JobInfo:
    session_id: str
    cancel_event: threading.Event
    abort_flag: threading.Event       # set on non-retryable error detected
    started_at: float
    generator: Generator              # reference for cleanup
```

### 4.2 Lease Acquisition

When `/extract-progress/<session_id>` is called:

1. Check `_active_jobs` for `session_id`
2. **If lease exists** (another extractor is running for this session):
   - Do NOT start a new extraction
   - Instead, enter a **pass-through polling mode**: read `meta.json` periodically and yield progress events derived from disk state
   - When the active job completes (check via `cancel_event.is_set()` or generator completion), yield the final status
   - Return to the frontend without having made any LLM calls
3. **If no lease exists**:
   - Create a new `JobInfo` with fresh `cancel_event` and `abort_flag`
   - Store in `_active_jobs`
   - Proceed with extraction (Section 3)

### 4.3 Lease Release

The lease is released in the generator's `finally` block:

```python
try:
    # extraction logic
    ...
finally:
    _active_jobs.pop(session_id, None)
```

### 4.4 Stale Lease Recovery

`_active_jobs` is process-local. On Flask process restart, the dict is empty. Stale leases are inherently recovered because:
- New requests find no active lease → start a fresh extraction
- The disk state (fragments in `extraction_fragments/`, `meta.json` task statuses) provides the true source of truth
- Already-completed fragments are skipped (Section 3.2 step 3)

A worker thread that outlives its lease (blocked in an LLM call) will eventually complete or fail. Its `meta.json` update will be a no-op on the next extraction (the task is already `extracted` or `failed`).

---

## 5. Atomic State Operations

### 5.1 `meta.json` Atomic Write API

`SessionManager` gains a session-scoped lock and an atomic update API:

```python
class SessionManager:
    def __init__(self, upload_dir, crop_dir):
        ...
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def update_meta(self, session_id: str, mutation_fn: Callable) -> dict:
        """Atomically read meta.json, apply mutation_fn, write back."""
        with self.session_lock(session_id):
            data = self.load_meta(session_id) or {}
            data = mutation_fn(data)
            self._save_meta_atomic(session_id, data)
            return data

    def _save_meta_atomic(self, session_id: str, data: dict) -> None:
        """Write meta.json via temp file + os.replace."""
        session_dir = os.path.join(self.upload_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        meta_path = os.path.join(session_dir, "meta.json")
        fd, tmp_path = tempfile.mkstemp(dir=session_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, meta_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
```

**Why this is safe**:
- The session lock prevents concurrent read-modify-write races between workers and the SSE generator
- `tempfile.mkstemp` + `os.replace` ensures atomic write at the filesystem level (POSIX)
- If the process crashes mid-write, the `.tmp` file is abandoned; the old `meta.json` remains intact
- The existing `save_meta()` is replaced by `_save_meta_atomic()` everywhere

### 5.2 Immediate Analysis Result Persistence

The analysis endpoint persists each page result immediately — not batched at the end:

```python
for page_idx, page_info in enumerate(pages):
    if meta["pages"][page_idx].get("analysis_status") == "done":
        continue  # skip already-analyzed
    result = analyze_page(image_path)
    def _apply(m):
        m["pages"][page_idx]["classification"] = result["classification"]
        m["pages"][page_idx]["analysis_status"] = "done" if result["error"] is None else "error"
        if result["error"]:
            m["pages"][page_idx]["analysis_error"] = result["error"]
        return m
    sm.update_meta(session_id, _apply)
    # yield SSE progress ...
```

### 5.3 Output Publication via Completion Marker

Output is considered complete only when a `.complete` marker exists in the output directory. This marker is written **atomically** after all page files and `index.html` are written:

```python
def write_page_files(session_id, pages_data, title, output_root=None):
    ...
    # Write page files
    for i, pdata in enumerate(pages_data):
        out_path = os.path.join(session_dir, f"page-{i}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page_html)

    # Write index.html
    with open(os.path.join(session_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    # Write completion marker (atomic)
    marker = {"pages": len(pages_data), "timestamp": time.time()}
    marker_path = os.path.join(session_dir, ".complete")
    fd, tmp = tempfile.mkstemp(dir=session_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(marker, f)
        os.replace(tmp, marker_path)
    except BaseException:
        os.unlink(tmp)
        raise
```

**Completion checks**:
- The viewer route `/extracted/<session_id>/extraction.html`: checks for `.complete` in addition to `index.html` existence
- Assembly readiness: all tasks `extracted` + fragments on disk → run assembly → write `.complete`
- Output resume: if `.complete` exists, skip extraction entirely and yield `{"status": "done"}`

---

## 6. LLM Exception Hierarchy & Retry Logic

### 6.1 Exception Classes

New module: `table_extractor/retry.py`

```python
from openai import (
    APIStatusError, APIConnectionError, APITimeoutError,
    RateLimitError, AuthenticationError, PermissionDeniedError,
)

class PipelineCallError(Exception):
    """Base for pipeline LLM call failures."""
    def __init__(self, error_type: str, message: str, cause: Exception = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.cause = cause

class RetryableError(PipelineCallError):
    """Transient error; caller should retry with backoff."""
    def __init__(self, message: str, cause: Exception = None, retry_after: float = None):
        super().__init__("retryable", message, cause)
        self.retry_after = retry_after  # seconds, or None for exponential backoff

class NonRetryableError(PipelineCallError):
    """Permanent error; caller must not retry."""
    pass

class AuthError(NonRetryableError):
    def __init__(self, message: str, cause: Exception = None):
        super().__init__("auth", message, cause)

class CreditsExhaustedError(NonRetryableError):
    def __init__(self, message: str, cause: Exception = None):
        super().__init__("credits", message, cause)

class BlankResponseError(RetryableError):
    """LLM returned empty/blank output."""
    pass

class MalformedOutputError(RetryableError):
    """LLM output could not be parsed into valid JSON/expected structure."""
    pass
```

### 6.2 Error Classification

```python
def classify_api_error(exc: Exception) -> PipelineCallError:
    """Classify an OpenAI SDK exception into a PipelineCallError."""
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return RetryableError(f"Connection/timeout error: {exc}", cause=exc)
    elif isinstance(exc, RateLimitError):
        retry_after = _parse_retry_after(exc.response)
        return RetryableError(f"Rate limited: {exc}", cause=exc, retry_after=retry_after)
    elif isinstance(exc, AuthenticationError):
        return AuthError(f"Authentication failed: {exc}", cause=exc)
    elif isinstance(exc, PermissionDeniedError):
        return AuthError(f"Permission denied: {exc}", cause=exc)
    elif isinstance(exc, APIStatusError):
        sc = exc.status_code
        if 500 <= sc < 600:
            return RetryableError(f"Server error ({sc}): {exc}", cause=exc)
        elif sc == 429:
            retry_after = _parse_retry_after(exc.response)
            return RetryableError(f"Rate limited ({sc}): {exc}", cause=exc, retry_after=retry_after)
        elif sc == 402:
            return CreditsExhaustedError(f"Insufficient credits: {exc}", cause=exc)
        else:
            return NonRetryableError(f"API error ({sc}): {exc}", cause=exc)
    else:
        return NonRetryableError(f"Unexpected error: {exc}", cause=exc)

def _parse_retry_after(response) -> float | None:
    """Parse Retry-After header (seconds) from an HTTP response, capped at max_delay."""
    if response is None:
        return None
    val = response.headers.get("Retry-After")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
```

### 6.3 Retry with Backoff

```python
def retry_with_backoff(
    fn: Callable,
    *,
    max_attempts: int = 3,     # TOTAL attempts (1 initial + 2 retries). NOT retries.
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.5,       # random.random() * jitter added to each delay
):
    """Execute fn() with exponential backoff on RetryableError.

    - max_attempts=3 means at most 3 total attempts (initial + 2 retries).
    - Non-retryable errors propagate immediately (no retry).
    - Retry-After header is respected if present (capped at max_delay).
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except RetryableError as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise  # exhausted
            if e.retry_after is not None:
                delay = min(e.retry_after, max_delay)
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.random() * jitter
            time.sleep(delay)
        except NonRetryableError as e:
            raise  # never retry
        except Exception as e:
            # Classify unknown exceptions
            classified = classify_api_error(e)
            if isinstance(classified, RetryableError):
                last_exc = classified
                if attempt == max_attempts - 1:
                    raise classified
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.random() * jitter
                time.sleep(delay)
            else:
                raise classified
```

### 6.4 `cached_call` Interaction

`cached_call` only writes to cache when `fn()` returns successfully. If all retries raise:
- No cache entry is written
- The exception propagates
- On resume, `cached_call` retries fresh (correct)

**Stale blank cache entries** (from before blank detection): see §8.3.

### 6.5 Application Points

1. **`crop_app/llm.py` — `analyze_page()`**: Wraps the inner API call function (after image conversion) in `retry_with_backoff()`. Malformed JSON from the model raises `MalformedOutputError` (retryable) instead of returning `"Complex"` with error.

2. **`table_extractor/html_extractor.py` — `extract_crop_as_html()`**: The `_call()` closure is wrapped in `retry_with_backoff()`. Blank response detection raises `BlankResponseError` (retryable). After `cached_call` returns, a post-check handles stale cached blanks (see §8.3).

---

## 7. Bounded Submission & Non-Retryable Error Handling

### 7.1 Bounded Submission Pattern

Instead of submitting all futures at once, tasks are submitted lazily with a bounded in-flight count:

```python
import threading

max_workers = min(EXTRACTION_MAX_WORKERS, len(tasks))
semaphore = threading.Semaphore(max_workers)
abort_flag = threading.Event()
completed_count = {"count": pre_existing_completed}
futures = []

for task in tasks_to_submit:
    if cancel_event.is_set() or abort_flag.is_set():
        break
    semaphore.acquire()
    future = executor.submit(_run_task_with_semaphore, task, semaphore, ...)
    futures.append(future)
```

The semaphore limits concurrent LLM calls to `max_workers`. Between each `executor.submit()`, we check `cancel_event` (disconnect) and `abort_flag` (non-retryable error).

### 7.2 Non-Retryable Error Detection

When a worker detects a non-retryable error (e.g., 401/402):
1. The worker sets `abort_flag = True`
2. The worker still persists its failure to `meta.json`
3. The main submission loop sees `abort_flag.is_set()` → stops submitting more tasks
4. All already-submitted futures are allowed to complete (or cancelled if not started)
5. After all submitted futures finish:
   - Persist any in-flight results
   - Yield `{"status": "error", "type": "auth"|"credits", "message": "..."}` as the terminal event
   - **Never assemble** while any task has `extraction_status == "failed"`
   - Release the lease

### 7.3 Cancellation & Draining

When `cancel_event` is set (SSE disconnect):
1. The submission loop stops submitting
2. All unsubmitted tasks remain `pending` in `meta.json`
3. Already-submitted futures are cancelled via `future.cancel()`
4. In-flight futures (already running) are allowed to complete — their results are persisted to `meta.json` and fragments written to disk
5. The generator exits without emitting `"done"` or `"error"` (the client already disconnected)
6. On reconnect, the new SSE request finds partial state on disk and resumes (Section 3.2)

### 7.4 No Output After Cancellation

The SSE generator guarantees: after `cancel_event.is_set()` is detected, no further events are yielded to the stream. Assembly is never triggered on cancellation — only successful full completions trigger assembly.

---

## 8. Blank Response Detection

### 8.1 Detection Logic

```python
def is_blank_fragment(fragment: str) -> bool:
    """Return True if the fragment carries no meaningful content."""
    if not fragment or not fragment.strip():
        return True
    return False
```

### 8.2 `_call()` Raises `BlankResponseError`

Inside `extract_crop_as_html()`, before returning to `cached_call`:

```python
def _call():
    ...
    raw_content = response.choices[0].message.content or ""
    html_fragment = clean_up_html_fragment(raw_content)
    if is_blank_fragment(html_fragment):
        raise BlankResponseError("LLM returned an empty/blank HTML fragment")
    return [html_fragment, usage_meta]
```

Because `BlankResponseError` extends `RetryableError`, `retry_with_backoff()` catches it and retries with backoff. After exhaustion, the exception propagates — `cached_call` does not write a blank entry.

### 8.3 Stale Blank Cache Invalidation

Existing blank entries may be in `.stage_cache/` from before detection was implemented. After `cached_call` returns (from cache hit), check the fragment:

```python
result = cached_call(fn=lambda: retry_with_backoff(_call), ...)
if is_blank_fragment(result[0]):
    # Stale cache bypass: force re-execute to replace the blank cache entry
    result = cached_call(fn=lambda: retry_with_backoff(_call), ..., force=True)
return result[0]
```

- If cache returned non-blank: returned immediately (no API call)
- If cache returned stale blank: `force=True` bypasses cache → `_call()` detects blank → `retry_with_backoff` retries → success overwrites stale cache, or exhaustion propagates `BlankResponseError`

---

## 9. Analysis Idempotency

### 9.1 `analysis_status` Field

Each page in `meta.json` gains:

| Value | Meaning |
|---|---|
| `"pending"` | Default. Not yet analyzed. |
| `"done"` | Successfully classified. `classification` is set to `"Simple"` or `"Complex"`. |
| `"error"` | LLM call failed or returned unparseable output. `analysis_error` contains the message. `classification` remains `null`. |

### 9.2 Error-Proof Analysis Flow

**Current bugs**:
1. `analyze_page()` catches exceptions and returns `{"classification": "Complex", "error": str(e)}` → permanent false Complex
2. Malformed model JSON returns `{"classification": "Complex", "error": ...}` → indistinguishable from genuine Complex

**Fixes**:
1. `analyze_page()` returns `{"classification": None, "error": str(e)}` on API failure
2. Malformed JSON raises `MalformedOutputError` (retryable) — if all retries fail, returns `{"classification": None, "error": "Malformed output: ..."}`
3. Per-page results are persisted to `meta.json` immediately via `sm.update_meta()` (not batched)

### 9.3 Analysis Call Caching

```python
def analyze_page(image_path: str) -> dict:
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Encode as JPEG quality=85 — the EXACT bytes sent to the API
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    image_bytes = buf.getvalue()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    def _call_api():
        """Inner API call, wrapped in retry_with_backoff."""
        response = _get_client().chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": ANALYSIS_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]}
            ],
            reasoning_effort="minimal",
        )
        raw_content = response.choices[0].message.content or ""
        return _parse_response_strict(raw_content)

    try:
        result = cached_call(
            image_bytes=image_bytes,        # JPEG bytes — what's actually sent
            stage="analyze",
            model=MODEL_ID,
            fn=lambda: [retry_with_backoff(_call_api)],
            force=False,
            extra_key=ANALYSIS_PROMPT,       # prompt hash ensures cache invalidates on prompt change
        )
        return result[0]
    except PipelineCallError as e:
        return {"classification": None, "error": e.message}
    except Exception as e:
        return {"classification": None, "error": str(e)}

def _parse_response_strict(raw: str) -> dict:
    """Parse LLM JSON response. Raises MalformedOutputError on failure."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise MalformedOutputError(f"Failed to parse JSON: {raw[:200]}")

    if not isinstance(data, dict):
        raise MalformedOutputError(f"Expected JSON object: {raw[:200]}")

    classification = data.get("classification")
    if classification not in ("Simple", "Complex"):
        raise MalformedOutputError(f"Invalid classification value: {raw[:200]}")

    return {"classification": classification, "error": None}
```

Key changes:
- `image_bytes` is the JPEG bytes actually sent to the model (not the raw PNG)
- `extra_key=ANALYSIS_PROMPT` ensures cache invalidates if the prompt changes
- `_parse_response_strict` raises `MalformedOutputError` (retryable) instead of returning `"Complex"` — the retry wrapper will attempt to get valid JSON, and only after exhaustion does it propagate as an error
- Cache stores `{"classification": "Simple"|"Complex"}` only after a successful parse

---

## 10. Extraction Eligibility

### 10.1 The Decision

**All-Simple sessions MAY run extraction.** A session where all pages are `"Simple"` extracts each full page image directly — no crops are needed. The previous route prerequisite that required "at least one committed crop" is removed.

### 10.2 What Changes

- The `/extract-html/<session_id>` route check `if not any(page.get("crops") for page in meta.get("pages", []))` is **removed**
- The prerequisite becomes: all pages must have `analysis_status == "done"` (no pending/errors)
- If any page still has `analysis_status == "pending"` or `"error"`, the route returns a 400 with a message directing the user to analyze first

### 10.3 Task Construction for All-Simple

If all pages are Simple:
- Each page produces one task with `kind: "page"`, `task_id: "page-{idx}"`
- The task uses the full page image (same as Simple extraction today)
- No crops are needed; the full-page LLM call extracts the whole page

---

## 11. Resume UX

### 11.1 Sessions List Page

Each session card shows two status indicators derived from `meta.json`:

**Analysis status**:
- "Not classified" — no pages have `analysis_status == "done"`
- "N of M pages classified" — some pages done
- "Done" — all pages have `analysis_status == "done"`
- "Partial (X errors)" — some pages have `analysis_status == "error"`

**Extraction status** (derived from `extraction_tasks`):
- "No tasks extracted" — no tasks have `extraction_status == "extracted"`
- "N of M tasks extracted" — partial extraction
- "Done" — `.complete` marker exists in output dir
- "Interrupted — N of M extracted, click to resume" — has partial fragments but no `.complete`

Sessions with interrupted extraction show an amber badge: "Resume extraction".

### 11.2 Annotation Page

- "Analyze" button remains clickable until all pages have `analysis_status == "done"`
- Pages with `analysis_status == "error"` show an error badge with the error message
- Clicking "Analyze" processes only `pending`/`error` pages

### 11.3 Extraction Page

On re-entry, determined by `.complete` and task statuses:
- **`.complete` exists**: Redirect to `/extracted/<session_id>/extraction.html`
- **All tasks `extracted`, no `.complete`**: Run assembly only, yield progress
- **Partial tasks**: Show progress page, resume remaining tasks
- **No extraction started** (or all tasks `pending`): Normal fresh extraction
- **All failed with non-retryable error**: Show error banner with actionable message and "Retry" button. **Retry requires explicit user action** — navigating to the page does not auto-retry.

---

## 12. State Reconciliation

### 12.1 Status/Filesystem Disagreement Rules

When the extraction flow starts, discrepancies between `meta.json` task statuses and the actual filesystem state must be resolved before any extraction work begins:

| `meta.json` Status | Fragment File Exists? | Resolution |
|---|---|---|
| `"extracted"` | **No** | Reset to `"pending"`. The fragment was lost; re-extract. |
| `"pending"` or `"failed"` | **Yes** | Use the existing fragment. Mark as `"extracted"`, set `fragment_path`. **Do not** re-extract. |
| `"extracted"` | **Yes**, fragment | Trust it. Skip. |
| `"pending"` | **No** | Normal: needs extraction. |
| `"failed"` | **No** | Normal: retry. |

### 12.2 Output-Level Reconciliation

| Output `.complete` exists? | All tasks `extracted`? | Resolution |
|---|---|---|
| Yes | All extracted | Trust as complete. Yield `{"status": "done"}`. |
| Yes | Some failed/pending | **Stale output** — delete `.complete`, re-run assembly with current valid fragments. |
| No | All extracted | Run assembly, write `.complete`. |
| No | Some incomplete | Normal extraction/resume. |

### 12.3 Legacy Normalization

On first access of a legacy `meta.json` (no `analysis_status`, no `extraction_tasks`):

1. **Analysis**:
   - If `classification` is set (`"Simple"` or `"Complex"`): set `analysis_status = "done"`
   - If `classification` is `null` and `complex: true`: set `analysis_status = "done"`, `classification = "Complex"` (legacy behavior)
   - If no `classification` and no `complex`: set `analysis_status = "pending"`

2. **Extraction**:
   - If no `extraction_tasks` key: construct the task list from current pages and crops (see §3.2). All tasks start as `"pending"` unless a fragment file exists on disk (apply §12.1 row 2).

3. **Output**:
   - If `.complete` exists but no `extraction_tasks`: treat as stale output — delete `.complete`

This normalization happens at the start of any route that reads `meta.json` for extraction/analysis purposes.

---

## 13. SSE Reconnection

### 13.1 Behavior

When a client reconnects to `/extract-progress/<session_id>`:

- **Job already running** (lease exists): See §4.2 pass-through polling mode
- **No active job, `.complete` exists**: Yield `{"status": "done"}` immediately
- **No active job, partial fragments**: Start extraction, count pre-existing tasks as done, resume remaining
- **No active job, all tasks `extracted`, no `.complete`**: Run assembly only
- **No active job, no state**: Fresh extraction from scratch

### 13.2 Progress Counter

The progress counter reflects **total** completed work (pre-existing + just-completed in this run). On resume:
- Count tasks with `extraction_status == "extracted"` as already done
- Progress starts at that count, increments as new tasks complete

---

## 14. Backward Compatibility

### 14.1 Missing Fields

| Missing Field | Default Behavior |
|---|---|
| `analysis_status` | `"done"` if `classification` set or `complex: true`; else `"pending"` |
| `extraction_status` on page | `"pending"` |
| `extraction_status` on crop/task | `"pending"` |
| `extraction_tasks` on page | Construct from current pages + crops (see §12.3) |
| `.complete` marker | No output — assembly needed |

No migration script is needed. The normalization logic (§12.3) handles legacy data on first access.

### 14.2 No Schema Migration

New fields are additive. Old sessions continue to work. The reconciliation rules (§12) handle edge cases where status and filesystem disagree.

---

## 15. Non-Retryable Errors & Resume-After-Fix

### 15.1 Resume After Key Rotation / Credit Top-Up

The pipeline fully resumes from disk state:

**Key rotation**:
1. User edits `.env` with new API key
2. User restarts Flask app
3. User navigates to the session's extraction page
4. Backend reads `meta.json`, finds tasks with `extraction_status == "failed"` and `extraction_error_type == "auth"`
5. Failed tasks are re-submitted — new API key is used
6. Already-extracted tasks: fragments on disk, skipped
7. Assembly reads all fragments → `.complete` marker → done

**Credit top-up** (key unchanged):
1. User tops up OpenRouter account
2. User clicks "Retry" on extraction page (**explicit action required**)
3. Same flow as above

### 15.2 Why This Works

All state lives on disk and persists across app restarts:
- **Fragments** in `extraction_fragments/` — the extracted HTML is durable
- **Task status** in `meta.json` — tracks what's done, failed, or pending
- **Completion marker** `.complete` — atomic signal of output readiness
- **Cache** in `.stage_cache/` — content-addressed LLM response cache

---

## 16. File Change Summary

| File | Change |
|---|---|
| `crop_app/llm.py` | Wrap API call in `cached_call` + `retry_with_backoff()`. Cache JPEG bytes + `ANALYSIS_PROMPT`. `_parse_response_strict()` raises `MalformedOutputError` on malformed JSON. Persist results immediately. |
| `crop_app/app.py` | Use `sm.update_meta()` everywhere. Add lease check to SSE endpoint. Update `/extract-html/` prerequisite (remove crop check, require analysis done). Add `/analyze/` immediate persistence. Update `/trim/`, `/delete-crop/`, `/commit/` with invalidation. Update viewer route to check `.complete`. |
| `crop_app/session_manager.py` | Add `session_lock()`, `update_meta()`, `_save_meta_atomic()`. Keep existing methods. |
| `table_extractor/retry.py` | **NEW** — Full exception hierarchy (`PipelineCallError`, `RetryableError`, `NonRetryableError`, `AuthError`, `CreditsExhaustedError`, `BlankResponseError`, `MalformedOutputError`). `retry_with_backoff()` with `max_attempts` semantics, jitter, Retry-After parsing. `classify_api_error()`. `is_blank_fragment()`. |
| `table_extractor/html_extractor.py` | Rewrite `run_extraction()` for: lease-based concurrency (§4), bounded submission (§7), unified task schema (§1.2), fragment identity by task_id (§1.1), atomic fragment writes, `_call()` raises `BlankResponseError`, stale cache bypass, reconciliation (§12). |
| `table_extractor/html_assembler.py` | `write_page_files()` writes `.complete` marker atomically after all files. |
| `crop_app/templates/sessions.html` | Analysis/extraction status indicators from task-level data. |
| `crop_app/templates/annotate.html` | Error badges for analysis failures. |
| `crop_app/templates/extract_progress.html` | Handle resume states, error types, non-retryable error banners, explicit retry button. |

---

## 17. Testing Approach

### 17.1 Unit Tests

- `test_retry.py`:
  - `retry_with_backoff` with mocked transient errors → verify backoff timing, jitter, max_attempts
  - `retry_with_backoff` with mocked non-retryable error → verify immediate propagation (no retry)
  - `classify_api_error` for each OpenAI exception type
  - `is_blank_fragment` with empty, whitespace, valid fragments
  - `BlankResponseError` retry and exhaustion
  - `MalformedOutputError` triggered by unparseable JSON

- `test_llm.py`:
  - `analyze_page` returns `None` classification on API failure (not "Complex")
  - `analyze_page` raises `MalformedOutputError` on unparseable model output → propagates as retryable error
  - Cache key uses JPEG bytes (not PNG)

- `test_html_extractor.py`:
  - Fragment write protocol: atomic via `.tmp` + `os.replace`
  - Resume skipping tasks with `extracted` status + existing fragment
  - Assembly reads fragments from disk by `task_id`
  - Task ID stability: crop fragment filename = crop filename (not sort index)
  - Blank response retry: mock blank → `BlankResponseError` → retry → success
  - Stale cache bypass: pre-populate cache with blank → `force=True` → valid content replaces

- `test_session_manager.py`:
  - `update_meta` atomic write: verify `os.replace` path
  - `update_meta` concurrent: two threads racing → final state consistent
  - `session_lock` isolation between different sessions

- `test_crop_invalidations.py`:
  - `/trim` deletes fragment + resets task + deletes `.complete`
  - `/delete-crop` removes task + deletes fragment + deletes `.complete`
  - `/commit` adds tasks + deletes `.complete`

### 17.2 Integration Tests

- Mock LLM failure mid-extraction → verify partial task state → resume → verify complete output with `.complete` marker
- Mock 401 → verify bounded submission stops → abort → persist results → terminal error → fix key → resume → verify success
- Mock 429 → verify Retry-After delay → verify eventual success
- Mock blank response → verify retry → verify success → verify fragment written
- Pre-populate cache with blank entry → verify `force=True` bypass → verify cache overwritten
- Crop trim → extract → verify new fragment differs from pre-trim fragment
- Concurrent SSE requests → verify only one active job → second request polls
- Cancel during extraction → verify in-flight workers complete → verify meta.json accurate → resume → complete
- Crash during `meta.json` write → verify `.tmp` abandoned → verify old `meta.json` intact on next load
- Crash during `write_page_files` → verify `.complete` not written → verify resume re-runs assembly
- Load legacy `meta.json` → verify normalization produces correct default statuses
- All-Simple session → verify extraction runs without committed crops
- `extracted` + missing fragment → verify task reset to `pending` on resume
- Fragment exists + `pending` status → verify task used as-is and marked `extracted`
- `.complete` exists but tasks incomplete → verify output treated as stale, `.complete` deleted

### 17.3 Edge Cases

- Process death and restart: verify disk-state-only recovery, no stale lease
- Network disconnect and reconnect: verify resume from disk state
- Same image, different prompt → verify cache miss (prompt in extra_key)
- `max_attempts=3` semantics → verify exactly 3 total calls, not 3 retries
