import io
import os
import re
import base64
import logging
import concurrent.futures
import threading
from PIL import Image
from openai import OpenAI
from table_extractor.cache import cached_call

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts", "html")

OPENROUTER_URL = "https://openrouter.ai/api/v1"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=OPENROUTER_URL,
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            timeout=120.0,
        )
    return _client


def load_prompt(filename: str) -> str:
    filepath = os.path.join(PROMPTS_DIR, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def load_full_prompt() -> str:
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


def extract_crop_as_html(crop_image: Image.Image, model: str) -> str:
    """Send one crop image to the LLM and return an HTML fragment string."""
    system_prompt = load_full_prompt()

    buf = io.BytesIO()
    crop_image.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    def _call():
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
        )
    except Exception as e:
        logger.error(f"LLM extraction failed for crop (model={model}): {e}")
        raise

    return result[0]


def run_extraction(session_id: str, sm, crop_root: str, model: str):
    """Run full HTML extraction, yielding progress dicts and the final HTML document.

    Yields:
        {"status": "progress", "page": int, "totalPages": int, "log": str} — during extraction
        {"status": "done", "html": str} — final assembled HTML document
    """
    from table_extractor.html_assembler import assemble_full_document

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
        classification = page_info.get("classification")
        if classification is None:
            classification = "Complex" if page_info.get("complex") else "Simple"

        crops = page_info.get("crops") or []
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
            sorted_crops = sorted(crops, key=lambda c: c.get("bbox", [0, 0, 0, 0])[1])
            for crop_idx, crop_info in enumerate(sorted_crops):
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
                fragment = f'<div class="error-region">Crop file not found: {crop_filename}</div>'
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
                fragment = f'<div class="error-region">Extraction failed for {path_label}: {e}</div>'

        return {
            "page_idx": page_idx,
            "crop_idx": crop_idx,
            "fragment": fragment,
        }

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_task = {executor.submit(_process_task, t): t for t in tasks}

        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
            except Exception as e:
                result = {
                    "page_idx": task["page_idx"],
                    "crop_idx": task["crop_idx"],
                    "fragment": f'<div class="error-region">Thread execution failed: {e}</div>',
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

    yield {
        "status": "progress",
        "page": total_tasks,
        "totalPages": total_tasks,
        "log": "Assembling final HTML document...",
    }

    pages_data = []
    for page_idx, page_info in enumerate(pages):
        classification = page_info.get("classification")
        if classification is None:
            classification = "Complex" if page_info.get("complex") else "Simple"

        crops = page_info.get("crops") or []
        is_complex = classification == "Complex"

        if not is_complex or len(crops) == 0:
            pages_data.append({"html": results.get((page_idx, None), "")})
        else:
            sorted_crops = sorted(crops, key=lambda c: c.get("bbox", [0, 0, 0, 0])[1])
            parts = []
            for crop_idx in range(len(sorted_crops)):
                fragment = results.get((page_idx, crop_idx), "")
                if fragment:
                    parts.append(fragment)
            pages_data.append({"html": "\n".join(parts)})

    result_html = assemble_full_document(pages_data, title)

    yield {"status": "done", "html": result_html}
