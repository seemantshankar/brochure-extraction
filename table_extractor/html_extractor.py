from __future__ import annotations
"""Extract HTML fragments from brochure crops using an LLM and assemble pages."""
import io
import os
import re
import html
import base64
import logging
import concurrent.futures
import threading
from functools import lru_cache
from PIL import Image
from openai import OpenAI
from table_extractor.cache import cached_call
from table_extractor.retry import PipelineCallError

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts", "html")

OPENROUTER_URL = "https://openrouter.ai/api/v1"

EXTRACTION_MAX_WORKERS = int(os.environ.get("EXTRACTION_MAX_WORKERS", "4"))

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

    def _call():
        """Make the cached LLM call for this crop."""
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
            max_tokens=8192,
        )

        raw_content = response.choices[0].message.content or ""
        html_fragment = clean_up_html_fragment(raw_content)

        usage_meta = {}
        if response.usage:
            usage_meta = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }

        return [html_fragment, usage_meta]

    try:
        result = cached_call(
            image_bytes=img_bytes,
            stage="html_extract",
            model=model,
            fn=_call,
            force=False,
            extra_key=system_prompt,
        )
    except Exception as e:
        logger.error(f"LLM extraction failed for crop (model={model}): {e}")
        raise

    return result[0]


def run_extraction(
    session_id: str,
    sm,
    crop_root: str,
    model: str,
    max_workers: int = None,
    cancel_event: threading.Event = None,
    output_root: str = None,
):
    """Run full HTML extraction, yielding progress dicts and the final HTML document.

    Args:
        cancel_event: Optional threading.Event. When set, the extraction loop
            stops submitting work and yields a "cancelled" status before exiting.
        output_root: Optional root directory for extracted page files. Defaults
            to crop_app/static/extracted.

    Yields:
        {"status": "progress", "page": int, "totalPages": int, "log": str} — during extraction
        {"status": "cancelled"} — emitted when cancel_event is set mid-flight
        {"status": "done", "page_files": list, "index": str} — per-page file manifest
    """
    from table_extractor.html_assembler import write_page_files

    meta = sm.load_meta(session_id)
    if not meta:
        raise ValueError(f"Session {session_id} not found")

    pages = meta.get("pages", [])
    page_dir = sm.get_page_dir(session_id)
    session_files = meta.get("files", [])
    title = session_files[0] if session_files else f"Session {session_id[:8]}"
    total_pages = len(pages)

    tasks = []

    for page_idx, page_info in enumerate(pages):
        classification = _get_classification(page_info)
        crops = _get_sorted_crops(page_info)
        is_complex = classification == "Complex"

        if not is_complex or len(crops) == 0:
            tasks.append({
                "page_idx": page_idx,
                "crop_idx": None,
                "image_path": os.path.join(page_dir, page_info["path"]),
                "crop_filename": None,
                "is_simple": True,
            })
        else:
            for crop_idx, crop_info in enumerate(crops):
                crop_filename = (
                    crop_info.get("filename")
                    or crop_info.get("path")
                    or crop_info.get("crop_filename")
                )
                tasks.append({
                    "page_idx": page_idx,
                    "crop_idx": crop_idx,
                    "image_path": os.path.join(crop_root, session_id, crop_filename) if crop_filename else None,
                    "crop_filename": crop_filename,
                    "is_simple": False,
                })

    total_tasks = len(tasks)

    yield {
        "status": "progress",
        "page": 0,
        "totalPages": total_tasks,
        "log": f"Starting parallel extraction of {total_tasks} region(s) across {total_pages} page(s)...",
    }

    results = {}
    results_lock = threading.Lock()
    completed_count = {"count": 0}
    completed_lock = threading.Lock()

    def _process_task(task):
        """Extract HTML for a single page or crop task."""
        page_idx = task["page_idx"]
        crop_idx = task["crop_idx"]
        image_path = task["image_path"]
        crop_filename = task["crop_filename"]
        is_simple = task["is_simple"]

        if not image_path or not os.path.exists(image_path):
            if is_simple:
                fragment = '<div class="error-region">Page file not found</div>'
            elif not crop_filename:
                fragment = '<div class="error-region">Crop missing filename reference</div>'
            else:
                fragment = f'<div class="error-region">Crop file not found: {html.escape(crop_filename)}</div>'
        else:
            try:
                with Image.open(image_path) as img:
                    crop_img = img.copy()
                    crop_img.filename = image_path
                fragment = extract_crop_as_html(crop_img, model)
                if not fragment:
                    fragment = ""
            except Exception as e:
                logger.error(f"Extraction failed for page={page_idx} crop={crop_idx}: {e}")
                path_label = "page" if is_simple else crop_filename
                fragment = (
                    f'<div class="error-region">Extraction failed for '
                    f'{html.escape(str(path_label))}: {html.escape(str(e))}</div>'
                )

        return {
            "page_idx": page_idx,
            "crop_idx": crop_idx,
            "fragment": fragment,
        }

    workers = max_workers if max_workers is not None else EXTRACTION_MAX_WORKERS
    cancelled = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_task = {executor.submit(_process_task, t): t for t in tasks}

        try:
            for future in concurrent.futures.as_completed(future_to_task):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    for f in future_to_task:
                        f.cancel()
                    break

                task = future_to_task[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "page_idx": task["page_idx"],
                        "crop_idx": task["crop_idx"],
                        "fragment": f'<div class="error-region">Thread execution failed: {html.escape(str(e))}</div>',
                    }

                with results_lock:
                    results[(result["page_idx"], result["crop_idx"])] = result["fragment"]

                with completed_lock:
                    completed_count["count"] += 1
                    completed = completed_count["count"]

                yield {
                    "status": "progress",
                    "page": completed,
                    "totalPages": total_tasks,
                    "log": f"Extracted {completed}/{total_tasks} region(s)...",
                }
        finally:
            for f in future_to_task:
                f.cancel()

    if cancelled:
        yield {"status": "cancelled"}
        return

    yield {
        "status": "progress",
        "page": total_tasks,
        "totalPages": total_tasks,
        "log": "Assembling final HTML document...",
    }

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

    out_dir = output_root or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "crop_app", "static", "extracted",
    )
    write_page_files(session_id, pages_data, title, output_root=out_dir)

    yield {
        "status": "done",
        "page_files": [f"page-{i}.html" for i in range(len(pages_data))],
        "index": "index.html",
    }


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
    desired_ids = {t["task_id"] for t in desired_tasks}
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
