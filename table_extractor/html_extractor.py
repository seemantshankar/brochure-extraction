import io
import os
import re
import base64
import logging
from PIL import Image
from openai import OpenAI
from table_extractor.cache import cached_call

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts", "html")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=os.environ.get("OPENROUTER_API_KEY", "mock_key"),
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

    pages_data = []

    for page_idx, page_info in enumerate(pages):
        yield {
            "status": "progress",
            "page": page_idx,
            "totalPages": total_pages,
            "log": f"Processing Page {page_idx + 1} of {total_pages}...",
        }

        parts = []
        classification = page_info.get("classification")
        if classification is None:
            classification = "Complex" if page_info.get("complex") else "Simple"

        crops = page_info.get("crops") or []
        is_complex = classification == "Complex"

        if not is_complex or len(crops) == 0:
            page_path = os.path.join(page_dir, page_info["path"])
            if os.path.exists(page_path):
                try:
                    page_img = Image.open(page_path)
                    fragment = extract_crop_as_html(page_img, model)
                    if fragment:
                        parts.append(fragment)
                except Exception as e:
                    logger.error(f"Simple page extraction failed: {e}")
                    parts.append(f'<div class="error-region">Extraction failed: {e}</div>')
            else:
                parts.append(f'<div class="error-region">Page file not found: {page_info["path"]}</div>')
        else:
            sorted_crops = sorted(crops, key=lambda c: c.get("bbox", [0, 0, 0, 0])[1])
            total_crops = len(sorted_crops)
            for crop_idx, crop_info in enumerate(sorted_crops):
                crop_filename = (
                    crop_info.get("filename")
                    or crop_info.get("path")
                    or crop_info.get("crop_filename")
                )
                yield {
                    "status": "progress",
                    "page": page_idx,
                    "totalPages": total_pages,
                    "crop": crop_idx + 1,
                    "totalCrops": total_crops,
                    "log": f"  - Extracting crop {crop_idx + 1}/{total_crops} (Page {page_idx + 1})...",
                }
                if not crop_filename:
                    parts.append('<div class="error-region">Crop missing filename reference</div>')
                    continue
                crop_path = os.path.join(crop_root, session_id, crop_filename)
                if not os.path.exists(crop_path):
                    parts.append(f'<div class="error-region">Crop file not found: {crop_filename}</div>')
                    continue
                try:
                    crop_img = Image.open(crop_path)
                    fragment = extract_crop_as_html(crop_img, model)
                    if fragment:
                        parts.append(fragment)
                except Exception as e:
                    logger.error(f"Crop extraction failed for {crop_filename}: {e}")
                    parts.append(
                        f'<div class="error-region">Extraction failed for {crop_filename}: {e}</div>'
                    )

        pages_data.append({"html": "\n".join(parts)})

    yield {
        "status": "progress",
        "page": total_pages,
        "totalPages": total_pages,
        "log": "Assembling final HTML document...",
    }

    result_html = assemble_full_document(pages_data, title)

    yield {"status": "done", "html": result_html}
