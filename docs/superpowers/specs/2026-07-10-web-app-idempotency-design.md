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

## 2. Canonical Reconciliation

### 2.1 The Reconciliation Function

A single function derives the **exact** required task set from current state. It is the sole authority on what tasks should exist and their identity.

```python
def derive_required_tasks(meta: dict) -> list[dict]:
    """Derive the canonical set of extraction tasks from current meta state.

    Called after every crop mutation and before extraction starts.
    Returns a list of task dicts with stable task_ids.
    """
    tasks = []
    for page_idx, page_info in enumerate(meta.get("pages", [])):
        classification = page_info.get("classification")
        if classification is None:
            continue  # not yet analyzed, no tasks

        crops = page_info.get("crops", [])

        if classification == "Simple":
            # Simple page → one whole-page task
            tasks.append({
                "task_id": f"page-{page_idx}",
                "page_idx": page_idx,
                "kind": "page",
                "image_source": "page",  # use page image
            })
        elif classification == "Complex":
            if len(crops) > 0:
                # Complex page with crops → one task per crop, sorted by bbox y0
                sorted_crops = sorted(crops, key=lambda c: c["bbox"][1])
                for crop_info in sorted_crops:
                    crop_filename = crop_info["filename"]
                    task_id = os.path.splitext(crop_filename)[0]  # strip .png
                    tasks.append({
                        "task_id": task_id,
                        "page_idx": page_idx,
                        "kind": "crop",
                        "crop_filename": crop_filename,
                        "image_source": "crop",  # use crop image
                    })
            else:
                # Complex page with no crops → whole-page task (fallback)
                tasks.append({
                    "task_id": f"page-{page_idx}",
                    "page_idx": page_idx,
                    "kind": "page",
                    "image_source": "page",
                })

    return tasks
```

### 2.2 Reconciliation on Crop Mutation

Every crop mutation endpoint (`/commit`, `/trim`, `/delete-crop`) must:

```python
def on_crop_mutation(session_id, sm):
    """Called after applying any crop mutation to meta.json.

    1. Derive the new required task set.
    2. Compare with existing tasks.
    3. Delete fragments for removed/orphaned tasks.
    4. Add new tasks with pending status.
    5. Remove .complete marker.
    """
    with sm.metadata_lock(session_id):
        meta = sm.load_meta(session_id)

        # 1. Derive new tasks
        new_tasks = derive_required_tasks(meta)
        new_task_ids = {t["task_id"] for t in new_tasks}

        # 2. Get existing tasks
        existing_tasks = meta.get("extraction_tasks", [])
        existing_task_ids = {t["task_id"] for t in existing_tasks}

        # 3. Fragment dir
        fragments_dir = sm.get_extraction_fragments_dir(session_id)

        # 4. Delete fragments for removed tasks
        removed_ids = existing_task_ids - new_task_ids
        for task in existing_tasks:
            if task["task_id"] in removed_ids:
                fragment_path = task.get("fragment_path")
                if fragment_path:
                    full_path = os.path.join(fragments_dir, os.path.basename(fragment_path))
                    if os.path.exists(full_path):
                        os.unlink(full_path)

        # 5. Build new task list preserving status for surviving tasks
        surviving = {t["task_id"]: t for t in existing_tasks if t["task_id"] in new_task_ids}
        final_tasks = []
        for req in new_tasks:
            if req["task_id"] in surviving:
                # Task still exists → preserve its status
                final_tasks.append(surviving[req["task_id"]])
            else:
                # New task → pending
                final_tasks.append({
                    **req,
                    "extraction_status": "pending",
                    "extraction_error": None,
                    "extraction_error_type": None,
                    "fragment_path": None,
                })

        # 6. Persist
        meta["extraction_tasks"] = final_tasks

        # 7. Remove .complete marker
        _remove_output_marker(session_id, sm)

        sm.save_meta_atomic(session_id, meta)
```

**Why this handles all task-shape changes**:
- Adding first crop to Complex page: `derive_required_tasks` now produces crop tasks instead of a whole-page task → the `page-{idx}` task is in `removed_ids` → its fragment is deleted → crop tasks are added as `pending`
- Deleting last crop from Complex page: `derive_required_tasks` produces a whole-page task → old crop task fragments are deleted → new `page-{idx}` task is added
- Trimming a crop: The crop filename stays the same → task_id stays the same → task is in `surviving` → BUT the fragment file must also be deleted because the image changed. This is handled by the trim endpoint calling `_remove_fragment_for_task()` before `on_crop_mutation()`

### 2.3 Trim-Specific Fragment Invalidation

Trim modifies the crop image in place (same filename, different content). The task_id is the same but the fragment is stale:

```python
# In /trim handler:
fragments_dir = sm.get_extraction_fragments_dir(session_id)
task_id = os.path.splitext(crop_filename)[0]  # same as task_id
fragment_path = os.path.join(fragments_dir, f"{task_id}.html")
if os.path.exists(fragment_path):
    os.unlink(fragment_path)

# Then apply the trim to the image
# Then call on_crop_mutation() which will reset the task's status to pending
```

Actually, `on_crop_mutation()` only handles removed tasks. For trim (same task_id, stale fragment), we need an explicit step **before** calling `on_crop_mutation()`:

```python
# /trim handler (corrected):
fragments_dir = sm.get_extraction_fragments_dir(session_id)
task_id = os.path.splitext(crop_filename)[0]
_fragment_path = os.path.join(fragments_dir, f"{task_id}.html")
if os.path.exists(_fragment_path):
    os.unlink(_fragment_path)

# Apply trim to image
# ...

# Reconcile — but we need to reset this task's status too
with sm.metadata_lock(session_id):
    meta = sm.load_meta(session_id)
    # Reset task status for the trimmed crop
    for task in meta.get("extraction_tasks", []):
        if task["task_id"] == task_id:
            task["extraction_status"] = "pending"
            task["extraction_error"] = None
            task["extraction_error_type"] = None
            task["fragment_path"] = None
    # Remove .complete marker
    _remove_output_marker(session_id, sm)
    sm.save_meta_atomic(session_id, meta)
```

### 2.4 Fragment Layout

```
uploads/<session_id>/
  extraction_fragments/
    page-0.html       # Simple page 0 (whole-page extraction)
    page-2.html       # Simple page 2
    crop_003.html     # Complex page, crop 3
    crop_007.html     # Complex page, crop 7
```

### 3.2 Extraction Job Model

**Contract: Background Job with Subscribers**

The extraction job runs in a background thread **independent of SSE requests**. SSE clients are **subscribers** that observe the job's progress by polling disk state.

```python
class ExtractionJob:
    """Background extraction job for a session."""

    def __init__(self, session_id, sm, crop_root, model, max_workers=4):
        self.session_id = session_id
        self.sm = sm
        self.crop_root = crop_root
        self.model = model
        self.max_workers = max_workers

        self.cancel_event = threading.Event()
        self.done_event = threading.Event()  # Set when job completes (success or failure)
        self.abort_flag = threading.Event()  # Set on non-retryable error

        self.result = None  # Final result: "done", "error", or "cancelled"
        self.error_message = None
        self.error_type = None  # "auth", "credits", "retryable"

    def run(self):
        """Execute the extraction job. Called in background thread."""
        try:
            self._execute_extraction()
            self.result = "done" if not self.abort_flag.is_set() else "error"
        except Exception as e:
            self.result = "error"
            self.error_message = str(e)
            self.error_type = "retryable"
        finally:
            self.done_event.set()

    def _execute_extraction(self):
        """Core extraction logic (detailed in §3.3)."""
        # ... (see §3.3)
        pass
```

**Why this is correct**:
1. **Job independence**: The job runs to completion regardless of SSE client connections/disconnections
2. **Multiple observers**: Multiple SSE clients can subscribe to the same job via `done_event`
3. **Clean cancellation**: `cancel_event` is only set on explicit user cancellation, not on client disconnect
4. **Clear completion**: `done_event.is_set()` reliably indicates job completion

### 3.3 SSE Subscriber Pattern

```python
@app.route("/extract-progress/<session_id>", methods=["GET"])
def extraction_progress_sse(session_id):
    """Stream extraction progress as SSE events."""

    def generate():
        yield _sse_event({"status": "starting"})

        # Poll disk state and yield progress events
        while True:
            # Get current task statuses from meta.json
            meta = sm.load_meta(session_id)
            tasks = meta.get("extraction_tasks", [])

            # Count completed/total tasks
            completed = sum(1 for t in tasks if t["extraction_status"] == "extracted")
            total = len(tasks)

            # Check if job is still running
            job = _get_active_job(session_id)

            if job is None:
                # No job running
                if _output_complete(session_id):
                    yield _sse_event({"status": "done", "progress": completed, "total": total})
                    return
                else:
                    # Job never started or was cancelled
                    yield _sse_event({"status": "done", "progress": completed, "total": total})
                    return

            # Job is running, check completion
            if job.done_event.is_set():
                # Job finished
                if job.result == "done":
                    yield _sse_event({"status": "done", "progress": completed, "total": total})
                else:
                    yield _sse_event({
                        "status": "error",
                        "error_type": job.error_type,
                        "message": job.error_message,
                        "progress": completed,
                        "total": total
                    })
                return

            # Job still running, yield progress
            yield _sse_event({
                "status": "progress",
                "progress": completed,
                "total": total,
                "log": f"Extracted {completed}/{total} regions..."
            })

            # Wait before polling again (or until job completes)
            job.done_event.wait(timeout=0.5)

    return Response(generate(), mimetype="text/event-stream")
```

**Why this is correct**:
1. **Client disconnect doesn't cancel**: The generator exits on client disconnect (via `GeneratorExit`), but the job continues in the background
2. **Reconnect works**: A new SSE request finds the existing job via `_get_active_job()` and subscribes to it
3. **No race condition**: `done_event.wait(timeout=0.5)` ensures we don't busy-loop and we wake up immediately when job completes

### 3.4 Job Execution: Bounded Submission

```python
def _execute_extraction(self):
    """Core extraction logic with bounded submission."""

    # 1. Reconcile tasks (see §2.2)
    on_crop_mutation(self.session_id, self.sm)

    # 2. Load current state
    meta = self.sm.load_meta(self.session_id)
    tasks = meta.get("extraction_tasks", [])

    # 3. Reconcile filesystem with meta.json (see §12.1)
    self._reconcile_filesystem_state(meta)

    # 4. Determine what needs to run
    tasks_to_run = [t for t in tasks if t["extraction_status"] != "extracted"]

    if not tasks_to_run:
        # All tasks already extracted, just run assembly
        self._run_assembly(meta)
        return

    # 5. Set up bounded submission
    max_workers = min(self.max_workers, len(tasks_to_run))
    semaphore = threading.Semaphore(max_workers)
    futures = []

    # 6. Submit tasks with bounded concurrency
    for task in tasks_to_run:
        # Check abort/cancel BEFORE acquiring semaphore
        if self.cancel_event.is_set() or self.abort_flag.is_set():
            break

        # Acquire semaphore (blocks if max_workers already running)
        semaphore.acquire()

        # Submit task
        future = self.executor.submit(self._extract_task, task, semaphore)
        futures.append(future)

    # 7. Wait for all futures to complete
    for future in futures:
        try:
            future.result()  # Wait for completion and get any exception
        except Exception as e:
            # Log error, don't crash the job
            self.error_message = str(e)

    # 8. Check if we should assemble
    if self.cancel_event.is_set():
        self.result = "cancelled"
        return

    if self.abort_flag.is_set():
        self.result = "error"
        return

    # Check for failed tasks
    meta = self.sm.load_meta(self.session_id)
    failed = [t for t in meta["extraction_tasks"] if t["extraction_status"] == "failed"]
    if failed:
        self.result = "error"
        self.error_message = f"{len(failed)} task(s) failed"
        self.error_type = "retryable"
        return

    # 9. Run assembly
    self._run_assembly(meta)
    self.result = "done"
```

**Why this is correct**:
1. **No submission after abort**: We check `abort_flag` BEFORE acquiring the semaphore, so we stop submitting immediately
2. **No deadlock on zero tasks**: If `tasks_to_run` is empty, the loop doesn't execute (no semaphore acquire)
3. **Bounded concurrency**: `Semaphore(max_workers)` ensures at most `max_workers` tasks run concurrently
4. **Semaphore released**: Each worker calls `semaphore.release()` when done (see §3.5)
5. **Proper draining**: We call `future.result()` on all submitted futures, waiting for them to complete before proceeding

### 3.5 Worker Implementation

```python
def _extract_task(self, task, semaphore):
    """Extract a single task. Called in thread pool worker."""
    try:
        # Get image path
        meta = self.sm.load_meta(self.session_id)
        page_info = meta["pages"][task["page_idx"]]

        if task["kind"] == "page":
            # Whole-page task
            image_path = os.path.join(self.page_dir, page_info["path"])
        else:
            # Crop task
            crop_path = os.path.join(self.crop_root, self.session_id, task["crop_filename"])
            image_path = crop_path

        # Extract HTML (with retry, see §6)
        html_fragment = extract_crop_as_html(image_path, self.model)

        # Write fragment atomically
        fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)
        fragment_path = os.path.join(fragments_dir, f"{task['task_id']}.html")
        _write_file_atomic(fragment_path, html_fragment)

        # Update meta.json
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            for t in meta["extraction_tasks"]:
                if t["task_id"] == task["task_id"]:
                    t["extraction_status"] = "extracted"
                    t["fragment_path"] = f"extraction_fragments/{task['task_id']}.html"
                    t["extraction_error"] = None
                    t["extraction_error_type"] = None
            self.sm.save_meta_atomic(self.session_id, meta)

    except PipelineCallError as e:
        # Persist failure to meta.json
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            for t in meta["extraction_tasks"]:
                if t["task_id"] == task["task_id"]:
                    t["extraction_status"] = "failed"
                    t["extraction_error"] = e.message
                    t["extraction_error_type"] = e.error_type
            self.sm.save_meta_atomic(self.session_id, meta)

        # Check if this is a non-retryable error
        if e.error_type in ("auth", "credits"):
            self.abort_flag.set()

    except Exception as e:
        # Unexpected error
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            for t in meta["extraction_tasks"]:
                if t["task_id"] == task["task_id"]:
                    t["extraction_status"] = "failed"
                    t["extraction_error"] = str(e)
                    t["extraction_error_type"] = "retryable"
            self.sm.save_meta_atomic(self.session_id, meta)

    finally:
        # Always release semaphore
        semaphore.release()
```

**Why this is correct**:
1. **Error persistence**: Every failure is immediately persisted to meta.json
2. **Abort on auth/credits**: Non-retryable errors set `abort_flag`, which stops submission (see §3.4 step 6)
3. **Semaphore release**: Always released in `finally`, preventing deadlock
4. **Lock safety**: We use `metadata_lock` to protect read-modify-write operations

### 3.6 Job Lifecycle Management

```python
# Module-level registry
_active_jobs = {}  # session_id → ExtractionJob
_jobs_lock = threading.Lock()  # Protects _active_jobs

def _get_active_job(session_id):
    """Get the active job for a session, or None."""
    with _jobs_lock:
        return _active_jobs.get(session_id)

def _start_extraction_job(session_id, sm, crop_root, model, max_workers=4):
    """Start a new extraction job if none is running."""
    with _jobs_lock:
        # Check if job already exists
        existing = _active_jobs.get(session_id)
        if existing and not existing.done_event.is_set():
            return existing  # Return existing job

        # Create new job
        job = ExtractionJob(session_id, sm, crop_root, model, max_workers)
        _active_jobs[session_id] = job

        # Start in background thread
        thread = threading.Thread(target=job.run, daemon=True)
        thread.start()

        return job

def _cleanup_completed_jobs():
    """Remove completed jobs from registry."""
    with _jobs_lock:
        completed = [sid for sid, job in _active_jobs.items() if job.done_event.is_set()]
        for sid in completed:
            del _active_jobs[sid]
```

**SSE route integration**:

```python
@app.route("/extract-html/<session_id>", methods=["POST"])
def start_extraction(session_id):
    """Start extraction job."""
    # Check prerequisites
    meta = sm.load_meta(session_id)
    if not _all_pages_analyzed(meta):
        return jsonify({"status": "error", "message": "Not all pages analyzed"}), 400

    # Start job (or return existing)
    job = _start_extraction_job(session_id, sm, crop_root, model)

    return jsonify({"status": "started", "message": "Extraction started"})

@app.route("/extract-progress/<session_id>", methods=["GET"])
def extraction_progress_sse(session_id):
    """Stream extraction progress (see §3.3)."""
    # ...
```

**Why this is correct**:
1. **No duplicate jobs**: `_start_extraction_job()` checks for existing job before creating new one
2. **Auto-cleanup**: Completed jobs are removed from registry
3. **Thread-safe**: `_jobs_lock` protects the registry

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
    """Run assembly and write .complete marker."""
    output_dir = self.output_dir

    # Write page files
    for page_idx, page_data in enumerate(pages_data):
        page_path = os.path.join(output_dir, f"page-{page_idx}.html")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(page_data["html"])

    # Write index.html
    index_path = os.path.join(output_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)

    # Write .complete marker atomically
    marker = {"timestamp": time.time()}
    marker_path = os.path.join(output_dir, ".complete")

    # Write to temp file first
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(marker, f)
        # Atomic replace
        os.replace(tmp_path, marker_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

### 5.3 Deleting .complete on Mutation

Every crop mutation must delete `.complete` if it exists:

```python
def _remove_output_marker(session_id, sm, output_dir):
    """Delete .complete marker if it exists."""
    output_session_dir = os.path.join(output_dir, session_id)
    marker_path = os.path.join(output_session_dir, ".complete")
    if os.path.exists(marker_path):
        os.unlink(marker_path)
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

When a client reconnects to `/extract-progress/<session_id>`:

- **Job already running**: Subscribe to the active job's `done_event` (see §3.6 Job Lifecycle Management)
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
| `table_extractor/html_extractor.py` | Add `ExtractionJob` class with background thread execution (`done_event`, `abort_flag`). Rewrite `_execute_extraction()` for bounded submission and task shape reconciliation. Add `derive_required_tasks()` and `on_crop_mutation()`. Wrap `_call()` in retry. Blank response detection + stale cache delete-and-retry. |
| `table_extractor/cache.py` | Export `_cache_key()` and `CACHE_DIR` for stale blank cache deletion from `html_extractor.py`. |
| `crop_app/crop_manager.py` | Rewrite `_next_crop_index()` to use persistent `next_crop_id` from `meta.json` instead of file-count-based counter. |
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
- Mock 401 → verify bounded submission stops → abort → persist results → terminal error → fix key → resume → verify success
- Mock 429 → verify Retry-After delay → verify eventual success
- Mock blank response → verify retry → verify success → verify fragment written
- Pre-populate cache with blank entry → verify cache entry deleted → verify re-call succeeds → verify cache overwritten
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

- Process death and restart: verify disk-state-only recovery, no stale job in registry
- Network disconnect and reconnect: verify resume from disk state
- Same image, different prompt → verify cache miss (prompt in extra_key)
- `max_attempts=3` semantics → verify exactly 3 total calls, not 3 retries
