import io
import json
import os
import base64
import httpx
import logging
from PIL import Image
from openai import OpenAI
from table_extractor.schemas import Region, RegionType
from table_extractor.cache import cached_call
from table_extractor.ingest import crop_with_padding

logger = logging.getLogger(__name__)

total_detection_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

def get_total_usage() -> dict:
    return total_detection_usage

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

# OpenRouter client configuration
client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
    api_key=os.environ.get("OPENROUTER_API_KEY", "mock_key"),
)

DETECTION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "region_type": {
                        "type": "string",
                        "enum": [t.value for t in RegionType]
                    },
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4
                    },
                    "may_contain_subregions": {"type": "boolean"}
                },
                "required": ["label", "region_type", "bbox", "may_contain_subregions"]
            }
        }
    },
    "required": ["regions"]
}

def _load_prompt() -> str:
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "detect_regions.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()

def _load_refine_prompt() -> str:
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "refine_region.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()

def _api_call(image_bytes: bytes, model: str, system_prompt: str = None) -> dict:
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    if system_prompt is None:
        system_prompt = _load_prompt()

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
                    "name": "return_regions",
                    "description": "Returns bounding boxes and metadata of brochure regions.",
                    "parameters": DETECTION_TOOL_SCHEMA
                }
            }
        ],
        tool_choice={"type": "function", "function": {"name": "return_regions"}},
        max_tokens=4096
    )
    
    # Capture usage
    prompt_tokens = response.usage.prompt_tokens if response.usage else 0
    completion_tokens = response.usage.completion_tokens if response.usage else 0
    
    # Cost calculation matching extract.py logic
    cost = 0.0
    gen_id = getattr(response, "id", None)
    if gen_id:
        cost = _fetch_generation_cost(gen_id)
    if cost == 0.0:
        cost = (prompt_tokens * 0.000003) + (completion_tokens * 0.000015)

    total_detection_usage["prompt_tokens"] += prompt_tokens
    total_detection_usage["completion_tokens"] += completion_tokens
    total_detection_usage["cost_usd"] += cost

    arguments = response.choices[0].message.tool_calls[0].function.arguments
    return json.loads(arguments)

def normalize_bbox_list(bboxes: list[list[float]], w: int, h: int) -> list[list[float]]:
    if not bboxes:
        return bboxes

    max_x = max(max(box[0], box[2]) for box in bboxes)
    max_y = max(max(box[1], box[3]) for box in bboxes)

    # Determine if already normalized
    is_normalized = False
    if max_x <= 1005 and max_y <= 1005:
        if w >= 1000 and h >= 1000:
            is_normalized = True
        elif max_x > w * 1.05 or max_y > h * 1.05:
            is_normalized = True

    if is_normalized:
        normalized = []
        for box in bboxes:
            normalized.append([
                max(0.0, min(1000.0, box[0])),
                max(0.0, min(1000.0, box[1])),
                max(0.0, min(1000.0, box[2])),
                max(0.0, min(1000.0, box[3]))
            ])
        return normalized

    # Calculate coordinate space dimensions (w_space, h_space)
    shorter_side = min(w, h)
    if shorter_side > 1560:
        scale = 1560.0 / shorter_side
        w_space = w * scale
        h_space = h * scale
    else:
        w_space = float(w)
        h_space = float(h)

    # Check if the coordinates are closer to the original pixel space
    limit_x = w_space * 1.2
    limit_y = h_space * 1.2
    if max_x > limit_x or max_y > limit_y:
        space_w = max(float(w), max_x)
        space_h = max(float(h), max_y)
    else:
        space_w = max(w_space, max_x)
        space_h = max(h_space, max_y)

    normalized = []
    for box in bboxes:
        normalized.append([
            max(0.0, min(1000.0, (box[0] / space_w) * 1000.0)),
            max(0.0, min(1000.0, (box[1] / space_h) * 1000.0)),
            max(0.0, min(1000.0, (box[2] / space_w) * 1000.0)),
            max(0.0, min(1000.0, (box[3] / space_h) * 1000.0))
        ])
    return normalized

def _safe_get_regions(raw_json: dict) -> list:
    raw_regions = raw_json.get("regions", [])
    if isinstance(raw_regions, str):
        try:
            cleaned = raw_regions.strip()
            if cleaned.endswith("</invoke>"):
                cleaned = cleaned[:-9].strip()
            raw_regions = json.loads(cleaned)
        except Exception as e:
            logger.error(f"Failed to parse raw_regions string: {e}")
            raw_regions = []
    return raw_regions


def detect_regions(img: Image.Image, model: str, force: bool = False) -> list[Region]:
    # Convert image to bytes to use in caching key
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    img_bytes = img_byte_arr.getvalue()

    # API Call wrapped in caching
    raw_json = cached_call(
        image_bytes=img_bytes,
        stage="detect",
        model=model,
        fn=lambda: _api_call(img_bytes, model),
        force=force
    )

    w, h = img.size
    raw_regions = _safe_get_regions(raw_json)
    
    bboxes = [r["bbox"] for r in raw_regions]
    normalized_bboxes = normalize_bbox_list(bboxes, w, h)

    regions = []
    for idx, r_data in enumerate(raw_regions):
        region = Region(
            id=f"r{idx}",
            parent_id=None,
            label=r_data["label"],
            region_type=RegionType(r_data["region_type"]),
            bbox=normalized_bboxes[idx],
            may_contain_subregions=r_data.get("may_contain_subregions", False),
            depth=0
        )
        regions.append(region)
    check_overlaps(regions)
    return regions

def detect_subregions(img: Image.Image, parent: Region, model: str, depth: int = 1, max_depth: int = 2, force: bool = False) -> list[Region]:
    if depth > max_depth:
        return []

    # Use 8% padding for refinement zooming to capture full table/drawing boundaries
    pad_pct = 0.08
    cropped_img = crop_with_padding(img, parent.bbox, pad_pct=pad_pct)
    img_byte_arr = io.BytesIO()
    cropped_img.save(img_byte_arr, format="PNG")
    img_bytes = img_byte_arr.getvalue()

    raw_json = cached_call(
        image_bytes=img_bytes,
        stage=f"detect_subregion_L{depth}",
        model=model,
        fn=lambda: _api_call(img_bytes, model),
        force=force
    )

    px0, py0, px1, py1 = parent.bbox
    pw = px1 - px0
    ph = py1 - py0

    # Calculate padded box boundaries in 0-1000 scale
    pad_w = pw * pad_pct
    pad_h = ph * pad_pct
    gx0_crop = max(0.0, px0 - pad_w)
    gy0_crop = max(0.0, py0 - pad_h)
    gx1_crop = min(1000.0, px1 + pad_w)
    gy1_crop = min(1000.0, py1 + pad_h)

    pw_padded = gx1_crop - gx0_crop
    ph_padded = gy1_crop - gy0_crop

    cw, ch = cropped_img.size
    raw_regions = _safe_get_regions(raw_json)
    
    bboxes = [r["bbox"] for r in raw_regions]
    normalized_bboxes = normalize_bbox_list(bboxes, cw, ch)

    subregions = []
    for idx, r_data in enumerate(raw_regions):
        sx0, sy0, sx1, sy1 = normalized_bboxes[idx]
        # Map relative to padded crop boundaries
        gx0 = gx0_crop + (sx0 / 1000.0 * pw_padded)
        gy0 = gy0_crop + (sy0 / 1000.0 * ph_padded)
        gx1 = gx0_crop + (sx1 / 1000.0 * pw_padded)
        gy1 = gy0_crop + (sy1 / 1000.0 * ph_padded)

        # Check if the subregion is duplicate of the parent
        sub_area = (gx1 - gx0) * (gy1 - gy0)
        parent_area = pw * ph
        iou_with_parent = _calculate_iou([gx0, gy0, gx1, gy1], parent.bbox)
        if parent_area > 0 and (sub_area / parent_area > 0.70 or iou_with_parent > 0.70):
            logger.info(f"Skipping duplicate subregion {r_data['label']} and updating parent bbox (IoU={iou_with_parent:.2f})")
            # Prevent severe truncation/shrinkage (e.g. losing bottom boundary context)
            rw = gx1 - gx0
            rh = gy1 - gy0
            new_x0 = gx0 if (rw / pw >= 0.90) else parent.bbox[0]
            new_x1 = gx1 if (rw / pw >= 0.90) else parent.bbox[2]
            new_y0 = gy0 if (rh / ph >= 0.90) else parent.bbox[1]
            new_y1 = gy1 if (rh / ph >= 0.90) else parent.bbox[3]
            parent.bbox = [new_x0, new_y0, new_x1, new_y1]
            
            # Since parent matches the subregion itself, it is a leaf node
            parent.may_contain_subregions = False
            return []

        # Check containment inside parent's unpadded boundaries to filter out adjacent regions in padding
        ix0 = max(gx0, px0)
        iy0 = max(gy0, py0)
        ix1 = min(gx1, px1)
        iy1 = min(gy1, py1)
        inter_area = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        if sub_area > 0 and (inter_area / sub_area < 0.50):
            logger.info(f"Skipping out-of-bounds adjacent subregion {r_data['label']} (Containment={inter_area/sub_area:.2f})")
            continue

        subregion = Region(
            id=f"{parent.id}_s{idx}",
            parent_id=parent.id,
            label=r_data["label"],
            region_type=RegionType(r_data["region_type"]),
            bbox=[gx0, gy0, gx1, gy1],
            may_contain_subregions=r_data.get("may_contain_subregions", False),
            depth=depth
        )
        if subregion.may_contain_subregions:
            # Recurse subregion extraction on the original image since subregion.bbox is now in global coordinates
            subregion.children = detect_subregions(img, subregion, model, depth + 1, max_depth, force)
            if not subregion.children:
                subregion.may_contain_subregions = False
        subregions.append(subregion)
    if subregions:
        parent.bbox = [
            min(s.bbox[0] for s in subregions),
            min(s.bbox[1] for s in subregions),
            max(s.bbox[2] for s in subregions),
            max(s.bbox[3] for s in subregions)
        ]
    check_overlaps(subregions)
    return subregions

def _calculate_iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interArea = max(0, xB - xA) * max(0, yB - yA)
    boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    unionArea = float(boxAArea + boxBArea - interArea)
    if unionArea == 0:
        return 0.0
    return interArea / unionArea

def check_overlaps(regions: list[Region]):
    # Group by parent_id to compare siblings
    by_parent = {}
    for r in regions:
        by_parent.setdefault(r.parent_id, []).append(r)
    
    for parent, siblings in by_parent.items():
        n = len(siblings)
        for i in range(n):
            for j in range(i + 1, n):
                rA, rB = siblings[i], siblings[j]
                iou = _calculate_iou(rA.bbox, rB.bbox)
                if iou > 0.1:
                    logger.warning(f"Overlap detected: {rA.id} ({rA.label}) / {rB.id} ({rB.label}), IoU={iou:.2f}")
                    rA.overlap_warning = True
                    rB.overlap_warning = True

def refine_region_zoom(img: Image.Image, region: Region, model: str, force: bool = False) -> Region:
    # Use 8% padding to capture full table/drawing boundaries, consistent with subregions
    pad_pct = 0.08
    cropped_img = crop_with_padding(img, region.bbox, pad_pct=pad_pct)
    img_byte_arr = io.BytesIO()
    cropped_img.save(img_byte_arr, format="PNG")
    img_bytes = img_byte_arr.getvalue()

    system_prompt = _load_refine_prompt()
    raw_json = cached_call(
        image_bytes=img_bytes,
        stage="refine_region_zoom",
        model=model,
        fn=lambda: _api_call(img_bytes, model, system_prompt=system_prompt),
        force=force
    )

    px0, py0, px1, py1 = region.bbox
    pw = px1 - px0
    ph = py1 - py0

    # Calculate padded box boundaries in 0-1000 scale
    pad_w = pw * pad_pct
    pad_h = ph * pad_pct
    gx0_crop = max(0.0, px0 - pad_w)
    gy0_crop = max(0.0, py0 - pad_h)
    gx1_crop = min(1000.0, px1 + pad_w)
    gy1_crop = min(1000.0, py1 + pad_h)

    pw_padded = gx1_crop - gx0_crop
    ph_padded = gy1_crop - gy0_crop

    cw, ch = cropped_img.size
    raw_regions = _safe_get_regions(raw_json)
    if not raw_regions or "bbox" not in raw_regions[0]:
        return region

    # Use the first region returned as our refined coordinate source
    r_data = raw_regions[0]
    bboxes = [r_data["bbox"]]
    normalized_bboxes = normalize_bbox_list(bboxes, cw, ch)
    sx0, sy0, sx1, sy1 = normalized_bboxes[0]

    # Map relative to padded crop boundaries back to global coordinate space
    gx0 = gx0_crop + (sx0 / 1000.0 * pw_padded)
    gy0 = gy0_crop + (sy0 / 1000.0 * ph_padded)
    gx1 = gx0_crop + (sx1 / 1000.0 * pw_padded)
    gy1 = gy0_crop + (sy1 / 1000.0 * ph_padded)

    try:
        region_type = RegionType(r_data.get("region_type"))
    except ValueError:
        region_type = region.region_type

    # Return a new refined Region object or update its bbox
    refined = Region(
        id=region.id,
        parent_id=region.parent_id,
        label=r_data.get("label", region.label),
        region_type=region_type,
        bbox=[gx0, gy0, gx1, gy1],
        may_contain_subregions=region.may_contain_subregions,
        depth=region.depth
    )
    return refined


