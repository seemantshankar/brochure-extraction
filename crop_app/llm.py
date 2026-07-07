# crop_app/llm.py
import os
import json
import base64
from openai import OpenAI
from PIL import Image

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

MODEL_ID = os.environ.get("PAGE_ANALYSIS_MODEL_ID", "openai/gpt-4o-mini")

ANALYSIS_PROMPT = """You are analyzing a single page of a product brochure / spec sheet.
Your job is to determine whether the page is "Simple" or "Complex" for processing by a small and cheap vision LLM.

Classify as "Simple" only when BOTH of the following condition groups are met:

Positive Indicators — at least ONE must be true:
1. The page only contains images with no text (0% text).
2. The page contains less than 60% text overall.
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

    Returns: {"classification": "Simple" | "Complex", "error": str|None}
    """
    try:
        img = Image.open(image_path)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Save as temp JPEG for embedding (smaller than PNG base64)
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        response = _get_client().chat.completions.create(
            model=MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ANALYSIS_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                    ],
                }
            ],
            reasoning_effort="minimal",
        )

        raw_content = response.choices[0].message.content or ""
        return _parse_response(raw_content)

    except Exception as e:
        return {"classification": "Complex", "error": str(e)}


def _parse_response(raw: str) -> dict:
    """Parse LLM JSON response, stripping markdown fences if present."""
    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            classification = data.get("classification", "Complex")
            if classification not in ("Simple", "Complex"):
                classification = "Complex"
            return {"classification": classification, "error": None}
    except (json.JSONDecodeError, AttributeError):
        pass

    return {"classification": "Complex", "error": f"Failed to parse: {raw[:200]}"}


def analyze_pages(page_paths: list[str]) -> list[dict]:
    """Analyze multiple pages sequentially. Returns list of results."""
    return [analyze_page(p) for p in page_paths]
