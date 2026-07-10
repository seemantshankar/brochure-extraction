# crop_app/llm.py
from __future__ import annotations
import os
import json
import base64
import io
from openai import OpenAI
from PIL import Image
from table_extractor.cache import cached_call
from table_extractor.retry import (
    retry_with_backoff,
    MalformedOutputError,
    PipelineCallError,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1"

def _load_env():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)

_load_env()

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=OPENROUTER_URL,
            api_key=os.environ["OPENROUTER_API_KEY"],
            timeout=120.0,
        )
    return _client

MODEL_ID = os.environ["PAGE_ANALYSIS_MODEL_ID"]

ANALYSIS_PROMPT = """You are analyzing a single page of a product brochure / spec sheet.
Your job is to determine whether the page is "Simple" or "Complex" for processing by a small and cheap vision LLM.

Classify as "Simple" only when BOTH of the following condition groups are met:

Positive Indicators — at least ONE must be true:
1. The page only contains images with no text (0% text).
2. The page contains less than 60% text overall (e.g., a hero photo plus a short paragraph, or an image grid with short captions).
3. The page contains simple tables (no sub-sections, no row/column spans, no merges) with less than 60% text overall.
4. All text on the page is bold/large-font (approximately >18pt and high weight).

Negative / General Constraints — BOTH must be true:
5. The page does NOT contain too many or complex symbols (e.g., ^^#, **#, ^^^, etc.).
6. The page can be easily and confidently scanned by a small/cheap vision LLM.

If you cannot confidently satisfy both groups, classify as "Complex".

Respond with ONLY valid JSON in this exact format:
{
  "classification": "Simple" or "Complex"
}

Do NOT include any other text, explanation, or markdown formatting outside the JSON.
"""


def analyze_page(image_path: str) -> dict:
    """Send a page image to the LLM and return classification.

    Returns: {"classification": "Simple"|"Complex"|None, "error": str|None}
    On API failure returns classification None (never "Complex").
    """
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

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
            image_bytes=image_bytes,
            stage="analyze",
            model=MODEL_ID,
            fn=lambda: [retry_with_backoff(_call_api)],
            force=False,
            extra_key=ANALYSIS_PROMPT,
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



def analyze_pages(page_paths: list[str]) -> list[dict]:
    """Analyze multiple pages sequentially. Returns list of results."""
    return [analyze_page(p) for p in page_paths]
