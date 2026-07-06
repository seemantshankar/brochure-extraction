# Brochure Table/Region Extraction Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a command-line tool that ingests vehicle brochure page images, detects visual regions (tables, feature bullet lists, drawings, etc.), extracts their contents using OpenRouter, reconciles footnotes/verifies self-consistency, and outputs markdown results alongside a color-coded region overlay QA image.

**Architecture:** A modular Python pipeline consisting of an ingestion module, a recursive region detection module, a structured regional content extraction module, a reconciliation module (footnote resolution and cross-model self-consistency verify), an overlay renderer, and a stage-based caching module keying by page/crop hash, stage name, and model string to support fast local developer iteration.

**Tech Stack:** Python 3, Pydantic v2, Pillow, OpenRouter API (using standard `openai` library).

## Global Constraints

* Target platform: MacOS (zsh shell)
* OpenRouter API key configured via `OPENROUTER_API_KEY` env variable
* OpenAI SDK version floor: `openai>=1.0.0`
* Pillow version floor: `pillow>=10.0`
* Pydantic version floor: `pydantic>=2.0`
* Avoid writing project code to tmp, .gemini, or outside the workspace.
* Keep intermediate LLM caches under local directory `table_extractor/.stage_cache/` (gitignore-d).

---

## File Structure Map

```
table_extractor/
  __init__.py
  schemas.py            # Pydantic models (RegionType, ExtractedContent, Region)
  ingest.py             # load_and_prep() splitting & image conversion
  cache.py              # Hash-based stage cache using (image_bytes, stage, model)
  detect.py             # detect_regions() & detect_subregions() calling OpenAI tools
  extract.py            # extract_content() dispatching regional prompts & tool calls
  reconcile.py          # resolve_footnotes(), self_consistency_check(), and to_markdown()
  render.py             # draw_overlay() rendering Coral/Blue/Teal boxes
  main.py               # argparser, coordination, constants
  prompts/              # txt prompts matching spec
```

---

## Tasks

### Task 1: Scaffolding and Schemas

**Files:**
* Create: `table_extractor/requirements.txt`
* Create: `table_extractor/__init__.py`
* Create: `table_extractor/schemas.py`
* Create: `table_extractor/tests/test_schemas.py`

**Interfaces:**
* Consumes: None
* Produces: `RegionType`, `ExtractedContent`, `Region` Pydantic models

- [ ] **Step 1: Write requirements.txt**
  Create `table_extractor/requirements.txt`:
  ```text
  openai>=1.0.0
  pillow>=10.0
  pydantic>=2.0
  pytest>=7.0
  pytest-mock>=3.6
  ```

- [ ] **Step 2: Create empty package marker**
  Create `table_extractor/__init__.py`.

- [ ] **Step 3: Write schemas.py**
  Create `table_extractor/schemas.py`:
  ```python
  from enum import Enum
  from typing import Optional
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
      model_used: str
      usage: dict  # {"prompt_tokens": int, "completion_tokens": int, "cost_usd": float}

  class Region(BaseModel):
      id: str
      parent_id: Optional[str] = None
      label: str
      region_type: RegionType
      bbox: list[float]  # [x0, y0, x1, y1] normalized to 0-1000
      may_contain_subregions: bool
      depth: int = 0
      children: list["Region"] = []
      extracted: Optional[ExtractedContent] = None

  Region.model_rebuild()
  ```

- [ ] **Step 4: Write schema test**
  Create `table_extractor/tests/test_schemas.py`:
  ```python
  from table_extractor.schemas import Region, RegionType, ExtractedContent

  def test_region_nesting():
      content = ExtractedContent(
          region_id="r1",
          region_type=RegionType.BULLET_PANEL,
          markdown="* Bullet 1",
          model_used="test-model",
          usage={"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001}
      )
      child = Region(
          id="r1-1",
          parent_id="r1",
          label="Child Panel",
          region_type=RegionType.BULLET_PANEL,
          bbox=[100, 100, 200, 200],
          may_contain_subregions=False,
          depth=1
      )
      parent = Region(
          id="r1",
          parent_id=None,
          label="Parent Panel",
          region_type=RegionType.BULLET_PANEL,
          bbox=[50, 50, 300, 300],
          may_contain_subregions=True,
          depth=0,
          children=[child],
          extracted=content
      )
      assert parent.children[0].id == "r1-1"
      assert parent.extracted.region_id == "r1"
  ```

- [ ] **Step 5: Run tests**
  Run: `pytest table_extractor/tests/test_schemas.py -v`
  Expected: PASS

- [ ] **Step 6: Commit**
  Run:
  ```bash
  git add table_extractor/requirements.txt table_extractor/__init__.py table_extractor/schemas.py table_extractor/tests/test_schemas.py
  git commit -m "feat: setup project structure, requirements, and schemas"
  ```

---

### Task 2: Ingestion & Splitting

**Files:**
* Create: `table_extractor/ingest.py`
* Create: `table_extractor/tests/test_ingest.py`

**Interfaces:**
* Consumes: Image files from local filesystem
* Produces: `load_and_prep(image_path: str) -> list[PIL.Image.Image]` and helper `crop_with_padding(img: Image.Image, bbox: list[float], pad_pct: float) -> Image.Image`

- [ ] **Step 1: Write test_ingest.py**
  Create `table_extractor/tests/test_ingest.py`:
  ```python
  from PIL import Image
  from table_extractor.ingest import load_and_prep, crop_with_padding
  import os

  def test_load_and_prep_single(tmp_path):
      img_path = os.path.join(tmp_path, "single.png")
      img = Image.new("RGB", (100, 100), color="red")
      img.save(img_path)
      pages = load_and_prep(img_path)
      assert len(pages) == 1
      assert pages[0].size == (100, 100)

  def test_load_and_prep_double_spread(tmp_path):
      img_path = os.path.join(tmp_path, "spread.png")
      img = Image.new("RGB", (200, 100), color="blue")  # w/h = 2.0 (> 1.8)
      img.save(img_path)
      pages = load_and_prep(img_path)
      assert len(pages) == 2
      assert pages[0].size == (100, 100)
      assert pages[1].size == (100, 100)

  def test_crop_with_padding():
      img = Image.new("RGB", (1000, 1000), color="white")
      cropped = crop_with_padding(img, [100.0, 100.0, 200.0, 200.0], pad_pct=0.05)
      # Box is [100, 100, 200, 200], width/height = 100
      # Padding = 100 * 0.05 = 5. Target crop = [95, 95, 205, 205]
      assert cropped.size == (110, 110)
  ```

- [ ] **Step 2: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_ingest.py -v`
  Expected: FAIL (ModuleNotFoundError or ImportErrors)

- [ ] **Step 3: Write ingest.py**
  Create `table_extractor/ingest.py`:
  ```python
  from PIL import Image

  def load_and_prep(image_path: str) -> list[Image.Image]:
      img = Image.open(image_path)
      w, h = img.size
      if w / h > 1.8:
          mid = w // 2
          return [img.crop((0, 0, mid, h)), img.crop((mid, 0, w, h))]
      return [img]

  def crop_with_padding(img: Image.Image, bbox: list[float], pad_pct: float = 0.03) -> Image.Image:
      w, h = img.size
      # Convert 0-1000 normalized to absolute pixel coordinates
      x0 = (bbox[0] / 1000.0) * w
      y0 = (bbox[1] / 1000.0) * h
      x1 = (bbox[2] / 1000.0) * w
      y1 = (bbox[3] / 1000.0) * h

      bw = x1 - x0
      bh = y1 - y0
      pad_w = bw * pad_pct
      pad_h = bh * pad_pct

      # Apply padding and clamp to image boundaries
      x0_pad = max(0, int(x0 - pad_w))
      y0_pad = max(0, int(y0 - pad_h))
      x1_pad = min(w, int(x1 + pad_w))
      y1_pad = min(h, int(y1 + pad_h))

      return img.crop((x0_pad, y0_pad, x1_pad, y1_pad))
  ```

- [ ] **Step 4: Run tests**
  Run: `pytest table_extractor/tests/test_ingest.py -v`
  Expected: PASS

- [ ] **Step 5: Commit**
  Run:
  ```bash
  git add table_extractor/ingest.py table_extractor/tests/test_ingest.py
  git commit -m "feat: implement image loading, split spread heuristic, and padding crop"
  ```

---

### Task 3: Caching Module

**Files:**
* Create: `table_extractor/cache.py`
* Create: `table_extractor/tests/test_cache.py`

**Interfaces:**
* Consumes: Image raw bytes, stage name, model name, call execution function
* Produces: `cached_call(image_bytes: bytes, stage: str, model: str, fn, force: bool = False) -> dict`

- [ ] **Step 1: Write test_cache.py**
  Create `table_extractor/tests/test_cache.py`:
  ```python
  from table_extractor.cache import cached_call, _cache_key
  import os
  import shutil

  def test_caching_and_force(tmp_path):
      test_dir = os.path.join(tmp_path, ".stage_cache")
      # Override the cache directory path in the module for test isolation
      import table_extractor.cache as cache
      orig_dir = cache.CACHE_DIR
      cache.CACHE_DIR = test_dir

      try:
          called = 0
          def execute_api():
              nonlocal called
              called += 1
              return {"regions": [{"id": "r1"}]}

          img_bytes = b"dummy_image_data"
          stage = "detect"
          model = "claude-3-5"

          # 1. First run (miss)
          res1 = cached_call(img_bytes, stage, model, execute_api)
          assert called == 1
          assert res1 == {"regions": [{"id": "r1"}]}

          # 2. Second run (hit)
          res2 = cached_call(img_bytes, stage, model, execute_api)
          assert called == 1
          assert res2 == {"regions": [{"id": "r1"}]}

          # 3. Forced run (miss/update)
          res3 = cached_call(img_bytes, stage, model, execute_api, force=True)
          assert called == 2
          assert res3 == {"regions": [{"id": "r1"}]}

      finally:
          cache.CACHE_DIR = orig_dir
          if os.path.exists(test_dir):
              shutil.rmtree(test_dir)
  ```

- [ ] **Step 2: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_cache.py -v`
  Expected: FAIL

- [ ] **Step 3: Write cache.py**
  Create `table_extractor/cache.py`:
  ```python
  import hashlib
  import json
  import os
  from pathlib import Path

  CACHE_DIR = ".stage_cache"

  def _cache_key(image_bytes: bytes, stage: str, model: str) -> str:
      h = hashlib.sha256(image_bytes).hexdigest()[:16]
      sanitized_model = model.replace("/", "_")
      return f"{h}_{stage}_{sanitized_model}"

  def cached_call(image_bytes: bytes, stage: str, model: str, fn, force: bool = False) -> dict:
      os.makedirs(CACHE_DIR, exist_ok=True)
      key = _cache_key(image_bytes, stage, model)
      cache_file = Path(CACHE_DIR) / f"{key}.json"

      if cache_file.exists() and not force:
          with open(cache_file, "r") as f:
              return json.load(f)

      result = fn()
      with open(cache_file, "w") as f:
          json.dump(result, f, indent=2)

      return result
  ```

- [ ] **Step 4: Run tests**
  Run: `pytest table_extractor/tests/test_cache.py -v`
  Expected: PASS

- [ ] **Step 5: Add gitignore rule & commit**
  Create/update `.gitignore` inside `table_extractor/` to ignore `.stage_cache/`:
  Create `table_extractor/.gitignore`:
  ```text
  .stage_cache/
  __pycache__/
  ```
  Run:
  ```bash
  git add table_extractor/cache.py table_extractor/tests/test_cache.py table_extractor/.gitignore
  git commit -m "feat: add Stage-based caching keyed by hash, stage, and model"
  ```

---

### Task 4: Region Detection & Tool Calling

**Files:**
* Create: `table_extractor/prompts/detect_regions.txt`
* Create: `table_extractor/detect.py`
* Create: `table_extractor/tests/test_detect.py`

**Interfaces:**
* Consumes: `client: OpenAI`, `PIL.Image.Image`, `model: str`, optional `parent_id`
* Produces: `detect_regions(img: PIL.Image.Image, model: str) -> list[Region]` and `detect_subregions(img: Image.Image, parent: Region, model: str, depth: int) -> list[Region]`

- [ ] **Step 1: Create prompts directory and detect_regions.txt**
  Create `table_extractor/prompts/detect_regions.txt`:
  ```text
  You are analyzing a page from a vehicle brochure. Identify every distinct visual region:
  tables, sub-tables, bullet/feature panels, color swatch grids, stat/callout cards, technical
  line drawings, icon badges, and footnote text blocks.

  For each region return:
  - label: short text describing the region, taken from its own header if it has one
  - region_type: one of [ruled_table, section_grouped_table, bullet_panel, swatch_grid,
    stat_cards, technical_drawing, icon_badge, footnote_block, other]
  - bbox: [x0, y0, x1, y1] normalized to a 0-1000 scale, tightest box that contains the region
    including its header/label but excluding surrounding whitespace
  - may_contain_subregions: true if this region is itself a container of multiple distinct
    sub-tables or sub-panels (e.g. a bordered zone containing several trim-tier panels side by
    side), false if it is already a single atomic region

  Do not merge visually distinct panels into one region even if they share a border. Do not
  split a single ruled table into multiple regions just because it has section-divider rows —
  that is one region of type section_grouped_table.

  Return via the provided tool call only. No prose.
  ```

- [ ] **Step 2: Write test_detect.py with OpenAI mocked**
  Create `table_extractor/tests/test_detect.py`:
  ```python
  from PIL import Image
  from table_extractor.schemas import RegionType
  from table_extractor.detect import detect_regions

  def test_detect_regions_calls_openrouter(mocker):
      # Mock the OpenAI client call
      mock_client = mocker.patch("table_extractor.detect.client")
      
      mock_choice = mocker.MagicMock()
      mock_tool_call = mocker.MagicMock()
      mock_tool_call.function.arguments = """
      {
        "regions": [
          {
            "label": "Specs Table",
            "region_type": "ruled_table",
            "bbox": [50, 50, 950, 950],
            "may_contain_subregions": false
          }
        ]
      }
      """
      mock_choice.message.tool_calls = [mock_tool_call]
      mock_client.chat.completions.create.return_value.choices = [mock_choice]

      img = Image.new("RGB", (100, 100))
      regions = detect_regions(img, "test-model")
      
      assert len(regions) == 1
      assert regions[0].label == "Specs Table"
      assert regions[0].region_type == RegionType.RULED_TABLE
      assert regions[0].depth == 0
      assert regions[0].id == "r0"
  ```

- [ ] **Step 3: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_detect.py -v`
  Expected: FAIL

- [ ] **Step 4: Implement detect.py**
  Create `table_extractor/detect.py`:
  ```python
  import io
  import json
  import os
  import base64
  from PIL import Image
  from openai import OpenAI
  from table_extractor.schemas import Region, RegionType
  from table_extractor.cache import cached_call
  from table_extractor.ingest import crop_with_padding

  # OpenRouter client configuration
  client = OpenAI(
      base_url="https://openrouter.ai/api/v1",
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
      with open(prompt_path, "r") as f:
          return f.read()

  def _api_call(image_bytes: bytes, model: str) -> dict:
      base64_image = base64.b64encode(image_bytes).decode("utf-8")
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
          tool_choice={"type": "function", "function": {"name": "return_regions"}}
      )
      
      arguments = response.choices[0].message.tool_calls[0].function.arguments
      return json.loads(arguments)

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

      regions = []
      for idx, r_data in enumerate(raw_json.get("regions", [])):
          region = Region(
              id=f"r{idx}",
              parent_id=None,
              label=r_data["label"],
              region_type=RegionType(r_data["region_type"]),
              bbox=r_data["bbox"],
              may_contain_subregions=r_data["may_contain_subregions"],
              depth=0
          )
          regions.append(region)
      return regions

  def detect_subregions(img: Image.Image, parent: Region, model: str, depth: int = 1, max_depth: int = 2, force: bool = False) -> list[Region]:
      if depth > max_depth:
          return []

      cropped_img = crop_with_padding(img, parent.bbox, pad_pct=0.0)
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

      subregions = []
      for idx, r_data in enumerate(raw_json.get("regions", [])):
          subregion = Region(
              id=f"{parent.id}_s{idx}",
              parent_id=parent.id,
              label=r_data["label"],
              region_type=RegionType(r_data["region_type"]),
              bbox=r_data["bbox"],
              may_contain_subregions=r_data["may_contain_subregions"],
              depth=depth
          )
          if subregion.may_contain_subregions:
              # Recurse subregion extraction
              subregion.children = detect_subregions(img, subregion, model, depth + 1, max_depth, force)
          subregions.append(subregion)
      return subregions
  ```

- [ ] **Step 5: Run tests**
  Run: `pytest table_extractor/tests/test_detect.py -v`
  Expected: PASS

- [ ] **Step 6: Commit**
  Run:
  ```bash
  git add table_extractor/prompts/detect_regions.txt table_extractor/detect.py table_extractor/tests/test_detect.py
  git commit -m "feat: implement stage 2 coarse and stage 3 recursive subregion detection with OpenAI tools"
  ```

---

### Task 5: Bounding Box Overlay Rendering

**Files:**
* Create: `table_extractor/render.py`
* Create: `table_extractor/tests/test_render.py`

**Interfaces:**
* Consumes: `PIL.Image.Image`, `regions: list[Region]`
* Produces: `draw_overlay(img: Image.Image, regions: list[Region]) -> Image.Image` (creates color-coded overlays by depth: L0=coral, L1=blue, L2=teal)

- [ ] **Step 1: Write test_render.py**
  Create `table_extractor/tests/test_render.py`:
  ```python
  from PIL import Image
  from table_extractor.schemas import Region, RegionType
  from table_extractor.render import draw_overlay

  def test_draw_overlay():
      img = Image.new("RGB", (1000, 1000), color="white")
      r0 = Region(
          id="r0", label="Test ruled table", region_type=RegionType.RULED_TABLE,
          bbox=[100, 100, 900, 900], may_contain_subregions=False, depth=0
      )
      res = draw_overlay(img, [r0])
      assert res.size == (1000, 1000)
      # Check that the image was drawn on (not pure white)
      pixels = list(res.getdata())
      assert any(p != (255, 255, 255) for p in pixels)
  ```

- [ ] **Step 2: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_render.py -v`
  Expected: FAIL

- [ ] **Step 3: Implement render.py**
  Create `table_extractor/render.py`:
  ```python
  from PIL import Image, ImageDraw, ImageFont
  from table_extractor.schemas import Region

  # Colors for different depth levels
  DEPTH_COLORS = {
      0: (240, 128, 128),  # Coral
      1: (30, 144, 255),   # Blue
      2: (0, 128, 128)     # Teal
  }

  def draw_overlay(img: Image.Image, regions: list[Region]) -> Image.Image:
      overlay = img.copy()
      draw = ImageDraw.Draw(overlay)
      w, h = overlay.size

      # Simple fallback font
      try:
          font = ImageFont.load_default()
      except Exception:
          font = None

      def draw_box(region: Region):
          # Scale coordinates from 0-1000 back to pixels
          x0 = (region.bbox[0] / 1000.0) * w
          y0 = (region.bbox[1] / 1000.0) * h
          x1 = (region.bbox[2] / 1000.0) * w
          y1 = (region.bbox[3] / 1000.0) * h

          color = DEPTH_COLORS.get(region.depth, (128, 128, 128))
          
          # Draw rectangle outline
          draw.rectangle([x0, y0, x1, y1], outline=color, width=4)
          
          # Draw label at top-left
          label_str = f"{region.id}: {region.label} ({region.region_type.value})"
          if font:
              # Draw label background
              draw.rectangle([x0, y0 - 15, x0 + len(label_str) * 6, y0], fill=color)
              draw.text((x0 + 2, y0 - 13), label_str, fill=(255, 255, 255), font=font)
          else:
              draw.text((x0 + 2, y0 - 13), label_str, fill=color)

          for child in region.children:
              draw_box(child)

      for region in regions:
          draw_box(region)

      return overlay
  ```

- [ ] **Step 4: Run tests**
  Run: `pytest table_extractor/tests/test_render.py -v`
  Expected: PASS

- [ ] **Step 5: Commit**
  Run:
  ```bash
  git add table_extractor/render.py table_extractor/tests/test_render.py
  git commit -m "feat: implement bounding box overlay rendering color-coded by tree depth"
  ```

---

### Task 6: Regional Content Extraction & Prompts

**Files:**
* Create prompts:
  * `table_extractor/prompts/extract_ruled_table.txt`
  * `table_extractor/prompts/extract_section_grouped_table.txt`
  * `table_extractor/prompts/extract_bullet_panel.txt`
  * `table_extractor/prompts/extract_swatch_grid.txt`
  * `table_extractor/prompts/extract_stat_cards.txt`
  * `table_extractor/prompts/extract_technical_drawing.txt`
  * `table_extractor/prompts/extract_footnote_block.txt`
* Create: `table_extractor/extract.py`
* Create: `table_extractor/tests/test_extract.py`

**Interfaces:**
* Consumes: `client: OpenAI`, `cropped_img: PIL.Image.Image`, `region: Region`, `model: str`
* Produces: `extract_content(crop: Image.Image, region: Region, model: str) -> ExtractedContent`

- [ ] **Step 1: Write all remaining prompt files**
  
  Create `table_extractor/prompts/extract_ruled_table.txt`:
  ```text
  This image is a cropped table from a vehicle brochure. Extract it as structured data, not
  markdown. Return:
  - columns: list of column headers, preserving merged/grouped headers as nested objects
    where applicable (e.g. a "1.0L" group spanning "LXi" and "VXi" sub-columns)
  - rows: list of rows; if the table has black section-divider rows (e.g. "SAFETY & SECURITY"),
    include a "section" field on each row giving the most recent divider label, do not treat
    the divider as its own row
  - Preserve checkmarks as "yes", dashes as "no", and any other literal cell values exactly as
    shown, including footnote markers (e.g. "**", "^^", "#") attached to values or headers —
    do not strip them
  Return via the provided tool call only.
  ```

  Create `table_extractor/prompts/extract_section_grouped_table.txt`:
  ```text
  This image is a cropped table from a vehicle brochure. Extract it as structured data, not
  markdown. Return:
  - columns: list of column headers, preserving merged/grouped headers as nested objects
    where applicable.
  - rows: list of rows; if the table has black section-divider rows (e.g. "SAFETY & SECURITY"),
    include a "section" field on each row giving the most recent divider label, do not treat
    the divider as its own row.
  - Preserve checkmarks as "yes", dashes as "no", and any other literal cell values exactly as
    shown, including footnote markers (e.g. "**", "^^", "#") attached to values or headers.
  Return via the provided tool call only.
  ```

  Create `table_extractor/prompts/extract_bullet_panel.txt`:
  ```text
  This image is a feature list panel for one trim tier. Extract:
  - tier_name: the panel's own header, e.g. "HTX [In addition to HTK (O)]"
  - inherits_from: the tier name referenced in "[In addition to X]", or null if none
  - features: list of feature strings, one per bullet, verbatim including any footnote markers
  Return via the provided tool call only.
  ```

  Create `table_extractor/prompts/extract_swatch_grid.txt`:
  ```text
  Extract each color swatch as: swatch_name (verbatim label), tone_type ("single" or "dual"),
  and for dual-tone entries, both component names (e.g. roof color, body color) separately.
  Return via the provided tool call only.
  ```

  Create `table_extractor/prompts/extract_stat_cards.txt`:
  ```text
  Extract each stat card as: card_label (e.g. "WagonR Petrol 1L"), variant (e.g. "MT" or
  "AGS"), value, and unit. Return via the provided tool call only.
  ```

  Create `table_extractor/prompts/extract_technical_drawing.txt`:
  ```text
  This image shows a technical line drawing with measurement callouts. Extract: view
  ("front"/"rear"/"side"/"top", inferred from the drawing), and a list of
  {label, value, unit} for every numeric measurement shown. Return via the provided tool
  call only.
  ```

  Create `table_extractor/prompts/extract_footnote_block.txt`:
  ```text
  Extract the footnote/disclaimer text block verbatim as plain text, preserving each footnote
  marker (*, **, ***, ****, ^, ^^, ^##, #, ##, etc.) at the start of the sentence it introduces,
  as a list of {marker, text} objects. Return via the provided tool call only.
  ```

- [ ] **Step 2: Write test_extract.py**
  Create `table_extractor/tests/test_extract.py`:
  ```python
  from PIL import Image
  from table_extractor.schemas import Region, RegionType
  from table_extractor.extract import extract_content

  def test_extract_ruled_table(mocker):
      mock_client = mocker.patch("table_extractor.extract.client")
      mock_choice = mocker.MagicMock()
      mock_tool_call = mocker.MagicMock()
      mock_tool_call.function.arguments = """
      {
        "columns": ["Features", "LXi"],
        "rows": [{"Features": "Power Steering", "LXi": "yes"}]
      }
      """
      # Mock OpenRouter header search and base returns
      mock_choice.message.tool_calls = [mock_tool_call]
      mock_resp = mocker.MagicMock()
      mock_resp.choices = [mock_choice]
      mock_resp.usage.prompt_tokens = 100
      mock_resp.usage.completion_tokens = 50
      mock_client.chat.completions.create.return_value = mock_resp

      img = Image.new("RGB", (100, 100))
      region = Region(
          id="r0", label="Specs", region_type=RegionType.RULED_TABLE,
          bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
      )
      
      content = extract_content(img, region, "test-model")
      assert content.region_type == RegionType.RULED_TABLE
      assert content.table_json == {
          "columns": ["Features", "LXi"],
          "rows": [{"Features": "Power Steering", "LXi": "yes"}]
      }
      assert content.model_used == "test-model"
  ```

- [ ] **Step 3: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_extract.py -v`
  Expected: FAIL

- [ ] **Step 4: Implement extract.py**
  Create `table_extractor/extract.py`:
  ```python
  import io
  import os
  import json
  import base64
  import httpx
  from PIL import Image
  from openai import OpenAI
  from table_extractor.schemas import Region, ExtractedContent, RegionType
  from table_extractor.cache import cached_call

  client = OpenAI(
      base_url="https://openrouter.ai/api/v1",
      api_key=os.environ.get("OPENROUTER_API_KEY", "mock_key"),
  )

  EXTRACTION_SCHEMAS = {
      RegionType.RULED_TABLE: {
          "type": "object",
          "properties": {
              "columns": {"type": "array", "items": {"type": "string"}},
              "rows": {"type": "array", "items": {"type": "object"}}
          },
          "required": ["columns", "rows"]
      },
      RegionType.SECTION_GROUPED_TABLE: {
          "type": "object",
          "properties": {
              "columns": {"type": "array", "items": {"type": "string"}},
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
              "view": {"type": "string", "enum": ["front", "rear", "side", "top"]},
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
          with open(prompt_path, "r") as f:
              return f.read()
      return f"Extract structured details from this {region_type.value}."

  def _fetch_generation_cost(generation_id: str) -> float:
      api_key = os.environ.get("OPENROUTER_API_KEY")
      if not api_key:
          return 0.0
      try:
          r = httpx.get(
              f"https://openrouter.ai/api/v1/generation?id={generation_id}",
              headers={"Authorization": f"Bearer {api_key}"}
          )
          if r.status_code == 200:
              return r.json().get("data", {}).get("total_cost", 0.0)
      except Exception:
          pass
      return 0.0

  def _call_extraction_api(image_bytes: bytes, region: Region, model: str) -> tuple[dict, dict]:
      base64_image = base64.b64encode(image_bytes).decode("utf-8")
      system_prompt = _load_prompt(region.region_type)
      schema = EXTRACTION_SCHEMAS.get(region.region_type, EXTRACTION_SCHEMAS[RegionType.OTHER])

      # Basic single-retry parser loop for robustness
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
                  tool_choice={"type": "function", "function": {"name": "return_extracted_content"}}
              )
              
              arguments = response.choices[0].message.tool_calls[0].function.arguments
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
              return data, usage_meta
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
  ```

- [ ] **Step 5: Run tests**
  Run: `pytest table_extractor/tests/test_extract.py -v`
  Expected: PASS

- [ ] **Step 6: Commit**
  Run:
  ```bash
  git add table_extractor/prompts/extract_*.txt table_extractor/extract.py table_extractor/tests/test_extract.py
  git commit -m "feat: implement individual region extraction handlers, usage metrics tracking, and OpenRouter API lookup fallback"
  ```

---

### Task 7: Footnote Reconciliation, Consistency Checking & Markdown Generation

**Files:**
* Create: `table_extractor/reconcile.py`
* Create: `table_extractor/tests/test_reconcile.py`

**Interfaces:**
* Consumes: `all_regions: list[Region]`, `img: Image.Image`
* Produces:
  * `resolve_footnotes(regions: list[Region]) -> None`
  * `self_consistency_check(regions: list[Region], img: Image.Image, verification_model: str, sample_rate: float) -> None`
  * `to_markdown(regions: list[Region]) -> str`

- [ ] **Step 1: Write test_reconcile.py**
  Create `table_extractor/tests/test_reconcile.py`:
  ```python
  from table_extractor.schemas import Region, RegionType, ExtractedContent
  from table_extractor.reconcile import resolve_footnotes, to_markdown

  def test_resolve_footnotes():
      # 1. Footnote block region with markers
      fn_content = ExtractedContent(
          region_id="r2", region_type=RegionType.FOOTNOTE_BLOCK,
          items_json=[{"marker": "*", "text": "1.0L Engine variant only."}],
          model_used="test", usage={}
      )
      fn_region = Region(
          id="r2", label="Footnotes", region_type=RegionType.FOOTNOTE_BLOCK,
          bbox=[0, 0, 10, 10], may_contain_subregions=False, extracted=fn_content
      )

      # 2. Table region containing markers
      table_content = ExtractedContent(
          region_id="r1", region_type=RegionType.RULED_TABLE,
          table_json={
              "columns": ["Feature", "Note*"],
              "rows": [{"Feature": "Sunroof", "Note*": "yes*"}]
          },
          model_used="test", usage={}
      )
      # Simulating found footnote markers during run
      table_content.footnote_markers = ["*"]
      table_region = Region(
          id="r1", label="Specs", region_type=RegionType.RULED_TABLE,
          bbox=[0, 0, 10, 10], may_contain_subregions=False, extracted=table_content
      )

      resolve_footnotes([table_region, fn_region])
      # Check that unresolved warnings or dict properties would bind if we had custom props

  def test_to_markdown():
      content = ExtractedContent(
          region_id="r1", region_type=RegionType.RULED_TABLE,
          table_json={
              "columns": ["Feature", "LXi"],
              "rows": [{"Feature": "Airbag", "LXi": "yes"}]
          },
          model_used="test", usage={}
      )
      region = Region(
          id="r1", label="SafetySpecs", region_type=RegionType.RULED_TABLE,
          bbox=[0, 0, 100, 100], may_contain_subregions=False, extracted=content
      )
      md = to_markdown([region])
      assert "SafetySpecs" in md
      assert "| Feature | LXi |" in md
      assert "| Airbag | yes |" in md
  ```

- [ ] **Step 2: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_reconcile.py -v`
  Expected: FAIL

- [ ] **Step 3: Implement reconcile.py**
  Create `table_extractor/reconcile.py`:
  ```python
  import random
  import re
  from PIL import Image
  from table_extractor.schemas import Region, RegionType, ExtractedContent
  from table_extractor.extract import extract_content

  def _extract_markers_from_value(val: str) -> list[str]:
      # Look for typical footnote markers like *, **, ^, ^^, #, ##, etc.
      if not isinstance(val, str):
          return []
      matches = re.findall(r"([\*\^\#]+)", val)
      return matches

  def resolve_footnotes(regions: list[Region]) -> None:
      # Find footnote blocks and construct marker registry
      footnote_registry = {}
      for region in regions:
          if region.extracted and region.region_type == RegionType.FOOTNOTE_BLOCK:
              items = region.extracted.items_json or []
              for item in items:
                  marker = item.get("marker")
                  text = item.get("text")
                  if marker:
                      footnote_registry[marker.strip()] = text.strip()

      # Iterate over all regions and attempt to parse and resolve footnote markers
      for region in regions:
          if not region.extracted or region.region_type == RegionType.FOOTNOTE_BLOCK:
              continue

          found_markers = set()
          # Check in table row/column text
          if region.extracted.table_json:
              cols = region.extracted.table_json.get("columns", [])
              rows = region.extracted.table_json.get("rows", [])
              for col in cols:
                  found_markers.update(_extract_markers_from_value(col))
              for row in rows:
                  for val in row.values():
                      if isinstance(val, str):
                          found_markers.update(_extract_markers_from_value(val))

          # Check in markdown text
          if region.extracted.markdown:
              found_markers.update(_extract_markers_from_value(region.extracted.markdown))

          # Attach found markers
          region.extracted.footnote_markers = list(found_markers)
          
          # Compute resolutions
          resolutions = {}
          for marker in found_markers:
              m_clean = marker.strip()
              if m_clean in footnote_registry:
                  resolutions[marker] = footnote_registry[m_clean]
              else:
                  resolutions[marker] = "WARNING: Unresolved footnote text"

          # Store footnote resolution inside usage details dict or custom print maps
          region.extracted.usage["footnote_resolutions"] = resolutions

  def self_consistency_check(regions: list[Region], img: Image.Image, verification_model: str, sample_rate: float = 0.2, force: bool = False) -> None:
      # Filter atomic regions with extracted content
      eligible = [r for r in regions if r.extracted and not r.may_contain_subregions]
      if not eligible:
          return

      sample_size = max(1, int(len(eligible) * sample_rate))
      sample = random.sample(eligible, sample_size)

      # Import ingest helper here to avoid circular imports
      from table_extractor.ingest import crop_with_padding

      for region in sample:
          crop = crop_with_padding(img, region.bbox, pad_pct=0.03)
          # Call API under the 'extract_verify' caching key using the VERIFICATION_MODEL
          try:
              verify_content = extract_content(
                  crop=crop,
                  region=region,
                  model=verification_model,
                  stage_name="extract_verify",
                  force=force
              )
              
              # Simple diff verification comparison (comparing schemas field-by-field)
              primary_data = region.extracted.table_json or region.extracted.items_json or region.extracted.markdown
              verify_data = verify_content.table_json or verify_content.items_json or verify_content.markdown
              
              if json_dumps_hash(primary_data) != json_dumps_hash(verify_data):
                  region.extracted.confidence_flag = True
          except Exception:
              # Fallback mark confidence false if API verification call failed
              region.extracted.confidence_flag = True

  def json_dumps_hash(data) -> str:
      if not data:
          return ""
      try:
          return json.dumps(data, sort_keys=True)
      except Exception:
          return str(data)

  def _table_to_pipe_table(table_dict: dict) -> str:
      cols = table_dict.get("columns", [])
      rows = table_dict.get("rows", [])
      if not cols:
          return ""
      
      lines = []
      # Column headers
      lines.append("| " + " | ".join(str(c) for c in cols) + " |")
      # Divider
      lines.append("| " + " | ".join("---" for _ in cols) + " |")
      
      # Rows
      for row in rows:
          row_vals = []
          for col in cols:
              # Get matching key value, support fallback
              val = row.get(col, row.get(col.lower(), ""))
              row_vals.append(str(val))
          lines.append("| " + " | ".join(row_vals) + " |")
          
      return "\n".join(lines)

  def to_markdown(regions: list[Region]) -> str:
      md_blocks = []

      def walk(region: Region, path_prefix: str = ""):
          current_path = f"{path_prefix} > {region.label}" if path_prefix else region.label
          
          # Render current region
          if region.extracted:
              md_blocks.append(f"\n## {current_path} (ID: {region.id}, Type: {region.region_type.value})")
              
              content = region.extracted
              # 1. Ruled/Section Grouped Tables
              if content.table_json:
                  md_blocks.append(_table_to_pipe_table(content.table_json))
              
              # 2. Markdown text (bullets)
              elif content.markdown:
                  md_blocks.append(content.markdown)
                  
              # 3. Item List (Swatches, Stat cards, Footnotes)
              elif content.items_json:
                  for idx, item in enumerate(content.items_json):
                      item_str = ", ".join(f"{k}: {v}" for k, v in item.items())
                      md_blocks.append(f"- Item {idx+1}: {item_str}")

              # 4. Technical Drawings
              elif content.drawing_json:
                  draw = content.drawing_json
                  md_blocks.append(f"Technical Drawing view: **{draw.get('view')}**")
                  for m in draw.get("measurements", []):
                      md_blocks.append(f"- {m.get('label')}: {m.get('value')} {m.get('unit')}")

              # Append resolved footnotes details if present
              resolutions = content.usage.get("footnote_resolutions")
              if resolutions:
                  md_blocks.append("\n**Footnotes:**")
                  for marker, text in resolutions.items():
                      md_blocks.append(f"* **{marker}**: {text}")

              # Confidence flag warning
              if content.confidence_flag:
                  md_blocks.append("\n> [!WARNING]\n> Self-consistency verification check disagreed on this region extraction. Human QA suggested.")

          # Recurse children
          for child in region.children:
              walk(child, current_path)

      for region in regions:
          if region.parent_id is None:
              walk(region)

      return "\n".join(md_blocks)
  ```

- [ ] **Step 4: Run tests**
  Run: `pytest table_extractor/tests/test_reconcile.py -v`
  Expected: PASS

- [ ] **Step 5: Commit**
  Run:
  ```bash
  git add table_extractor/reconcile.py table_extractor/tests/test_reconcile.py
  git commit -m "feat: implement footnote resolver, cross-model verify, and Markdown pipe table outputs"
  ```

---

### Task 8: CLI Entrypoint (main.py)

**Files:**
* Create: `table_extractor/main.py`
* Create: `table_extractor/tests/test_main.py`

**Interfaces:**
* Consumes: CLI Arguments (`--image`, `--out`, `--detection-model`, `--extraction-model`, `--verification-model`, `--force-from`)
* Produces: Orchestrated pipeline execution, writing `overlay.png`, `extraction.md`, and `regions.json`.

- [ ] **Step 1: Write test_main.py**
  Create `table_extractor/tests/test_main.py`:
  ```python
  import os
  import subprocess

  def test_cli_help():
      res = subprocess.run(["python", "table_extractor/main.py", "--help"], capture_output=True, text=True)
      assert res.returncode == 0
      assert "--image" in res.stdout
      assert "--detection-model" in res.stdout
  ```

- [ ] **Step 2: Run test to verify failure**
  Run: `pytest table_extractor/tests/test_main.py -v`
  Expected: PASS or FAIL (PASS if command parses, but since main.py isn't created it should FAIL)

- [ ] **Step 3: Implement main.py**
  Create `table_extractor/main.py`:
  ```python
  import argparse
  import json
  import os
  from PIL import Image
  from table_extractor import ingest, detect, extract, reconcile, render

  # Model selection fallback defaults
  DEFAULT_DETECTION_MODEL = "anthropic/claude-3-5-sonnet"
  DEFAULT_EXTRACTION_MODEL = "anthropic/claude-3-5-sonnet"
  DEFAULT_VERIFICATION_MODEL = "google/gemini-2.5-pro"

  def _flatten(regions: list) -> list:
      flat = []
      for r in regions:
          flat.append(r)
          if r.children:
              flat.extend(_flatten(r.children))
      return flat

  def run_pipeline(
      image_path: str,
      out_dir: str,
      detection_model: str,
      extraction_model: str,
      verification_model: str,
      force_from: str = None
  ):
      os.makedirs(out_dir, exist_ok=True)
      
      # Determine cache invalidation layers
      force_detect = force_from in ("detect",)
      force_extract = force_from in ("detect", "extract")
      force_reconcile = force_from in ("detect", "extract", "reconcile")

      # 1. Ingestion
      print(f"[*] Ingesting: {image_path}")
      page_images = ingest.load_and_prep(image_path)
      print(f"[*] Extracted {len(page_images)} page(s) from spreads.")

      for page_idx, img in enumerate(page_images):
          suffix = f"_page{page_idx}" if len(page_images) > 1 else ""
          print(f"[*] Processing page {page_idx}...")

          # 2. Coarse detection
          top_regions = detect.detect_regions(img, detection_model, force=force_detect)

          # 3. Recursive subregion detection
          all_regions = []
          for r in top_regions:
              all_regions.append(r)
              if r.may_contain_subregions:
                  sub = detect.detect_subregions(img, r, detection_model, depth=1, max_depth=2, force=force_detect)
                  r.children = sub
                  all_regions.extend(_flatten(sub))

          # 4. Regional Extraction
          flat_regions = _flatten(top_regions)
          print(f"[*] Extracting content for {len(flat_regions)} regions...")
          for region in flat_regions:
              crop = ingest.crop_with_padding(img, region.bbox, pad_pct=0.03)
              content = extract.extract_content(crop, region, extraction_model, force=force_extract)
              region.extracted = content

          # 5. Reconciliation (Footnotes + Consistency sampling check)
          print("[*] Reconciling footnotes & performing self-consistency verify...")
          reconcile.resolve_footnotes(flat_regions)
          reconcile.self_consistency_check(
              regions=flat_regions,
              img=img,
              verification_model=verification_model,
              sample_rate=0.2,
              force=force_reconcile
          )

          # 6. Render overlay & save
          overlay = render.draw_overlay(img, top_regions)
          overlay_path = os.path.join(out_dir, f"overlay{suffix}.png")
          overlay.save(overlay_path)
          print(f"[+] Overlay saved: {overlay_path}")

          # 7. Convert to Markdown and save
          markdown_output = reconcile.to_markdown(top_regions)
          md_path = os.path.join(out_dir, f"extraction{suffix}.md")
          with open(md_path, "w") as f:
              f.write(markdown_output)
          print(f"[+] Extraction markdown saved: {md_path}")

          # 8. Save structured regions JSON tree
          regions_json = [r.model_dump() for r in top_regions]
          json_path = os.path.join(out_dir, f"regions{suffix}.json")
          with open(json_path, "w") as f:
              json.dump(regions_json, f, indent=2)
          print(f"[+] Structured regions JSON saved: {json_path}")

  def main():
      parser = argparse.ArgumentParser(description="Brochure Table/Region Extraction Pipeline (v0 Prototype)")
      parser.add_argument("--image", required=True, help="Path to input brochure sheet image")
      parser.add_argument("--out", required=True, help="Output directory to save artifacts")
      parser.add_argument("--detection-model", default=DEFAULT_DETECTION_MODEL, help="Model to use for region detection")
      parser.add_argument("--extraction-model", default=DEFAULT_EXTRACTION_MODEL, help="Model to use for extraction")
      parser.add_argument("--verification-model", default=DEFAULT_VERIFICATION_MODEL, help="Model to use for self-consistency verify check")
      parser.add_argument("--force-from", choices=["detect", "extract", "reconcile"], help="Nuke cache downstream of this stage")
      
      args = parser.parse_args()
      
      run_pipeline(
          image_path=args.image,
          out_dir=args.out,
          detection_model=args.detection-model if hasattr(args, "detection-model") else args.detection_model,
          extraction_model=args.extraction-model if hasattr(args, "extraction-model") else args.extraction_model,
          verification_model=args.verification-model if hasattr(args, "verification-model") else args.verification_model,
          force_from=args.force_from
      )

  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests**
  Run: `pytest table_extractor/tests/test_main.py -v`
  Expected: PASS

- [ ] **Step 5: Commit**
  Run:
  ```bash
  git add table_extractor/main.py table_extractor/tests/test_main.py
  git commit -m "feat: complete CLI entrypoint and orchestrate overall pipeline execution flow"
  ```
