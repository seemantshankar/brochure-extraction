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

**Contract: Persistent `next_crop_id`**

Crop filenames are assigned from a persistent counter stored in `meta.json`:

```json
{
  "session_id": "abc-def",
  "pages": [...],
  "next_crop_id": 3
}
```

When `/commit` creates a new crop:
1. Read `next_crop_id` from meta.json
2. Create filename: `crop_{next_crop_id:03d}.png`
3. Increment `next_crop_id` by 1
4. Persist to meta.json

**Why this is stable**: The counter never decreases. Deleting a crop does not decrement the counter. This prevents ID reuse collisions.

**Display/assembly sort**: Crops within a page are sorted by vertical bbox position (y0 coordinate) during extraction and assembly. This ensures visual order (top-to-bottom) regardless of insertion order. The crop list in meta.json preserves insertion order for display purposes, but extraction/assembly always applies bbox sort before creating tasks.

### 1.2 Unified Task Schema in `meta.json`

`meta.json` gains a **top-level** `extraction_tasks` array. Every extraction unit — whether whole-page (Simple) or crop-level (Complex) — uses the same flat schema:

```json
{
  "files": ["brochure.pdf"],
  "pages": [...],
  "next_crop_id": 4,
  "extraction_tasks": [
    {
      "task_id": "page-1",
      "page_idx": 1,
      "kind": "page",
      "extraction_status": "pending" | "extracted" | "failed",
      "extraction_error": null | "error message",
      "extraction_error_type": null | "retryable" | "auth" | "credits" | "malformed_output",
      "fragment_path": null | "extraction_fragments/page-1.html"
    },
    {
      "task_id": "crop_003",
      "page_idx": 0,
      "kind": "crop",
      "crop_filename": "crop_003.png",
      "extraction_status": "extracted",
      "extraction_error": null,
      "extraction_error_type": null,
      "fragment_path": "extraction_fragments/crop_003.html"
    }
  ]
}
```

There is no per-page `extraction_status` field. Page-level completion is derived from the task set: a page is complete when **all** its tasks have `"extracted"` status and the corresponding fragment files exist on disk. The session is complete when all tasks are extracted, assembly has run, and the `.complete` marker exists. This eliminates schema drift — one set of tasks is the sole authority on extraction progress.

### 1.3 Why Not Crop List Index

A previous draft used `page-N_crop-M.html` based on the crop's position in the sorted crop list. This is unsafe because:

- **Trim** modifies a crop in place — same filename, same index, but the fragment is stale.
- **Delete** removes a crop — all subsequent indices shift.
- **Add/Commit** inserts new crops — indices shift.
- **Reposition** (future) could reorder crops — indices change.

Using `crop_filename` (without `.png`) as the `task_id` ensures that the fragment path is a pure function of the crop's identity, not its position.

---

## 2. Canonical Reconciliation

### 2.1 The Reconciliation Function

A single pure function derives the **exact** required task set from current state. It does not touch `meta.json`, fragments, or `.complete`. It returns what the task list *should* look like given the current classifications and crop geometry.

```python
def derive_required_tasks(meta: dict) -> list[dict]:
    """Derive the canonical set of extraction tasks from current meta state.

    Returns a list of task dicts with stable task_ids.
    Pure function — no side effects.
    """
    tasks = []
    for page_idx, page_info in enumerate(meta.get("pages", [])):
        classification = page_info.get("analysis_status")
        if classification != "done":
            continue  # not yet analyzed, no task

        class_value = page_info.get("classification")
        crops = page_info.get("crops", [])

        if class_value == "Simple":
            tasks.append({
                "task_id": f"page-{page_idx}",
                "page_idx": page_idx,
                "kind": "page",
                "image_source": "page",
            })
        elif class_value == "Complex":
            if len(crops) > 0:
                sorted_crops = sorted(crops, key=lambda c: c["bbox"][1])
                for crop_info in sorted_crops:
                    crop_filename = crop_info["filename"]
                    task_id = os.path.splitext(crop_filename)[0]
                    tasks.append({
                        "task_id": task_id,
                        "page_idx": page_idx,
                        "kind": "crop",
                        "crop_filename": crop_filename,
                        "image_source": "crop",
                    })
            else:
                tasks.append({
                    "task_id": f"page-{page_idx}",
                    "page_idx": page_idx,
                    "kind": "page",
                    "image_source": "page",
                })

    return tasks
```

### 2.2 Two Reconciliation Modes

The derivation function produces the desired task set, but two different modes of reconciling with the current state exist:

#### `reconcile_tasks(meta, desired_tasks)` — used at job start

Preserves statuses for surviving tasks. Does **not** delete fragments or `.complete`. This is called by the background job at start to sync the task list with current geometry, pick up any fragment files that exist on disk (from a prior partial run), and avoid duplicate work.

```python
def reconcile_tasks(meta: dict, desired_tasks: list) -> None:
    """Reconcile meta["extraction_tasks"] with desired set. In-place mutation.

    - Tasks whose task_id is in desired set: preserve status and fragment_path
    - Tasks whose task_id is NOT in desired set: dropped from the list
    - Desired tasks not yet in the list: added with "pending" status
    - If a surviving task has extraction_status=="extracted" but fragment file
      is missing from disk: reset to "pending"
    - If a surviving task has status!="extracted" but fragment file exists:
      upgrade to "extracted" and set fragment_path

    Does NOT delete .complete marker. Does NOT remove .complete.
    """
    existing_tasks = meta.get("extraction_tasks", [])
    desired_ids = {t["task_id"] for t in desired_tasks}
    existing_by_id = {t["task_id"]: t for t in existing_tasks}
    fragments_dir = _get_extraction_fragments_dir()

    final = []
    for desired in desired_tasks:
        tid = desired["task_id"]
        if tid in existing_by_id:
            existing = existing_by_id[tid]
            # Filesystem-level validation
            frag_path = existing.get("fragment_path")
            has_fragment = bool(frag_path and os.path.exists(os.path.join(fragments_dir, os.path.basename(frag_path))))
            if existing["extraction_status"] == "extracted" and not has_fragment:
                # Fragment lost — reset to pending
                existing["extraction_status"] = "pending"
                existing["fragment_path"] = None
                existing["extraction_error"] = None
                existing["extraction_error_type"] = None
            elif existing["extraction_status"] != "extracted" and has_fragment:
                # Fragment exists but status is not extracted — accept the fragment
                existing["extraction_status"] = "extracted"
                if not existing.get("fragment_path"):
                    existing["fragment_path"] = f"extraction_fragments/{tid}.html"
                existing["extraction_error"] = None
                existing["extraction_error_type"] = None
            final.append(existing)
        else:
            # Brand new task
            frag_path = f"extraction_fragments/{tid}.html"
            if os.path.exists(os.path.join(fragments_dir, tid + ".html")):
                final.append({
                    **desired,
                    "extraction_status": "extracted",
                    "fragment_path": frag_path,
                    "extraction_error": None,
                    "extraction_error_type": None,
                })
            else:
                final.append({
                    **desired,
                    "extraction_status": "pending",
                    "fragment_path": None,
                    "extraction_error": None,
                    "extraction_error_type": None,
                })

    meta["extraction_tasks"] = final
```

#### `on_crop_mutation(meta, sm, session_id, output_dir)` — used during crop mutations

Invalidates tasks whose underlying data changed, deletes their fragments, and removes the `.complete` marker. Called after every `/commit`, `/delete-crop`, `/trim` operation. Returns 409 if an extraction job is currently running for the session.

```python
def on_crop_mutation(meta: dict, sm, session_id: str, output_dir: str) -> None:
    """Called after a crop mutation to invalidate stale fragments and tasks.

    In-place mutation on meta. Deletes fragments and .complete marker.
    """
    if _is_extraction_in_progress(session_id):
        raise ExtractionInProgressError("Extraction job is running — cancel it or wait before mutating crops")

    desired_tasks = derive_required_tasks(meta)
    desired_ids = {t["task_id"] for t in desired_tasks}

    existing_tasks = meta.get("extraction_tasks", [])
    fragments_dir = sm.get_extraction_fragments_dir(session_id)

    # Delete fragments for tasks that no longer exist
    for task in existing_tasks:
        if task["task_id"] not in desired_ids:
            frag_path = task.get("fragment_path")
            if frag_path:
                full_path = os.path.join(fragments_dir, os.path.basename(frag_path))
                if os.path.exists(full_path):
                    os.unlink(full_path)

    # Rebuild task list: surviving tasks keep their status, new tasks are pending
    existing_by_id = {t["task_id"]: t for t in existing_tasks}
    final = []
    for desired in desired_tasks:
        tid = desired["task_id"]
        if tid in existing_by_id:
            final.append(existing_by_id[tid])
        else:
            final.append({
                **desired,
                "extraction_status": "pending",
                "extraction_error": None,
                "extraction_error_type": None,
                "fragment_path": None,
            })

    meta["extraction_tasks"] = final
    _remove_output_marker(session_id, output_dir)
```

### 2.3 Mutation Endpoints: Trim, Commit, Delete

Each mutation endpoint is a single locked transaction. The pattern is:

1. Acquire `metadata_lock`, re-read meta.json
2. Check `_is_extraction_in_progress()` → raise 409 if so
3. Apply the mutation (modify crops/image bytes)
4. Call `on_crop_mutation()` to invalidate stale state
5. Persist meta.json atomically
6. Release lock

```python
# /trim handler (complete transaction):
def handle_trim(session_id, crop_filename, new_bbox, sm, output_dir):
    with sm.metadata_lock(session_id):
        if _is_extraction_in_progress(session_id):
            return jsonify({"status": "error", "message": "Extraction in progress"}), 409

        meta = sm.load_meta(session_id)

        # 1. Find crop record and reset its task status BEFORE trimming the image
        fragments_dir = sm.get_extraction_fragments_dir(session_id)
        task_id = os.path.splitext(crop_filename)[0]
        for task in meta.get("extraction_tasks", []):
            if task["task_id"] == task_id:
                task["extraction_status"] = "pending"
                task["extraction_error"] = None
                task["extraction_error_type"] = None
                # Delete fragment file
                frag_path = os.path.join(fragments_dir, f"{task_id}.html")
                if os.path.exists(frag_path):
                    os.unlink(frag_path)
                task["fragment_path"] = None
                break

        # 2. Apply the actual trim to the image
        cm = CropManager(app.config["CROP_DIR"])
        crop_path = os.path.join(cm.crop_root, session_id, crop_filename)
        cm.trim_crop(crop_path, new_bbox)

        # 3. Reconcile tasks (handles any task-shape changes)
        on_crop_mutation(meta, sm, session_id, output_dir)

        # 4. Persist atomic write
        sm.save_meta_atomic(session_id, meta)

    return jsonify({"status": "ok"})
```

`/commit` and `/delete-crop` follow the same pattern: lock → check active job → apply mutation → `on_crop_mutation()` → persist → unlock.

**Why this handles all task-shape changes**:
- Adding first crop to Complex page: `derive_required_tasks` now produces crop tasks instead of a whole-page task → the `page-{idx}` task is in `removed_ids` → its fragment is deleted → crop tasks are added as `"pending"`
- Deleting last crop from Complex page: `derive_required_tasks` produces a whole-page task → old crop fragments deleted → new `page-{idx}` task added
- Trimming a crop: The crop filename stays the same → the specific handler resets the task's own status before calling `on_crop_mutation()`. If the trim changes crop geometry, `on_crop_mutation()` catches this through reconciliation.

### 2.4 Extraction-In-Progress Guard

To prevent unsafe concurrent mutation during extraction, a module-level set tracks active jobs:

```python
_extraction_in_progress = set()  # session_ids with active extraction jobs
_extraction_lock = threading.Lock()

def _is_extraction_in_progress(session_id: str) -> bool:
    with _extraction_lock:
        return session_id in _extraction_in_progress

def _set_extraction_in_progress(session_id: str):
    with _extraction_lock:
        _extraction_in_progress.add(session_id)

def _clear_extraction_in_progress(session_id: str):
    with _extraction_lock:
        _extraction_in_progress.discard(session_id)
```

The background `ExtractionJob` sets this flag when starting and clears it when finished (in `finally`).

### 2.5 Fragment Layout

```
uploads/<session_id>/
  extraction_fragments/
    page-0.html       # Simple page 0 (whole-page extraction)
    page-2.html       # Simple page 2
    crop_003.html     # Complex page, crop 3
    crop_007.html     # Complex page, crop 7
```

## 3. Background Job & SSE Observation

### 3.1 Roles: Start Endpoint vs SSE Subscriber

Two distinct endpoints govern extraction:

- **`POST /extract-html/<session_id>`** — the *only* way to start or retry an extraction job. Accepts a `retry_nonretryable` query parameter (see §3.4). Returns 409 if extraction is already in progress.
- **`GET /extract-progress/<session_id>`** — purely observes the job. **NEVER** starts or resumes a job. Returns the current disk state (including failed tasks). If no job is running, derives the terminal SSE event from `meta.json` task states and `.complete` existence.

This separation means: navigating to the extraction page shows failures but doesn't auto-retry them. The user must explicitly click "Retry" (which calls POST) to restart after non-retryable errors.

### 3.2 Extraction Job Model

**Contract: Background Job with Subscribers**

The extraction job runs in a background thread **independent of SSE requests**. SSE clients are **subscribers** that observe the job's progress by polling disk state.

```python
class ExtractionJob:
    """Background extraction job for a session."""

    def __init__(self, session_id, sm, crop_root, page_dir, output_dir, model,
                 max_workers=4, retry_nonretryable=False):
        self.session_id = session_id
        self.sm = sm
        self.crop_root = crop_root
        self.page_dir = page_dir
        self.output_dir = output_dir
        self.model = model
        self.max_workers = max_workers
        self.retry_nonretryable = retry_nonretryable
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        self.abort_flag = threading.Event()

        # Outcome: set ONLY by _execute_extraction(). Never overwritten by run().
        self.result = None      # "done" | "error" | "cancelled"
        self.error_message = None
        self.error_type = None  # "auth" | "credits" | "retryable" | "malformed_output"

    def run(self):
        """Execute the extraction job. Called in background thread.

        Note: _set_extraction_in_progress was called in _start_extraction_job()
        BEFORE this thread started, so the mutation guard is race-free.
        """
        # _set_extraction_in_progress is NOT called here (too late — see §3.6).
        try:
            self._execute_extraction()
            # _execute_extraction sets self.result directly. If it returned
            # without setting result (unexpected), mark as error.
            if self.result is None:
                self.result = "error"
                self.error_message = "Job returned without setting outcome"
                self.error_type = "retryable"
        except Exception as e:
            # Truly unexpected failure
            self.result = "error"
            self.error_message = str(e)
            self.error_type = "retryable"
        finally:
            _clear_extraction_in_progress(self.session_id)
            self.done_event.set()
            self.executor.shutdown(wait=False)
```

**Key design**: `_execute_extraction()` is the **sole writer** of `self.result`. `run()` only fills in a fallback for truly unexpected exceptions — it never overwrites a value that `_execute_extraction` set. Cancelled and retryable-failure jobs are therefore always reported correctly.

### 3.3 SSE Subscriber Pattern

```python
@app.route("/extract-progress/<session_id>", methods=["GET"])
def extraction_progress_sse(session_id):
    """Stream extraction progress as SSE events."""

    def generate():
        yield _sse_event({"status": "starting"})

        while True:
            meta = sm.load_meta(session_id)
            tasks = meta.get("extraction_tasks", [])
            completed = sum(1 for t in tasks if t["extraction_status"] == "extracted")
            total = len(tasks)

            job = _get_active_job(session_id)

            if job is None:
                # No job running. Derive terminal state from disk:
                # Case A: .complete exists → extraction finished successfully
                if _output_complete(session_id):
                    yield _sse_event({"status": "done", "progress": completed, "total": total})
                    return
                # Case B: failed tasks exist → last job failed
                failed = [t for t in tasks if t["extraction_status"] == "failed"]
                if failed:
                    # Report the first non-retryable failure preferentially
                    terminal_err = next(
                        (t for t in failed if t["extraction_error_type"] in ("auth", "credits")),
                        failed[0]
                    )
                    yield _sse_event({
                        "status": "error",
                        "error_type": terminal_err.get("extraction_error_type") or "retryable",
                        "message": terminal_err.get("extraction_error") or "Tasks failed",
                        "progress": completed,
                        "total": total
                    })
                    return
                # Case C: partially done but not complete — needs restart (user action)
                yield _sse_event({
                    "status": "paused",
                    "progress": completed,
                    "total": total,
                    "message": "Extraction interrupted. Click Retry to resume."
                })
                return

            # Job running — wait for done or timeout
            if job.done_event.is_set():
                yield _task_completion_event(job, completed, total)
                return

            yield _sse_event({
                "status": "progress",
                "progress": completed,
                "total": total,
                "log": f"Extracted {completed}/{total} regions..."
            })
            job.done_event.wait(timeout=0.5)

    return Response(generate(), mimetype="text/event-stream")
```

**Why this is correct**:
1. **Client disconnect ≠ cancellation**: `GeneratorExit` exits the generator but the job keeps running
2. **Reconnect only observes**: No job is created from SSE; POST is the only trigger
3. **Terminal state survives cleanup**: Even after `_cleanup_completed_jobs()` removes the job, SSE derives the final outcome from `meta.json` failed tasks + `.complete` existence
4. **Failed tasks drive error reporting**: When no job is running, we check `meta.json` for failed tasks. Auth/credit failures are reported prominently

### 3.4 Job Execution: Bounded Submission

```python
def _execute_extraction(self):
    """Core extraction logic with bounded submission."""

    # 1. Reconcile tasks against current disk state (see §2.2)
    #    Uses reconcile_tasks, NOT on_crop_mutation — we don't want to
    #    delete .complete here, we want to *create* it at the end.
    with self.sm.metadata_lock(self.session_id):
        meta = self.sm.load_meta(self.session_id)
        desired = derive_required_tasks(meta)
        reconcile_tasks(meta, desired)
        self.sm.save_meta_atomic(self.session_id, meta)

    # 2. Determine what needs to run
    meta = self.sm.load_meta(self.session_id)
    tasks = meta.get("extraction_tasks", [])

    # Filter: unless retry_nonretryable flag, skip tasks that failed with auth/credits
    def _eligible(task):
        if task["extraction_status"] == "extracted":
            return False
        if not self.retry_nonretryable and task["extraction_status"] == "failed" \
           and task.get("extraction_error_type") in ("auth", "credits"):
            return False
        return True

    tasks_to_run = [t for t in tasks if _eligible(t)]

    if not tasks_to_run:
        # All tasks extracted (or only skipped auth/credit failures remain)
        failed = [t for t in tasks if t["extraction_status"] == "failed"]
        if failed:
            terminal = next((t for t in failed if t["extraction_error_type"] in ("auth", "credits")), failed[0])
            self.result = "error"
            self.error_message = terminal.get("extraction_error") or "Tasks failed"
            self.error_type = terminal.get("extraction_error_type") or "retryable"
            return
        # Nothing failed — run assembly
        self._run_assembly(meta)
        self.result = "done"
        return

    # 3. Zero-task short-circuit (safety; also guards semaphore deadlock)
    max_workers = min(self.max_workers, len(tasks_to_run))
    if max_workers <= 0:
        self.result = "error"
        self.error_message = "No eligible tasks"
        self.error_type = "retryable"
        return

    semaphore = threading.Semaphore(max_workers)
    futures = []

    # 4. Submit tasks with bounded concurrency
    for task in tasks_to_run:
        if self.cancel_event.is_set() or self.abort_flag.is_set():
            break
        semaphore.acquire()
        # Post-acquire check: abort/cancel may have been set while waiting
        if self.cancel_event.is_set() or self.abort_flag.is_set():
            semaphore.release()
            break
        future = self.executor.submit(self._extract_task, task, semaphore)
        futures.append(future)

    # 5. Drain futures — collect any that raised PipelineCallError
    for future in futures:
        try:
            future.result()
        except PipelineCallError:
            pass  # already persisted to meta.json by worker
        except Exception as e:
            # Unexpected: log but don't crash the job
            pass

    # 6. Final decision — NEVER overwrite self.result here; derive from disk state
    if self.cancel_event.is_set() and not self.abort_flag.is_set():
        self.result = "cancelled"
        return

    meta = self.sm.load_meta(self.session_id)
    failed = [t for t in meta["extraction_tasks"] if t["extraction_status"] == "failed"]

    if failed:
        # Pick terminal error type
        auth_fail = [t for t in failed if t["extraction_error_type"] in ("auth", "credits")]
        if auth_fail:
            self.result = "error"
            self.error_message = auth_fail[0]["extraction_error"]
            self.error_type = auth_fail[0]["extraction_error_type"]
        else:
            self.result = "error"
            self.error_message = f"{len(failed)} task(s) failed"
            self.error_type = failed[0]["extraction_error_type"] or "retryable"
        return

    # Assemble and publish
    self._run_assembly(meta)
    self.result = "done"
```

**Why this is correct**:
1. **Pre+post abort checks**: Prevent submission after abort_flag is set while blocked on semaphore
2. **Zero-task guard**: No semaphore acquire, no deadlock, clear error message
3. **Explicit `retry_nonretryable` flag**: Normal start does NOT retry auth/credit failures. Only the explicit Retry action sets the flag.
4. **Result only set by `_execute_extraction()`**: `run()` never overwrites (see §3.2)
5. **`reconcile_tasks` not `on_crop_mutation`**: Doesn't delete `.complete`; only syncs task list and validates filesystem state

### 3.5 Worker Implementation

```python
def _extract_task(self, task, semaphore):
    """Extract a single task. Called in thread pool worker."""
    try:
        # Determine image source
        meta = self.sm.load_meta(self.session_id)
        page_info = meta["pages"][task["page_idx"]]

        if task["kind"] == "page":
            # Whole-page task → load the full page image
            page_path = os.path.join(self.page_dir, page_info["path"])
            with Image.open(page_path) as img:
                crop_img = img.copy()
        else:
            # Crop task → load the crop image
            crop_path = os.path.join(self.crop_root, self.session_id, task["crop_filename"])
            with Image.open(crop_path) as img:
                crop_img = img.copy()

        # extract_crop_as_html accepts a PIL.Image.Image (not a path)
        html_fragment = extract_crop_as_html(crop_img, self.model)

        # Write fragment atomically
        fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)
        fragment_path = os.path.join(fragments_dir, f"{task['task_id']}.html")
        _write_file_atomic(fragment_path, html_fragment)

        # Update meta.json
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            for t in meta.get("extraction_tasks", []):
                if t["task_id"] == task["task_id"]:
                    t["extraction_status"] = "extracted"
                    t["fragment_path"] = f"extraction_fragments/{task['task_id']}.html"
                    t["extraction_error"] = None
                    t["extraction_error_type"] = None
            self.sm.save_meta_atomic(self.session_id, meta)

    except PipelineCallError as e:
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            for t in meta.get("extraction_tasks", []):
                if t["task_id"] == task["task_id"]:
                    t["extraction_status"] = "failed"
                    t["extraction_error"] = e.message
                    t["extraction_error_type"] = e.error_type
            self.sm.save_meta_atomic(self.session_id, meta)

        if e.error_type in ("auth", "credits"):
            self.abort_flag.set()

    except Exception as e:
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            for t in meta.get("extraction_tasks", []):
                if t["task_id"] == task["task_id"]:
                    t["extraction_status"] = "failed"
                    t["extraction_error"] = str(e)
                    t["extraction_error_type"] = "retryable"
            self.sm.save_meta_atomic(self.session_id, meta)

    finally:
        semaphore.release()
```

**Key fixes**:
1. Uses `Image.open()` and passes a `PIL.Image.Image` to `extract_crop_as_html` (which already expects an Image, not a path)
2. `ThreadPoolExecutor` is constructed in `ExtractionJob.__init__` (see §3.2) and shut down in `run()`'s `finally`
3. Uses top-level `meta["extraction_tasks"]` (consistent with §1.2)

### 3.6 Job Lifecycle Management

```python
import weakref

# Module-level registry with weak references for cleanup
_active_jobs = {}  # session_id → ExtractionJob
_jobs_lock = threading.Lock()

def _get_active_job(session_id):
    with _jobs_lock:
        return _active_jobs.get(session_id)

def _start_extraction_job(session_id, sm, crop_root, page_dir, output_dir,
                           model, max_workers=4, retry_nonretryable=False):
    """Create and start a new extraction job.

    Atomically registers the in-progress state BEFORE starting the thread,
    so crop mutation guards in §2.4 observe an active extraction immediately
    after this function returns — no race window exists.
    """
    with _jobs_lock:
        existing = _active_jobs.get(session_id)
        if existing and not existing.done_event.is_set():
            raise ExtractionInProgressError("Job already running for session")

        job = ExtractionJob(
            session_id=session_id,
            sm=sm,
            crop_root=crop_root,
            page_dir=page_dir,
            output_dir=output_dir,
            model=model,
            max_workers=max_workers,
            retry_nonretryable=retry_nonretryable,
        )
        _active_jobs[session_id] = job
        # Set in-progress flag ATOMICALLY with job registration, under the
        # same lock. This is the moment mutation guards begin to reject crops.
        _set_extraction_in_progress(session_id)

        thread = threading.Thread(target=job.run, name=f"extract-{session_id[:8]}", daemon=True)
        thread.start()
        return job
```

**SSE route integration**:

```python
@app.route("/extract-html/<session_id>", methods=["POST"])
def start_extraction(session_id):
    """Start extraction job. The ONLY way to start/retry extraction."""
    retry_nonretryable = request.args.get("retry_nonretryable", "false") == "true"

    meta = sm.load_meta(session_id)
    if not _all_pages_analyzed(meta):
        return jsonify({"status": "error", "message": "Not all pages analyzed"}), 400

    # If any tasks failed with auth/credits and retry flag not set, return error
    tasks = meta.get("extraction_tasks", [])
    if not retry_nonretryable:
        auth_failed = [t for t in tasks
                       if t["extraction_status"] == "failed"
                       and t.get("extraction_error_type") in ("auth", "credits")]
        if auth_failed:
            return jsonify({
                "status": "error",
                "message": "Auth/credit failure. Call with ?retry_nonretryable=true after fixing.",
                "error_type": auth_failed[0]["extraction_error_type"]
            }), 400

    try:
        job = _start_extraction_job(session_id, sm, crop_root, page_dir,
                                     output_dir, model,
                                     retry_nonretryable=retry_nonretryable)
    except ExtractionInProgressError:
        return jsonify({"status": "error", "message": "Extraction already running"}), 409

    return jsonify({"status": "started"})

@app.route("/extract-progress/<session_id>", methods=["GET"])
def extraction_progress_sse(session_id):
    """Observe extraction progress. NEVER starts or retries a job."""
    # (see §3.3)
```

**Why this is correct**:
1. **POST is the only start mechanism**: SSE observes; POST acts
2. **Explicit retry flag**: Auth/credit retries require query parameter; normal start fails
3. **409 when job running**: Prevents duplicate jobs
4. **Auto-cleanup via `done_event`**: Job remains in registry for SSE observers to read terminal state; cleanup happens periodically via `_cleanup_completed_jobs()`

---

## 4. Session Lock Model (Thread-Local Only)

### 4.1 Lock Scope and Limitations

The session lock is a **threading.Lock per session**, protecting read-modify-write operations on `meta.json` **within a single process**.

**Limitations**:
- **NOT process-safe**: Multiple Flask worker processes can still race. This design assumes single-process Flask (or that concurrent mutations from different processes are acceptable to lose).
- **No nesting**: Never acquire `metadata_lock` while already holding it (deadlock risk)
- **No cross-session dependencies**: Each session's lock is independent

### 4.2 Lock Implementation

```python
class SessionManager:
    def __init__(self, upload_dir, crop_dir):
        self.upload_dir = upload_dir
        self.crop_dir = crop_dir
        self._session_locks = {}  # session_id → threading.Lock
        self._locks_lock = threading.Lock()  # Protects _session_locks dict

    def metadata_lock(self, session_id):
        """Get the lock for a session. Creates if needed."""
        with self._locks_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def save_meta_atomic(self, session_id, meta):
        """Write meta.json atomically using temp file + os.replace.

        MUST be called while holding metadata_lock(session_id).
        """
        session_dir = self.get_session_dir(session_id)
        meta_path = os.path.join(session_dir, "meta.json")

        # Write to temp file
        fd, tmp_path = tempfile.mkstemp(dir=session_dir, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            # Atomic replace
            os.replace(tmp_path, meta_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
```

### 4.3 Correct Usage Patterns

**Pattern 1: Read-modify-write with lock held:**

```python
with sm.metadata_lock(session_id):
    meta = sm.load_meta(session_id)
    # Modify meta
    meta["some_field"] = "value"
    sm.save_meta_atomic(session_id, meta)
```

**Pattern 2: Update meta for one task (worker):**

```python
with sm.metadata_lock(session_id):
    meta = sm.load_meta(session_id)
    for task in meta["extraction_tasks"]:
        if task["task_id"] == target_task_id:
            task["extraction_status"] = "extracted"
    sm.save_meta_atomic(session_id, meta)
```

**Pattern 3: NEVER do this (deadlock risk):**

```python
# WRONG: Nested locks
with sm.metadata_lock(session_id):
    meta = sm.load_meta(session_id)
    # ... some code that calls another function ...
    # That function tries to acquire metadata_lock again → DEADLOCK!
```

### 4.4 Future Enhancement: Process-Level Locking

If multi-process deployment is needed, replace `threading.Lock` with file-based locking:

```python
import fcntl

class SessionManager:
    def metadata_lock(self, session_id):
        lock_path = os.path.join(self.get_session_dir(session_id), ".lock")
        return FileLock(lock_path)

class FileLock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()
```

This is not implemented now (single-process assumption), but the architecture supports it.

---

## 5. Output Publication: Complete .complete Lifecycle

### 5.1 The .complete Marker Contract

The `.complete` marker file is the **sole signal** that output is ready. It is written **atomically** after all page files and `index.html` are written.

**Rules**:
1. **Write**: Only after ALL output files are written successfully
2. **Read**: All output-serving routes MUST check for `.complete` before serving
3. **Delete**: On any crop mutation or failed extraction

### 5.2 Writing .complete Atomically

```python
def _run_assembly(self, meta):
    """Assemble fragments into page files and write .complete marker.

    Sequence:
    1. Delete any stale .complete marker (prevents serving partial output)
    2. Read fragment files from disk
    3. Assemble per-page HTML using write_page_files()
    4. Write fresh .complete marker atomically
    """
    output_dir = self.output_dir
    fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)

    # STEP 1: Remove stale .complete BEFORE any reassembly work
    session_output_dir = os.path.join(output_dir, self.session_id)
    _remove_output_marker(self.session_id, output_dir)

    # STEP 2: Build per-page HTML from fragments on disk
    pages_data = []
    for page_idx, page_info in enumerate(meta.get("pages", [])):
        # Collect fragment texts for this page, sorted by crop bbox y0
        page_tasks = [
            t for t in meta.get("extraction_tasks", [])
            if t["page_idx"] == page_idx and t["extraction_status"] == "extracted"
        ]
        # Preserve fragment sort order (bbox y0 sort already applied via derive_required_tasks)
        fragments = []
        for task in page_tasks:
            frag_path = os.path.join(fragments_dir, os.path.basename(task["fragment_path"]))
            with open(frag_path, "r", encoding="utf-8") as f:
                fragments.append(f.read())
        pages_data.append({"html": "\n".join(fragments)})

    # STEP 3: Write page files via existing assembler
    session_files = meta.get("files", [])
    title = session_files[0] if session_files else f"Session {self.session_id[:8]}"
    write_page_files(self.session_id, pages_data, title, output_root=output_dir)

    # STEP 4: Write .complete marker atomically
    _write_complete_marker(session_output_dir)
```

### 5.3 .complete Marker Helpers

```python
def _remove_output_marker(session_id, output_dir):
    """Delete .complete marker if it exists. Safe to call if marker absent."""
    session_dir = os.path.join(output_dir, session_id)
    marker_path = os.path.join(session_dir, ".complete")
    if os.path.exists(marker_path):
        os.unlink(marker_path)

def _write_complete_marker(session_output_dir):
    """Write .complete marker atomically via temp file + os.replace."""
    fd, tmp_path = tempfile.mkstemp(dir=session_output_dir, suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time()}, f)
        os.replace(tmp_path, os.path.join(session_output_dir, ".complete"))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

### 5.4 All Output-Serving Routes Must Check .complete

```python
@app.route("/extracted/<session_id>/extraction.html", methods=["GET"])
def serve_extracted_html(session_id):
    """Serve the index page for an extracted session."""
    _sm = app.session_manager
    if not _sm.session_exists(session_id):
        return "Session not found", 404

    base_dir = app.config["EXTRACTED_DIR"]
    session_dir = os.path.realpath(os.path.join(base_dir, session_id))
    if not session_dir.startswith(base_dir + os.sep):
        return "Session not found", 404
    if not os.path.isdir(session_dir):
        return "Extraction not found. Please run extraction first.", 404

    # MUST check .complete marker
    complete_marker = os.path.join(session_dir, ".complete")
    if not os.path.exists(complete_marker):
        return "Extraction not complete. Please retry.", 404

    index_path = os.path.join(session_dir, "index.html")
    if not os.path.exists(index_path):
        return "Extraction not found. Please run extraction first.", 404

    return send_file(index_path, mimetype="text/html")

@app.route("/extracted/<session_id>/page-<int:page_idx>.html", methods=["GET"])
def serve_extracted_page(session_id, page_idx):
    """Serve a single extracted page HTML file."""
    _sm = app.session_manager
    if not _sm.session_exists(session_id):
        return "Session not found", 404

    base_dir = app.config["EXTRACTED_DIR"]
    session_dir = os.path.realpath(os.path.join(base_dir, session_id))
    if not session_dir.startswith(base_dir + os.sep):
        return "Session not found", 404

    # MUST check .complete marker
    complete_marker = os.path.join(session_dir, ".complete")
    if not os.path.exists(complete_marker):
        return "Extraction not complete. Please retry.", 404

    page_path = os.path.join(session_dir, f"page-{page_idx}.html")
    if not os.path.exists(page_path):
        return "Page not found", 404

    return send_file(page_path, mimetype="text/html")
```

**Why this is correct**:
1. **No partial output**: If `.complete` doesn't exist, output is incomplete → return 404
2. **Consistent state**: `.complete` only exists if all files were written successfully
3. **Clean failure**: If crash during write, `.complete` doesn't exist → old files (if any) are not served

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
    def __init__(self, message: str, cause: Exception = None):
        super().__init__(message, cause)
        self.error_type = "malformed_output"
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

The implementation details are in §3.4 (Job Execution) and §3.5 (Worker). This section specifies the behavioral contracts.

### 7.1 Submission Guarantees

1. **No work submitted after abort**: Before each `executor.submit()`, both `cancel_event` and `abort_flag` are checked. If either is set, submission stops immediately.
2. **Check after semaphore acquire**: The abort check also runs AFTER `semaphore.acquire()` returns (in case abort was set while blocking on the semaphore). This prevents submitting work that shouldn't run:
   ```python
   for task in tasks_to_run:
       # Pre-check (fast path)
       if self.cancel_event.is_set() or self.abort_flag.is_set():
           break
       semaphore.acquire()
       # Post-check (in case abort was set while waiting for semaphore)
       if self.cancel_event.is_set() or self.abort_flag.is_set():
           semaphore.release()
           break
       future = self.executor.submit(self._extract_task, task, semaphore)
       futures.append(future)
   ```
3. **Zero-task safety**: When `tasks_to_run` is empty, the loop body never executes. No semaphore is acquired, no threads are blocked.
4. **Semaphore always released**: Workers call `semaphore.release()` in their `finally` block, guaranteeing the main submission loop can always make progress.

### 7.2 Non-Retryable Error Contract

When a worker detects a non-retryable error (auth/credits):
1. **Worker sets `abort_flag`** — no further tasks are submitted
2. **Worker persists failure** to `meta.json` under `metadata_lock`
3. **Worker releases semaphore** in `finally` — main thread unblocks
4. **Main thread drains** — waits for all already-submitted futures via `future.result()`
5. **Main thread returns `result = "error"`** without running assembly
6. **Assembly is never started** while any task has `extraction_status == "failed"`

### 7.3 Cancellation Contract

When `cancel_event` is set (explicit user cancellation, not client disconnect):
1. **Submission stops** — no further tasks submitted
2. **Unsubmitted tasks remain `pending`** in `meta.json`
3. **In-flight workers complete normally** — their results are persisted
4. **Job `.result = "cancelled"`** — assembly is NOT triggered
5. **`done_event` is set** — subscribers are notified
6. **On next SSE connection or extraction start**, `on_crop_mutation()` re-derives tasks and finds pending ones → they are re-submitted

### 7.4 SSE Disconnect vs Explicit Cancellation

- **SSE client disconnect**: The `generate()` generator exits via `GeneratorExit`. This does NOT set `cancel_event`. The job continues in the background. The subscriber just stops receiving events.
- **Explicit cancellation** (future UI): A separate endpoint would set `job.cancel_event`. This stops the job.

The current UI has no explicit cancel button, so SSE disconnect = job continues.

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

Existing blank entries may be in `.stage_cache/` from before detection was implemented.

**Problem**: `cached_call` only writes to cache when `fn()` returns successfully. If we use `force=True` to bypass the cache and re-execute `_call()`, but `_call()` raises `BlankResponseError`, the old blank cache entry is never overwritten. Using `force=True` alone is insufficient.

**Solution**: Detect stale blank after `cached_call` returns, delete the stale cache file manually, then re-call normally so that successful results are cached:

```python
from table_extractor.cache import _cache_key, CACHE_DIR

def extract_crop_as_html(crop_image, model):
    # ... setup ...

    def _cached_extract():
        return cached_call(
            image_bytes=img_bytes,
            stage="html_extract",
            model=model,
            fn=_call,
            force=False,
            extra_key=system_prompt,
        )

    result = _cached_extract()

    # Check for stale blank cache entry
    if is_blank_fragment(result[0]):
        # Delete stale cache file so next call gets a fresh result
        cache_key = _cache_key(img_bytes, "html_extract", model, system_prompt)
        cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
        if os.path.exists(cache_file):
            os.unlink(cache_file)
        # Re-call: this time cached_call won't find the stale entry,
        # so _call (with retry + BlankResponseError) runs fresh
        result = _cached_extract()

    return result[0]
```

**Why this works**:
1. `cached_call` returns the stale blank entry from cache → we detect it
2. We delete the cache file → next call has no cached result to hit
3. `_cached_extract()` runs again → `_call()` executes → raises `BlankResponseError` → `retry_with_backoff` retries → if successful, `cached_call` writes new non-blank result to cache
4. If all retries still blank → `BlankResponseError` propagates → no cache entry written

**No infinite loop**: The post-check only triggers once. If the second call also returns blank (shouldn't happen since we deleted the cache), it returns whatever result we got — the task will be marked as failed by the worker.

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
3. Per-page results are persisted to `meta.json` immediately via `sm.metadata_lock()` + `sm.save_meta_atomic()` (not batched)

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
- **Partial tasks with retryable errors**: Show progress page, auto-retry failed tasks (network timeouts, blank responses are transient)
- **No extraction started** (or all tasks `pending`): Normal fresh extraction
- **All failed with non-retryable error** (auth/credits): Show error banner with actionable message and "Retry" button. **Retry requires explicit user action** — navigating to the page does not auto-retry. This prevents repeated failed API calls after the underlying issue (expired key, exhausted credits) is resolved.

**Why the distinction**:
- **Retryable errors** (timeouts, 5xx, 429 rate limits, blank responses): These are transient issues that may resolve on their own. Auto-retrying on page navigation is safe because the underlying cause is likely fixed.
- **Non-retryable errors** (401 auth, 402 credits): These require user intervention (update API key, purchase credits). Auto-retrying would waste time and potentially incur costs. The user must explicitly click "Retry" after fixing the issue.

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

`/extract-progress/<session_id>` is purely observational — it never starts, resumes, or triggers any background work. Reconnecting clients derive their view from the current job state and disk state:

- **Job already running** (`_get_active_job(session_id)` returns a live job): Subscribe to the active job's `done_event`. Emit per-tick `{"status": "progress"}` events until `done_event.is_set()`, then emit the terminal event derived from the job's `result` (see §3.3).
- **No active job, `.complete` marker exists**: Yield `{"status": "done"}` with the current completion counter and return.
- **No active job, no `.complete`, failed tasks remain in `meta.json`**: Yield `{"status": "error"}` with the terminal task's `extraction_error_type` and `extraction_error` (see §3.3 derivation rules), and return. The user must click a Retry button (which calls `POST /extract-html/<session_id>`) to restart; reconnecting alone does not retry.
- **No active job, no `.complete`, no failed tasks, zero `extraction_tasks`**: No prior extraction has been started for this session. Yield `{"status": "idle", "message": "Extraction not started"}` and return.
- **No active job, no `.complete`, no failed tasks, but `extraction_tasks` exist with `extracted`/`pending`**: Assembly has not yet published output, but a prior run was interrupted or the user never called POST again. Yield `{"status": "paused", "message": "Click Retry to resume"}` and return.

Under no circumstances does GET `extract-progress` mutate `meta.json`, delete fragments, delete `.complete`, or call `_start_extraction_job()`.

### 13.2 Progress Counter

The progress counter reflects **total** completed work (pre-existing + just-completed in this run). On resume:
- Count tasks with `extraction_status == "extracted"` as already done
- Progress starts at that count, increments as new tasks complete

---

## 14. Backward Compatibility

### 14.1 Missing Fields

| Missing Field | Default Behavior |
|---|---|
| `analysis_status` on a page | `"done"` if `classification` set or `complex: true`; else `"pending"` |
| `extraction_tasks` (top-level) | Construct by calling `derive_required_tasks(meta)` against the current `pages[]` + crops; tasks without an on-disk fragment start as `"pending"`. (See §2.1.) |
| `next_crop_id` (top-level) | Derived from the existing crops directory: compute `max(existing numeric IDs)` + 1. (See §1.1.) |
| `.complete` marker | Absent — assembly is needed |

Note: there is no per-page `extraction_status`. Page-level completion is derived from the top-level tasks whose `page_idx` matches.

No migration script is needed. The normalization logic in §14 runs on first access by any of:
- `POST /extract-html` (start/retry extraction)
- Crop mutation routes (`POST /commit`, `POST /trim`, `POST /delete-crop`)

SSE (`GET /extract-progress`) observes current state but does not normalize. If the session has missing fields, SSE derives a degraded view (e.g., "idle" or "paused") and prompts the user to trigger normalization via one of the above routes.

### 14.2 No Schema Migration

New fields are additive. Old sessions continue to work. The reconciliation rules (§12) handle edge cases where status and filesystem disagree.

---

## 15. Non-Retryable Errors & Resume-After-Fix

### 15.1 Resume After Key Rotation / Credit Top-Up

The pipeline fully resumes from disk state. **Both scenarios require explicit "Retry" action** — navigating to the page does NOT auto-retry for non-retryable errors.

**Key rotation**:
1. User edits `.env` with new API key
2. User restarts Flask app
3. User navigates to the session's extraction page — sees "Authentication failed" banner with "Retry" button
4. User clicks "Retry" (explicit action)
5. Backend finds tasks with `extraction_status == "failed"` and `extraction_error_type == "auth"`
6. Failed tasks are re-submitted — new API key is used
7. Already-extracted tasks: fragments on disk, skipped
8. Assembly reads all fragments → `.complete` marker → done

**Credit top-up** (key unchanged):
1. User tops up OpenRouter account
2. User navigates to the session's extraction page — sees "Insufficient credits" banner with "Retry" button
3. User clicks "Retry" (explicit action)
4. Same flow as above

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
| `crop_app/app.py` | Use `sm.metadata_lock()` + `sm.save_meta_atomic()` for all meta writes. Add background job start to extraction route. Update `/extract-html/` prerequisite (remove crop check, require analysis done). Add `/analyze/` immediate persistence. Update `/trim/`, `/delete-crop/`, `/commit/` with `on_crop_mutation()` invalidation. Update all output-serving routes to check `.complete`. |
| `crop_app/session_manager.py` | Add `metadata_lock()`, `save_meta_atomic()`, `get_extraction_fragments_dir()`. Keep existing methods. |
| `table_extractor/retry.py` | **NEW** — Full exception hierarchy (`PipelineCallError`, `RetryableError`, `NonRetryableError`, `AuthError`, `CreditsExhaustedError`, `BlankResponseError`, `MalformedOutputError`). `retry_with_backoff()` with `max_attempts` semantics, jitter, Retry-After parsing. `classify_api_error()`. `is_blank_fragment()`. |
| `table_extractor/html_extractor.py` | Add `ExtractionJob` class with background thread (`done_event`, `abort_flag`, `retry_nonretryable` flag). Add `derive_required_tasks()`, `reconcile_tasks()`, `on_crop_mutation()`. Add `_extraction_in_progress` set + guard. Rewrite `_execute_extraction()` with bounded post-acquire abort checks and `retry_nonretryable` filter. Worker opens image via `Image.open()` and passes `PIL.Image` to `extract_crop_as_html()`. `_run_assembly()` deletes `.complete` first, reads fragments from disk, calls `write_page_files()`, then writes `.complete` via `_write_complete_marker()`. Blank response detection + stale cache delete-and-retry. |
| `table_extractor/cache.py` | Export `_cache_key()` and `CACHE_DIR` for stale blank cache deletion from `html_extractor.py`. |
| `crop_app/crop_manager.py` | Rewrite `_next_crop_index()` to use persistent `next_crop_id` from `meta.json` instead of file-count-based counter. |
| `table_extractor/html_assembler.py` | No change — `write_page_files()` continues to write page HTML files and `index.html`. The `.complete` marker is written by `_run_assembly()` after `write_page_files()` returns (see §5.2). |
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
  - Stale cache bypass: pre-populate cache with blank entry → verify cache entry deleted → verify re-call returns non-blank → verify cache overwritten with valid content

- `test_session_manager.py`:
  - `save_meta_atomic` atomic write: verify `os.replace` path
  - Concurrent writes via `metadata_lock`: two threads racing → final state consistent
  - `metadata_lock` isolation between different sessions

- `test_crop_invalidations.py`:
  - `/trim` deletes fragment + resets task + deletes `.complete`
  - `/delete-crop` removes task + deletes fragment + deletes `.complete`
  - `/commit` adds tasks + deletes `.complete`

### 17.2 Integration Tests

- Mock LLM failure mid-extraction → verify partial task state → resume → verify complete output with `.complete` marker
- Mock 401 → verify bounded submission stops → abort → persist results → terminal error → fix key → resume with `retry_nonretryable=true` → verify success
- Mock 429 → verify Retry-After delay → verify eventual success
- Mock blank response → verify retry → verify success → verify fragment written
- Pre-populate cache with blank entry → verify cache entry deleted → verify re-call succeeds → verify cache overwritten
- Crop trim → extract → verify new fragment differs from pre-trim fragment
- Concurrent SSE requests → verify only one active job → second request observes
- Cancel during extraction → verify in-flight workers complete → verify meta.json accurate → resume → complete
- Crash during `meta.json` write → verify `.tmp` abandoned → verify old `meta.json` intact on next load
- Crash during `write_page_files` → verify `.complete` not written → verify resume re-runs assembly (existing `.complete` deleted first)
- Load legacy `meta.json` → verify normalization produces correct default statuses
- All-Simple session → verify extraction runs without committed crops
- `extracted` + missing fragment → verify task reset to `pending` on resume
- Fragment exists + `pending` status → verify task used as-is and marked `extracted`
- `.complete` exists but tasks incomplete → verify output treated as stale, `.complete` deleted before reassembly
- **First-crop task-shape transition**: Complex page with no crops → extract (produces `page-0` task) → `/commit` adds first crop → verify `page-0` task removed, crop tasks created with `pending` status
- **Last-crop task-shape transition**: Complex page with crops → extract (per-crop tasks) → `/delete-crop` removes last crop → verify crop tasks removed, `page-0` task re-created with `pending` status
- **Non-reused crop IDs**: Commit → delete crop_001 → commit again → verify new crop gets `crop_002` (not `crop_001`). `next_crop_id` increases monotonically.
- **No automatic auth retry**: Session with auth failures → `POST /extract-html` (no `retry_nonretryable`) → verify 400 response, no job started. Then `POST /extract-html?retry_nonretryable=true` → verify job starts and retries auth failures.
- **Terminal SSE state after cleanup**: Complete extraction → auth failure tasks exist → call `_cleanup_completed_jobs()` → GET `/extract-progress` → verify "error" status derived from failed tasks even though no job object exists.
- **Mutation rejection during active job**: Start extraction → during extraction, `POST /trim` → verify 409 "Extraction in progress"

### 17.3 Edge Cases

- Process death and restart: verify disk-state-only recovery, no stale job in registry
- Network disconnect and reconnect: verify resume from disk state (SSE only observes, POST starts)
- Same image, different prompt → verify cache miss (prompt in extra_key)
- `max_attempts=3` semantics → verify exactly 3 total calls, not 3 retries
- SSE reconnection: verify terminal state derived from disk when no job exists in registry
- Crop mutation during extraction: verify 409 response from mutation endpoint
- `next_crop_id` persistence: verify it survives app restart and prevents ID reuse
