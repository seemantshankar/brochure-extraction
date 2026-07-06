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
Your job is to determine whether the page contains **complex layout regions** that need manual cropping.

Complex layout regions include (but are not limited to):
- Tables (ruled, grid-based, data tables)
- Swatch grids (color/ fabric/ material swatches arranged in a grid)
- Image grids (product photos arranged in a grid)
- Text grids (spec lists, bullet panels, feature lists)
- Feature matrices (comparison tables with icons/labels)
- Stat cards (KPI boxes, metric callouts)
- Technical drawings / diagrams

A page is "complex" if it contains at least one of these layout types.
A page is "not complex" if it is only body text, headings, footnotes, or full-bleed images with no grid/table structure.

Respond with ONLY valid JSON in this exact format:
{
  "complex": true or false,
  "labels": ["list", "of", "detected", "types"]
}

If complex is false, labels should be an empty list.
Do NOT include any other text, explanation, or markdown formatting outside the JSON.
"""

LABELS = [
    "table", "ruled_table", "swatch_grid", "image_grid",
    "text_grid", "feature_matrix", "stat_cards",
    "technical_drawing", "bullet_panel"
]


def analyze_page(image_path: str) -> dict:
    """Send a page image to the LLM and return complexity analysis.

    Returns: {"complex": bool, "labels": list[str], "error": str|None}
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
        return {"complex": False, "labels": [], "error": str(e)}


def _parse_response(raw: str) -> dict:
    """Parse LLM JSON response, stripping markdown fences if present."""
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        start = 1
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {
                "complex": bool(data.get("complex", False)),
                "labels": [lbl for lbl in data.get("labels", []) if lbl in LABELS],
                "error": None,
            }
    except (json.JSONDecodeError, AttributeError):
        pass

    return {"complex": False, "labels": [], "error": f"Failed to parse: {raw[:200]}"}


def analyze_pages(page_paths: list[str]) -> list[dict]:
    """Analyze multiple pages sequentially. Returns list of results."""
    return [analyze_page(p) for p in page_paths]
