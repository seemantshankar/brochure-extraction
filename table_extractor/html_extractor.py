"""Extract HTML fragments from brochure crops using an LLM and assemble pages."""
from __future__ import annotations
import io
import os
import re
import base64
import logging
import concurrent.futures
import threading
import tempfile
import time
import json
from functools import lru_cache
from PIL import Image
from openai import OpenAI
from table_extractor.cache import cached_call, CACHE_DIR, _cache_key
from table_extractor.retry import (
    PipelineCallError,
    retry_with_backoff,
    BlankResponseError,
    is_blank_fragment,
    RetryableError,
)


logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts", "html")

OPENROUTER_URL = "https://openrouter.ai/api/v1"

EXTRACTION_MAX_WORKERS = int(os.environ.get("EXTRACTION_MAX_WORKERS", "4"))

MAX_EXTRACTION_MAX_TOKENS = 65536

_client = None


def _get_client():
    """Return a cached OpenAI client pointing at OpenRouter."""
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=OPENROUTER_URL,
            api_key=os.environ["OPENROUTER_API_KEY"],
            timeout=120.0,
        )
    return _client


def load_prompt(filename: str) -> str:
    """Load a prompt file from the prompts/html directory."""
    filepath = os.path.join(PROMPTS_DIR, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


@lru_cache(maxsize=1)
def load_full_prompt() -> str:
    """Combine the master prompt with all extraction hint prompts."""
    master = load_prompt("_master.txt")
    hint_files = sorted(
        f for f in os.listdir(PROMPTS_DIR)
        if f.startswith("extract_") and f.endswith(".txt")
    )
    hints = [load_prompt(hf) for hf in hint_files]
    return master + "\n\n" + "\n\n".join(hints)


def clean_up_html_fragment(raw: str) -> str:
    """Strip markdown fences, backtick blocks, and surrounding whitespace from raw LLM output."""
    text = raw.strip()
    if not text:
        return ""

    fenced_pattern = re.search(
        r"^```\s*(?:html|HTML)?\s*\n(.*?)\n```\s*$", text, re.DOTALL
    )
    if fenced_pattern:
        text = fenced_pattern.group(1).strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:]
        while text.startswith("\n"):
            text = text[1:]
        if text.endswith("\n```"):
            text = text[:-4]
        text = text.strip()

    return text


def _get_classification(page_info: dict) -> str:
    """Return a page's classification, falling back to the legacy complex flag."""
    classification = page_info.get("classification")
    if classification is None:
        classification = "Complex" if page_info.get("complex") else "Simple"
    return classification


def _get_sorted_crops(page_info: dict) -> list:
    """Return a page's crops sorted by vertical position."""
    crops = page_info.get("crops") or []
    return sorted(crops, key=lambda c: c.get("bbox", [0, 0, 0, 0])[1])


def extract_crop_as_html(crop_image: Image.Image, model: str) -> str:
    """Send one crop image to the LLM and return an HTML fragment string."""
    system_prompt = load_full_prompt()

    buf = io.BytesIO()
    crop_image.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    max_tokens = 16384

    def _call():
        """Make the cached LLM call for this crop."""
        nonlocal max_tokens
        response = _get_client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        }
                    ],
                },
            ],
            max_tokens=max_tokens,
        )

        choices = response.choices
        if not choices:
            raise BlankResponseError("LLM returned no choices (empty or null response)")
        first_choice = choices[0]
        if first_choice is None or first_choice.message is None:
            raise BlankResponseError("LLM returned an empty choice or message")
        raw_content = first_choice.message.content or ""
        html_fragment = clean_up_html_fragment(raw_content)

        finish_reason = getattr(first_choice, "finish_reason", None)
        if finish_reason == "length":
            logger.warning(
                "LLM HTML output truncated (finish_reason=length) for model=%s, max_tokens=%s",
                model,
                max_tokens,
            )
            max_tokens = min(max_tokens * 2, MAX_EXTRACTION_MAX_TOKENS)
            raise RetryableError(
                f"LLM output truncated (finish_reason=length) at max_tokens={max_tokens}"
            )

        if is_blank_fragment(html_fragment):
            raise BlankResponseError("LLM returned an empty/blank HTML fragment")

        usage_meta = {}
        if response.usage:
            usage_meta = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }

        return [html_fragment, usage_meta]

    def _cached_extract(force=False):
        return cached_call(
            image_bytes=img_bytes,
            stage="html_extract",
            model=model,
            fn=lambda: retry_with_backoff(_call),
            force=force,
            extra_key=system_prompt,
        )

    def _invalidate_cache():
        cache_key = _cache_key(img_bytes, "html_extract", model, system_prompt)
        cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
        if os.path.exists(cache_file):
            os.unlink(cache_file)

    try:
        result = _cached_extract()

        # Defensive: a corrupt cache entry (e.g. None / not a list) must never
        # reach the subscript below. Discard it and fetch fresh.
        if not isinstance(result, (list, tuple)) or len(result) < 1:
            _invalidate_cache()
            result = _cached_extract(force=True)

        # Stale blank cache invalidation
        if is_blank_fragment(result[0]):
            _invalidate_cache()
            result = _cached_extract(force=True)
    except Exception as e:
        logger.error(f"LLM extraction failed for crop (model={model}): {e}")
        raise

    return result[0]


class ExtractionInProgressError(RuntimeError):
    pass


def _write_complete_marker(session_output_dir: str) -> None:
    """Write .complete marker atomically via temp file + os.replace."""
    fd, tmp_path = tempfile.mkstemp(dir=session_output_dir, suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time()}, f)
        os.replace(tmp_path, os.path.join(session_output_dir, ".complete"))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _output_complete(session_id: str) -> bool:
    session_output_dir = os.path.join(_OUTPUT_ROOT, session_id)
    return os.path.exists(os.path.join(session_output_dir, ".complete"))


# Module-level output root, set by app.py at startup via set_output_root().
_OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crop_app", "static", "extracted")


def set_output_root(output_root: str) -> None:
    """Allow app.py to set the extracted output root."""
    global _OUTPUT_ROOT
    _OUTPUT_ROOT = output_root


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
        self.result = None
        self.error_message = None
        self.error_type = None

    def run(self):
        try:
            self._execute_extraction()
            if self.result is None:
                self.result = "error"
                self.error_message = "Job returned without setting outcome"
                self.error_type = "retryable"
        except Exception as e:
            msg = str(e)
            if "shutdown" in msg:
                # Executor was torn down (e.g. server restart). Surfaced as a
                # resumable interruption rather than a cryptic internal error.
                self.result = "error"
                self.error_message = "Extraction interrupted by server restart. Click Retry to resume."
                self.error_type = "retryable"
            else:
                self.result = "error"
                self.error_message = msg
                self.error_type = "retryable"
        finally:
            _clear_extraction_in_progress(self.session_id)
            self.done_event.set()
            self.executor.shutdown(wait=False)

    def _run_assembly(self, meta):
        from table_extractor.html_assembler import write_page_files
        output_dir = self.output_dir
        fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)
        session_output_dir = os.path.join(output_dir, self.session_id)
        os.makedirs(session_output_dir, exist_ok=True)
        _remove_output_marker(self.session_id, output_dir)

        pages_data = []
        for page_idx, page_info in enumerate(meta.get("pages", [])):
            page_tasks = [
                t for t in meta.get("extraction_tasks", [])
                if t["page_idx"] == page_idx and t["extraction_status"] == "extracted"
            ]
            fragments = []
            for task in page_tasks:
                frag_path = os.path.join(fragments_dir, os.path.basename(task["fragment_path"]))
                with open(frag_path, "r", encoding="utf-8") as f:
                    fragments.append(f.read())
            pages_data.append({"html": "\n".join(fragments)})

        session_files = meta.get("files", [])
        title = session_files[0] if session_files else f"Session {self.session_id[:8]}"
        write_page_files(self.session_id, pages_data, title, output_root=output_dir)
        _write_complete_marker(session_output_dir)

    def _execute_extraction(self):
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            desired = derive_required_tasks(meta)
            reconcile_tasks(meta, desired, self.sm.get_extraction_fragments_dir(self.session_id))
            self.sm.save_meta_atomic(self.session_id, meta)
            
            # If any task is not yet extracted, remove stale complete marker.
            tasks = meta.get("extraction_tasks", [])
            if any(t.get("extraction_status") != "extracted" for t in tasks):
                _remove_output_marker(self.session_id, self.output_dir)

        meta = self.sm.load_meta(self.session_id)
        tasks = meta.get("extraction_tasks", [])

        def _eligible(task):
            if task["extraction_status"] == "extracted":
                return False
            if not self.retry_nonretryable and task["extraction_status"] == "failed" \
               and task.get("extraction_error_type") in ("auth", "credits"):
                return False
            return True

        tasks_to_run = [t for t in tasks if _eligible(t)]

        if not tasks_to_run:
            failed = [t for t in tasks if t["extraction_status"] == "failed"]
            if failed:
                terminal = next((t for t in failed if t["extraction_error_type"] in ("auth", "credits")), failed[0])
                self.result = "error"
                self.error_message = terminal.get("extraction_error") or "Tasks failed"
                self.error_type = terminal.get("extraction_error_type") or "retryable"
                return
            self._run_assembly(meta)
            self.result = "done"
            return

        max_workers = min(self.max_workers, len(tasks_to_run))
        if max_workers <= 0:
            self.result = "error"
            self.error_message = "No eligible tasks"
            self.error_type = "retryable"
            return

        semaphore = threading.Semaphore(max_workers)
        futures = []
        for task in tasks_to_run:
            if self.cancel_event.is_set() or self.abort_flag.is_set():
                break
            semaphore.acquire()
            if self.cancel_event.is_set() or self.abort_flag.is_set():
                semaphore.release()
                break
            future = self.executor.submit(self._extract_task, task, semaphore)
            futures.append(future)

        for future in futures:
            try:
                future.result()
            except PipelineCallError:
                pass
            except Exception:
                pass

        if self.cancel_event.is_set() and not self.abort_flag.is_set():
            self.result = "cancelled"
            return

        meta = self.sm.load_meta(self.session_id)
        failed = [t for t in meta["extraction_tasks"] if t["extraction_status"] == "failed"]
        if failed:
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

        self._run_assembly(meta)
        self.result = "done"

    def _extract_task(self, task, semaphore):
        try:
            meta = self.sm.load_meta(self.session_id)
            page_info = meta["pages"][task["page_idx"]]
            if task["kind"] == "page":
                page_path = os.path.join(self.page_dir, page_info["path"])
                with Image.open(page_path) as img:
                    crop_img = img.copy()
            else:
                crop_path = os.path.join(self.crop_root, self.session_id, task["crop_filename"])
                with Image.open(crop_path) as img:
                    crop_img = img.copy()

            html_fragment = extract_crop_as_html(crop_img, self.model)
            fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)
            fragment_path = os.path.join(fragments_dir, f"{task['task_id']}.html")
            _write_file_atomic(fragment_path, html_fragment)

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


def _write_file_atomic(path: str, content: str) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".html.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Job registry ---
_active_jobs = {}
_jobs_lock = threading.Lock()


def _get_active_job(session_id):
    with _jobs_lock:
        return _active_jobs.get(session_id)


def _start_extraction_job(session_id, sm, crop_root, page_dir, output_dir,
                          model, max_workers=4, retry_nonretryable=False):
    """Create and start a new extraction job. Atomically registers in-progress state."""
    _cleanup_completed_jobs()
    with _jobs_lock:
        existing = _active_jobs.get(session_id)
        if existing and not existing.done_event.is_set():
            raise ExtractionInProgressError("Job already running for session")
        job = ExtractionJob(
            session_id=session_id, sm=sm, crop_root=crop_root, page_dir=page_dir,
            output_dir=output_dir, model=model, max_workers=max_workers,
            retry_nonretryable=retry_nonretryable,
        )
        _active_jobs[session_id] = job
        _set_extraction_in_progress(session_id)
        thread = threading.Thread(target=job.run, name=f"extract-{session_id[:8]}", daemon=True)
        thread.start()
        return job


def _cleanup_completed_jobs():
    """Drop finished jobs from the registry so SSE observers read disk state."""
    with _jobs_lock:
        for sid in list(_active_jobs.keys()):
            job = _active_jobs[sid]
            if job.done_event.is_set():
                del _active_jobs[sid]


# --- Extraction-in-progress guard (module level) ---
_extraction_in_progress = set()
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


def _remove_output_marker(session_id: str, output_dir: str) -> None:
    session_dir = os.path.join(output_dir, session_id)
    marker_path = os.path.join(session_dir, ".complete")
    if os.path.exists(marker_path):
        os.unlink(marker_path)


def derive_required_tasks(meta: dict) -> list[dict]:
    """Derive the list of required tasks based on page analysis.

    For each page:
    - If analysis_status != "done", skip page.
    - If classification == "Simple", create a single page task.
    - If classification == "Complex" and there are crops, create sorted crop tasks.
    - If classification == "Complex" and there are no crops, fall back to page task.
    """
    tasks = []
    for page_idx, page_info in enumerate(meta.get("pages", [])):
        if page_info.get("analysis_status") != "done":
            continue

        classification = _get_classification(page_info)
        crops = _get_sorted_crops(page_info)

        if classification == "Simple":
            tasks.append({
                "task_id": f"page-{page_idx}",
                "page_idx": page_idx,
                "kind": "page",
                "image_source": "page",
            })
        elif classification == "Complex":
            if crops:
                for crop_info in crops:
                    crop_filename = (
                        crop_info.get("filename")
                        or crop_info.get("path")
                        or crop_info.get("crop_filename")
                    )
                    if not crop_filename:
                        continue
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


def reconcile_tasks(meta: dict, desired_tasks: list, fragments_dir: str) -> None:
    """Reconcile meta["extraction_tasks"] with desired set. In-place mutation.

    - Tasks whose task_id is in desired set: preserve status and fragment_path
    - Tasks whose task_id is NOT in desired set: dropped from the list
    - Desired tasks not yet in the list: added with "pending" status (unless fragment exists)
    - If a surviving task has extraction_status=="extracted" but fragment file
      is missing from disk: reset to "pending"
    - If a surviving task has status!="extracted" but fragment file exists:
      upgrade to "extracted" and set fragment_path
    """
    existing_tasks = meta.get("extraction_tasks", [])
    existing_by_id = {t["task_id"]: t for t in existing_tasks}

    final = []
    for desired in desired_tasks:
        tid = desired["task_id"]
        if tid in existing_by_id:
            existing = existing_by_id[tid]
            # Filesystem-level validation
            frag_path = existing.get("fragment_path")
            if frag_path:
                has_fragment = os.path.exists(os.path.join(fragments_dir, os.path.basename(frag_path)))
            else:
                has_fragment = os.path.exists(os.path.join(fragments_dir, f"{tid}.html"))

            if existing.get("extraction_status") == "extracted" and not has_fragment:
                # Fragment lost — reset to pending
                existing["extraction_status"] = "pending"
                existing["fragment_path"] = None
                existing["extraction_error"] = None
                existing["extraction_error_type"] = None
            elif existing.get("extraction_status") != "extracted" and has_fragment:
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

def normalize_legacy_meta(meta: dict, fragments_dir: str = None, crop_dir: str = None) -> dict:
    """Return a normalized copy of meta with additive fields defaulted.

    Handles legacy sessions lacking analysis_status / extraction_tasks / next_crop_id.
    `next_crop_id` defaults to max(existing numeric crop IDs in crop_dir) + 1 (never 0
    when crops already exist), so legacy crop filenames are never reused.
    """
    meta = dict(meta)  # shallow copy; we will replace nested lists with new lists
    pages = []
    for page in meta.get("pages", []):
        page = dict(page)
        if "analysis_status" not in page:
            if page.get("classification") in ("Simple", "Complex"):
                page["analysis_status"] = "done"
            elif page.get("complex"):
                page["analysis_status"] = "done"
                page["classification"] = "Complex"
            else:
                page["analysis_status"] = "pending"
        pages.append(page)
    meta["pages"] = pages

    if "extraction_tasks" not in meta:
        desired = derive_required_tasks(meta)
        tasks = []
        for d in desired:
            frag_name = f"{d['task_id']}.html"
            has_frag = bool(fragments_dir and os.path.exists(os.path.join(fragments_dir, frag_name)))
            if has_frag:
                tasks.append({**d, "extraction_status": "extracted",
                              "fragment_path": f"extraction_fragments/{frag_name}",
                              "extraction_error": None, "extraction_error_type": None})
            else:
                tasks.append({**d, "extraction_status": "pending",
                              "fragment_path": None, "extraction_error": None,
                              "extraction_error_type": None})
        meta["extraction_tasks"] = tasks

    if "next_crop_id" not in meta:
        max_id = -1
        if crop_dir and os.path.isdir(crop_dir):
            for fname in os.listdir(crop_dir):
                if fname.startswith("crop_") and fname.endswith(".png"):
                    digits = fname[len("crop_"):-len(".png")]
                    if digits.isdigit():
                        max_id = max(max_id, int(digits))
        meta["next_crop_id"] = max_id + 1

    return meta


def on_crop_mutation(meta: dict, sm, session_id: str, output_dir: str) -> None:
    """Invalidate stale fragments/tasks after a crop mutation. In-place on meta."""
    if _is_extraction_in_progress(session_id):
        raise RuntimeError("Extraction job is running — cancel it or wait before mutating crops")
    desired_tasks = derive_required_tasks(meta)
    desired_ids = {t["task_id"] for t in desired_tasks}
    existing_tasks = meta.get("extraction_tasks", [])
    fragments_dir = sm.get_extraction_fragments_dir(session_id)

    for task in existing_tasks:
        if task["task_id"] not in desired_ids:
            frag_path = task.get("fragment_path")
            if frag_path:
                full_path = os.path.join(fragments_dir, os.path.basename(frag_path))
                if os.path.exists(full_path):
                    os.unlink(full_path)

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
