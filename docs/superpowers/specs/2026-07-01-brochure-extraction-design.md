# Design Spec: Brochure Table/Region Extraction Pipeline (v0 Prototype)

Date: 2026-07-01
Author: Antigravity

This specification outlines the architecture, data flow, caching mechanisms, and build order for a command-line utility to extract structured data from dense vehicle brochure spec sheets.

---

## 1. Project Architecture & Directory Layout

The codebase will reside in the `table_extractor/` directory:

```
table_extractor/
  __init__.py
  schemas.py            # Region, ExtractedContent (with model_used, usage, cost_usd), Pydantic validation
  ingest.py             # Image loading, splitting double-page spreads
  detect.py             # Stage 2 (Coarse Pass) and Stage 3 (Subregion Recursion)
  extract.py            # Stage 4 (Regional prompts dispatch to OpenRouter)
  reconcile.py          # Stage 5 (Footnote resolution, consistency check, markdown generation)
  render.py             # Stage 6 (Draw colored overlay bounding boxes)
  cache.py              # Hash-based stage caching logic
  main.py               # CLI entrypoint with argparse & --force-from flags
  prompts/              # Directory of raw text prompts
    detect_regions.txt
    extract_ruled_table.txt
    extract_section_grouped_table.txt
    extract_bullet_panel.txt
    extract_swatch_grid.txt
    extract_stat_cards.txt
    extract_technical_drawing.txt
    extract_footnote_block.txt
  requirements.txt
```

### Dependencies (`requirements.txt`)
* `openai>=1.0.0` (for OpenRouter compatibility)
* `pillow>=10.0` (for image manipulation and overlay rendering)
* `pydantic>=2.0` (for data structures and validation)

---

## 1.5 schemas.py Schema Definition

To track performance, accuracy, and costs, every extracted region's content includes model usage data.

```python
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel

class RegionType(str, Enum):
    RULED_TABLE = "ruled_table"
    SECTION_GROUPED_TABLE = "section_grouped_table"
    BULLET_PANEL = "bullet_panel"
    SWATCH_GRID = "swatch_grid"
    STAT_CARDS = "stat_cards"
    TECHNICAL_DRAWING = "technical_drawing"
    ICON_BADGE = "icon_badge"
    FOOTNOTE_BLOCK = "footnote_block"
    OTHER = "other"

class ExtractedContent(BaseModel):
    region_id: str
    region_type: RegionType
    markdown: Optional[str] = None
    table_json: Optional[dict] = None
    items_json: Optional[list] = None
    drawing_json: Optional[dict] = None
    footnote_markers: list[str] = []
    confidence_flag: bool = False
    model_used: str                          # e.g., "anthropic/claude-3-5-sonnet"
    usage: dict                              # {"prompt_tokens": int, "completion_tokens": int, "cost_usd": float}

class Region(BaseModel):
    id: str                      # e.g. "r3" — assigned during detection
    parent_id: Optional[str]     # None for top-level regions
    label: str                   # short human label, e.g. "HTX [In addition to HTK (O)]"
    region_type: RegionType
    bbox: list[float]            # [x0, y0, x1, y1] normalized 0-1000
    may_contain_subregions: bool # detection-time hint, drives whether stage 3 recurses into it
    depth: int = 0               # tree depth level (L0=0, L1=1, L2=2)
    children: list["Region"] = []
    extracted: Optional[ExtractedContent] = None

Region.model_rebuild()
```

---

## 2. Ingestion & Double-Page Splitting

In `table_extractor/ingest.py`, we implement `load_and_prep` without automatic deskewing, targeting the digital exports directly.
For landscape-oriented double-page spreads, we use a simple midpoint cropping heuristic:

```python
from PIL import Image

def load_and_prep(image_path: str) -> list[Image.Image]:
    img = Image.open(image_path)
    w, h = img.size
    # Crude double-page spread heuristic (w/h > 1.8)
    if w / h > 1.8:
        mid = w // 2
        return [img.crop((0, 0, mid, h)), img.crop((mid, 0, w, h))]
    return [img]
```

To assist caching, `ingest.py` will also expose helper functions to get base64 encoded strings and raw bytes of images (or cropped sub-regions).

---

## 3. OpenRouter API & Stage-Based Caching

We will use the standard `openai` library targeting OpenRouter:

```python
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
)
```

The model constants will be defined globally in `main.py`:
* `DETECTION_MODEL`: Defaulting to `"google/gemini-2.5-pro"` or `"anthropic/claude-3-5-sonnet"`
* `EXTRACTION_MODEL`: Defaulting to `"google/gemini-2.5-pro"` or `"anthropic/claude-3-5-sonnet"`
* `VERIFICATION_MODEL`: Defaulting to a different model (e.g., if extraction is Claude, verification is Gemini, or vice versa) to ensure the self-consistency check is a true cross-model evaluation.

#### OpenRouter Usage & Cost Tracking
We will capture usage stats (`prompt_tokens`, `completion_tokens`, `cost_usd`) for every call.
> [!NOTE]
> Since OpenRouter's standard Chat Completion response might not always carry cost directly in the metadata, if `cost_usd` is missing or `None`, we query the OpenRouter `/generation` API endpoint using HTTP GET with the generation ID returned in the headers (or estimate it based on standard token pricing parameters).

### Caching Strategy (`table_extractor/cache.py`)
To enable iterative development of downstream stages without paying for repeated API calls, we cache API responses locally under a `.stage_cache/` directory.

The cache key is computed as follows:
* `detect` stage: `sha256(full_page_image_bytes)[:16]`
* `extract` stage: `sha256(cropped_region_image_bytes)[:16]`
* `extract_verify` stage (self-consistency check): `sha256(cropped_region_image_bytes)[:16]`

```python
# cache key = sha256(bytes actually sent to the model for THIS call)[:16] + stage + model
# detect: full-page bytes -> one key per page
# extract: cropped region bytes -> one key per region
# extract_verify: cropped region bytes -> separate key for the self-consistency call (keyed by VERIFICATION_MODEL)
```

We compute the key as:
```python
def _cache_key(image_bytes: bytes, stage: str, model: str) -> str:
    h = hashlib.sha256(image_bytes).hexdigest()[:16]
    sanitized_model = model.replace("/", "_")
    return f"{h}_{stage}_{sanitized_model}"
```

#### Selective Cache Invalidation
The CLI `main.py` supports a `--force-from` flag (options: `detect`, `extract`, `reconcile`).
* If `--force-from extract` is set, all cached responses for `extract`, `extract_verify`, and downstream reconciliation steps are skipped and updated, but prior cached steps like `detect` are kept.

---

## 4. Detection and Extraction Prompts

We use native tool calling (function calling) for structured output.

### Stage 2 & 3: Region Detection
* **Prompt**: `prompts/detect_regions.txt` (returns visual region bounding boxes normalized 0-1000).
* **Recursion**: If `may_contain_subregions=True` and recursion depth < 2, the pipeline crops the region and calls `detect_subregions` (Stage 3).

### Stage 4: Regional Content Extraction
Depending on `RegionType`, the crop is sent to OpenRouter along with the corresponding prompt file from `prompts/`:
* `RULED_TABLE` / `SECTION_GROUPED_TABLE` -> `extract_ruled_table.txt` / `extract_section_grouped_table.txt`
* `BULLET_PANEL` -> `extract_bullet_panel.txt`
* `SWATCH_GRID` -> `extract_swatch_grid.txt`
* `STAT_CARDS` -> `extract_stat_cards.txt`
* `TECHNICAL_DRAWING` -> `extract_technical_drawing.txt`
* `FOOTNOTE_BLOCK` -> `extract_footnote_block.txt`

---

## 5. Reconciliation & Output Renderers

### Footnote Resolution (`reconcile.py`)
Matches footnote markers (e.g. `*`, `**`, `^^`) found in tables/bullet panels with the extracted dictionary from the `footnote_block` region. Unresolved markers are flagged in `footnote_resolutions` dictionary.

### Self-Consistency Check (`reconcile.py`)
Randomly samples ~20% of the regions. Triggers a second API call under the `extract_verify` cache stage using `VERIFICATION_MODEL`. We perform a field-by-field comparison between the primary `EXTRACTION_MODEL` output and the `VERIFICATION_MODEL` output. If any mismatches are found, the `confidence_flag` is set to `True` on the extraction content.

### Markdown Generation (`reconcile.py`)
Depth-first walk of regions tree.
* Rule-based conversion of `ExtractedContent` schemas (columns/rows for tables, bullets/inheritance for panels, etc.) into readable markdown.
* Prepend markdown headers showing the hierarchical region path.

### Overlay Drawing (`render.py`)
Renders bounding boxes color-coded by tree depth on the original image:
* Depth 0 (L0): Coral
* Depth 1 (L1): Blue
* Depth 2 (L2): Teal
Labels the top-left corner of each box with the `region_type` and `id` (e.g. `r1 (ruled_table)`).

---

## 6. CLI Execution Sequence (`main.py`)

A user runs the pipeline via:
```bash
python main.py \
  --image <path_to_image> \
  --out <output_dir> \
  --detection-model <openrouter_model_name> \
  --extraction-model <openrouter_model_name> \
  --verification-model <openrouter_model_name> \
  [--force-from <stage>]
```

1. **Ingest**: Split and load images.
2. **Detect**: Coarse detect regions on each page image (and recursively detect subregions if `may_contain_subregions=True`).
3. **Crop & Extract**: Crop regions, dispatch to OpenRouter, parse with Pydantic schemas.
4. **Reconcile**: Perform footnote resolution and self-consistency check.
5. **Output**: Write `overlay.png` (or suffix page indexes if split), `extraction.md`, and `regions.json` containing the full structured metadata tree.

---

## 7. Build Order (Step-by-Step)

We will execute the development in this sequence:
1. **Scaffold & Ingestion + Coarse Detection + Overlay Rendering**: Run on Kia Seltos and WagonR. Check bounding boxes in `overlay.png` for alignment and correct scaling.
2. **Recursion**: Enable `detect_subregions` for nested regions (e.g., Kia Seltos trim-tier groups).
3. **Ruled Table Extraction**: Implement and validate extraction on WagonR specs tables.
4. **Remaining Extractions**: Implement prompts for bullet panels, swatches, stats, drawings, and footnotes.
5. **Reconciliation**: Footnote resolver and 20% self-consistency sampling.
6. **Markdown Output**: Implement the depth-first tree markdown renderer.
