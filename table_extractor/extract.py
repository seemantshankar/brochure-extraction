import io
import os
import json
import base64
import httpx
import logging
from PIL import Image
from openai import OpenAI
from table_extractor.schemas import Region, ExtractedContent, RegionType
from table_extractor.cache import cached_call
from table_extractor.retry import RetryableError

logger = logging.getLogger(__name__)

MAX_EXTRACTION_MAX_TOKENS = 65536

client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
    api_key=os.environ["OPENROUTER_API_KEY"],
)

total_extraction_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

def get_total_usage() -> dict:
    return total_extraction_usage


EXTRACTION_SCHEMAS = {
    RegionType.RULED_TABLE: {
        "type": "object",
        "properties": {
            "columns": {"type": "array", "items": {"type": ["string", "object"]}},
            "rows": {"type": "array", "items": {"type": "object"}}
        },
        "required": ["columns", "rows"]
    },
    RegionType.SECTION_GROUPED_TABLE: {
        "type": "object",
        "properties": {
            "columns": {"type": "array", "items": {"type": ["string", "object"]}},
            "rows": {"type": "array", "items": {"type": "object"}}
        },
        "required": ["columns", "rows"]
    },
    RegionType.BULLET_PANEL: {
        "type": "object",
        "properties": {
            "tier_name": {"type": "string"},
            "inherits_from": {"type": "string", "nullable": True},
            "features": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["tier_name", "features"]
    },
    RegionType.SWATCH_GRID: {
        "type": "object",
        "properties": {
            "colors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "swatch_name": {"type": "string"},
                        "tone_type": {"type": "string", "enum": ["single", "dual"]},
                        "roof_color": {"type": "string", "nullable": True},
                        "body_color": {"type": "string", "nullable": True}
                    },
                    "required": ["swatch_name", "tone_type"]
                }
            }
        },
        "required": ["colors"]
    },
    RegionType.STAT_CARDS: {
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "card_label": {"type": "string"},
                        "variant": {"type": "string"},
                        "value": {"type": "string"},
                        "unit": {"type": "string"}
                    },
                    "required": ["card_label", "value"]
                }
            }
        },
        "required": ["cards"]
    },
    RegionType.TECHNICAL_DRAWING: {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": ["front", "rear", "side", "top", "plan"]},
            "measurements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "number"},
                        "unit": {"type": "string"}
                    },
                    "required": ["label", "value", "unit"]
                }
            }
        },
        "required": ["view", "measurements"]
    },
    RegionType.FOOTNOTE_BLOCK: {
        "type": "object",
        "properties": {
            "footnotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "marker": {"type": "string"},
                        "text": {"type": "string"}
                    },
                    "required": ["marker", "text"]
                }
            }
        },
        "required": ["footnotes"]
    },
    RegionType.ICON_BADGE: {
        "type": "object",
        "properties": {
            "badge_name": {"type": "string"},
            "description": {"type": "string"}
        },
        "required": ["badge_name"]
    },
    RegionType.OTHER: {
        "type": "object",
        "properties": {
            "markdown": {"type": "string"}
        },
        "required": ["markdown"]
    }
}

def _load_prompt(region_type: RegionType) -> str:
    prompt_file = f"extract_{region_type.value}.txt"
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", prompt_file)
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    return f"Extract structured details from this {region_type.value}."

def _fetch_generation_cost(generation_id: str) -> float:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return 0.0
    try:
        r = httpx.get(
            f"https://openrouter.ai/api/v1/generation?id={generation_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0
        )
        if r.status_code == 200:
            return r.json().get("data", {}).get("total_cost", 0.0)
    except Exception:
        pass
    return 0.0

def _call_extraction_api(image_bytes: bytes, region: Region, model: str) -> list:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    system_prompt = _load_prompt(region.region_type)
    schema = EXTRACTION_SCHEMAS.get(region.region_type, EXTRACTION_SCHEMAS[RegionType.OTHER])

    # Basic single-retry parser loop for robustness
    max_tokens = 16384
    retries = 2
    last_exc = None
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{base64_image}"}
                            }
                        ]
                    }
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "return_extracted_content",
                            "description": f"Extracts content of {region.region_type.value}",
                            "parameters": schema
                        }
                    }
                ],
                tool_choice={"type": "function", "function": {"name": "return_extracted_content"}},
                max_tokens=max_tokens,
            )
            
            msg = response.choices[0].message
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if finish_reason == "length":
                logger.warning(
                    "LLM tool-call output truncated (finish_reason=length) for model=%s, max_tokens=%s",
                    model,
                    max_tokens,
                )
                max_tokens = min(max_tokens * 2, MAX_EXTRACTION_MAX_TOKENS)
                raise RetryableError(
                    f"LLM output truncated (finish_reason=length) at max_tokens={max_tokens}"
                )
            
            logger.info("LLM response finish_reason=%s for model=%s, max_tokens=%s", finish_reason, model, max_tokens)
            
            if not msg.tool_calls:
                logger.error(f"Model returned no tool calls. Message content: {msg.content}")
                raise ValueError(f"Model returned no tool calls. Content: {msg.content}")
            arguments = msg.tool_calls[0].function.arguments
            data = json.loads(arguments)
            
            # Capture usage
            prompt_tokens = response.usage.prompt_tokens if response.usage else 0
            completion_tokens = response.usage.completion_tokens if response.usage else 0
            
            # Attempt to resolve exact cost from headers or OpenRouter specific variables
            cost = 0.0
            gen_id = getattr(response, "id", None)
            if gen_id:
                cost = _fetch_generation_cost(gen_id)
            
            # Fallback simple estimation if OpenRouter lookup returns 0.0
            if cost == 0.0:
                cost = (prompt_tokens * 0.000003) + (completion_tokens * 0.000015)

            usage_meta = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost
            }
            total_extraction_usage["prompt_tokens"] += prompt_tokens
            total_extraction_usage["completion_tokens"] += completion_tokens
            total_extraction_usage["cost_usd"] += cost
            return [data, usage_meta]
        except Exception as e:
            last_exc = e
    
    raise last_exc or RuntimeError("Extraction failed")

def extract_content(crop: Image.Image, region: Region, model: str, stage_name: str = "extract", force: bool = False) -> ExtractedContent:
    # Convert cropped image to bytes for cache keying
    img_byte_arr = io.BytesIO()
    crop.save(img_byte_arr, format="PNG")
    img_bytes = img_byte_arr.getvalue()

    # caching layer
    raw_res = cached_call(
        image_bytes=img_bytes,
        stage=stage_name,
        model=model,
        fn=lambda: _call_extraction_api(img_bytes, region, model),
        force=force
    )

    data, usage = raw_res
    
    # Map fields to Pydantic ExtractedContent object
    markdown_text = None
    table_json = None
    items_json = None
    drawing_json = None

    if region.region_type in (RegionType.RULED_TABLE, RegionType.SECTION_GROUPED_TABLE):
        table_json = data
    elif region.region_type == RegionType.SWATCH_GRID:
        items_json = data.get("colors")
    elif region.region_type == RegionType.STAT_CARDS:
        items_json = data.get("cards")
    elif region.region_type == RegionType.TECHNICAL_DRAWING:
        drawing_json = data
    elif region.region_type == RegionType.FOOTNOTE_BLOCK:
        items_json = data.get("footnotes")
    elif region.region_type == RegionType.BULLET_PANEL:
        # Render tier feature lists into a clean markdown block
        inherits = f" [Inherits from {data.get('inherits_from')}]" if data.get("inherits_from") else ""
        lines = [f"### Trim: {data.get('tier_name')}{inherits}"]
        for feat in data.get("features", []):
            lines.append(f"- {feat}")
        markdown_text = "\n".join(lines)
    elif region.region_type == RegionType.ICON_BADGE:
        markdown_text = f"**{data.get('badge_name')}**: {data.get('description', '')}"
    else:
        markdown_text = data.get("markdown")

    return ExtractedContent(
        region_id=region.id,
        region_type=region.region_type,
        markdown=markdown_text,
        table_json=table_json,
        items_json=items_json,
        drawing_json=drawing_json,
        model_used=model,
        usage=usage
    )
