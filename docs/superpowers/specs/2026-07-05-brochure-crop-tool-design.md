# Brochure Crop Tool — Design Spec

**Date:** 2026-07-05
**Status:** Draft — awaiting user review

---

## 1. Overview & Goals

This is a complete pivot from the previous automated region-detection pipeline. We are building a human-in-the-loop web tool for identifying and extracting complex layout regions (tables, swatches, image grids, feature matrices, etc.) from brochure pages.

### Workflow

1. **Upload & Analyze** — User uploads a PDF or set of images. Each page is rendered at 300 DPI (if PDF) and sent to an LLM for complexity analysis. Pages with complex layouts are flagged.
2. **Annotate** — User opens flagged pages in a canvas-based annotation tool. Draws bounding boxes to define crop regions. Crops appear in a side panel.
3. **Commit & Fine-tune** — User commits all crops to disk. Each committed crop can be clicked to open a fine-tuning modal for further trimming.

### Key Constraints

- Flask + vanilla HTML/JS (no frontend framework)
- LLM analysis via OpenRouter using model ID from `.env` (`PAGE_ANALYSIS_MODEL_ID`)
- PDF-to-image conversion at 300 DPI using `pdf2image` (Poppler)
- All state stored on disk (no database)
- Existing `table_extractor/` pipeline is preserved but not modified

---

## 2. Architecture & File Structure

```
Brochure Extraction/
├── crop_app/                     # New web app package
│   ├── __init__.py
│   ├── app.py                    # Flask server, routes, session management
│   ├── llm.py                    # LLM complexity analysis (OpenRouter)
│   ├── pdf_converter.py          # PDF → 300 DPI PNG via pdf2image
│   ├── crop_manager.py           # Crop save/load/trim logic
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css
│   │   └── js/
│   │       ├── upload.js         # Upload & analysis page
│   │       ├── annotate.js       # Canvas annotation page
│   │       └── trim.js           # Crop fine-tuning modal
│   └── templates/
│       ├── index.html            # Upload & analysis page
│       └── annotate.html         # Annotation canvas page
├── uploads/                      # Temp session storage (gitignored)
│   └── <session_id>/
│       ├── meta.json             # Session state: page complexity, crop coords
│       ├── pages/                # 300 DPI page PNGs
│       └── original/             # Original uploaded file(s)
├── crops/                        # Final committed crops (gitignored)
│   └── <session_id>/
│       ├── crop_000.png
│       ├── crop_001.png
│       └── ...
```

### Session Lifecycle

1. User uploads files → Flask generates a UUID `session_id`
2. `uploads/<session_id>/` created; files processed and stored
3. `meta.json` tracks: page list, complexity labels per page, crop boxes per page, crop file paths
4. Crops saved to `crops/<session_id>/`
5. Session persists across browser refreshes (all state on disk)

---

## 3. Upload & Analysis Flow

### 3.1 Upload Page (`/`)

- Drag-and-drop zone or file picker
- Accepts: one PDF, or multiple images (PNG/JPG)
- On submit → Flask creates session, processes pages

### 3.2 PDF Conversion

- Uses `pdf2image.convert_from_path()` at `dpi=300`
- Each page saved as `pages/page_000.png`, `page_001.png`, ...
- Original PDF stored in `original/`

### 3.3 LLM Complexity Analysis

- Each page image sent to the model specified in `.env` as `PAGE_ANALYSIS_MODEL_ID`
- API call: OpenRouter chat/completions with image in messages (vision)
- `reasoning.enabled: true` as per user's reference implementation
- Prompt instructs the model to return structured JSON: `{"complex": bool, "labels": [...]}`
- Labels include: `table`, `swatch_grid`, `image_grid`, `text_grid`, `feature_matrix`, `stat_cards`, `technical_drawing`, `none`
- Pages where `complex == true` are flagged
- Results stored in `meta.json` under each page entry

### 3.4 Results Display

- Thumbnail grid of all pages
- Each complex page shows a colored badge/tag with complexity labels
- Click a complex page → navigates to `/annotate/<session_id>?page=<n>`

---

## 4. Annotation Canvas

### 4.1 Page Layout

```
┌─────────────────────────────────────────────────────────┐
│  [Page thumbnails with complexity badges]              │
├──────────────────────────────┬──────────────────────────┤
│                              │  Crop Preview Gallery    │
│   Canvas (Page Image)        │                          │
│   - Zoomable                 │  ┌─────┐  ┌─────┐       │
│   - Pannable                 │  │crop1│  │crop2│       │
│   - Bounding boxes           │  └─────┘  └─────┘       │
│   - Zoom controls (+/−)      │                          │
│                              │                          │
├──────────────────────────────┴──────────────────────────┤
│  [Commit All Crops]  [Undo]  Zoom: 100%                │
└─────────────────────────────────────────────────────────┘
```

### 4.2 Zoom & Pan

- `+`/`−` buttons (and keyboard `+`/`-` shortcuts) adjust zoom level
- Range: **25% to 500%** (0% = fit-to-viewport as initial view)
- Zoom applied via `ctx.scale()` + `ctx.translate()` — not CSS scaling (avoids blurriness)
- When image exceeds canvas viewport bounds → pan is available
- **Pan trigger:** click-and-drag on empty canvas area (no bounding box under cursor)
- Cursor: `grab` (idle) → `grabbing` (actively dragging)
- Cursor changes to crosshair (`crosshair`) when user is not near any box or handle

### 4.3 Coordinate System

- Bounding boxes stored as **normalized coordinates** `[x0, y0, x1, y1]` (0–1 relative to image dimensions)
- Screen rendering applies: `screen_x = (normalized_x * image_width) * zoom + pan_offset_x`
- All drawing (boxes, crop previews) derives from normalized coords → zoom changes re-render boxes automatically with no recalculation
- Mouse events inverse-projected: `normalized_x = (screen_x - pan_offset_x) / (image_width * zoom)`

---

## 5. Bounding Box Interaction — Cursor-Driven Model

### 5.1 Four Zone Priority System

When a bounding box exists, the cursor position determines the action. Priority (highest → lowest):

| Priority | Zone | Location (screen-space) | Cursor | Action |
|---|---|---|---|---|
| 1 | **North Drag Handle** | Visible icon floating above the North edge, ~12px gap, 24×10px visual + 6px invisible padding hit area | `grab` / `grabbing` | Repositions the **entire box** only. Does not pan the page. |
| 2 | **Corner Hit Zone** | 14×14px invisible area centered on each of the 4 vertices (NW, NE, SW, SE) | `nwse-resize` or `nesw-resize` (diagonal arrows) | 2-axis resize from that corner |
| 3 | **Edge Hit Zone** | ~8px invisible band along N/S/E/W edges, **excluding** the corner 14×14px areas | `ns-resize` (vertical arrows) for N/S, `ew-resize` (horizontal arrows) for E/W | Single-axis resize along that edge |
| 4 | **Background / Canvas** | Everywhere else on the canvas | `grab` / `grabbing` | **Pans the page image** |

When no bounding box is near the cursor → cursor is `crosshair` (signals "draw mode" — draw mode is activated on mousedown in background zone, see below).

### 5.2 Drawing New Boxes

- User clicks on empty canvas area (background zone) → mousedown starts box drawing
- Cursor during draw: `crosshair`
- Drag → shows rubber-band rectangle outline
- Mouse-up → box is created with normalized coordinates, becomes active/selected
- If the user wants to pan but there are boxes nearby → they must drag from an area where no box edge/handle/drag-handle is under the cursor. The cursor is `grab` near the background zone between boxes.

> **Design decision:** Drawing always starts from the background zone. Pan also uses background zone. To disambiguate: if mousedown occurs on empty canvas and a new box is being drawn, it's draw mode. To pan, user must be far enough from any box boundary. This is resolved by checking if the cursor is within the edge/corner hit zone of any existing box. If not within any interactive zone, it's "empty canvas" → **draw mode by default, pan mode via Shift+drag or middle-mouse-button**.

**Revised rule:** Background zone = **draw mode** by default. **Pan** is triggered by:
- Holding **Shift** while dragging on empty canvas
- **Middle-mouse-button** drag
- Clicking and dragging on the canvas area that is outside the image bounds (margin/padding area around the image)

This prevents conflict between "draw new box" and "pan page."

### 5.3 North Drag Handle (Move Box)

- **Visual:** horizontal bar icon with 3 parallel lines (grip pattern), ~24×10px screen-space
- **Position:** centered horizontally above the box's North edge, with ~12px gap
- **Hit area:** visual bar + 6px invisible padding around it (generous click target)
- **Style:** `rgba(blue, 0.5)` default → `rgba(blue, 1.0)` on hover
- **Optional:** thin 1px dotted stem connecting handle to box's North edge center
- **Scale:** always fixed CSS pixel size — does not change with zoom level
- **On drag:** box moves in screen space (inverse-projected to normalized coords); crop preview updates live

### 5.4 Edge & Corner Resize (Cursor-Only, No Visible Handles)

- **No visible dots/squares** — the cursor itself is the affordance
- Hover feedback: the edge under the cursor briefly brightens (stroke width increases from 2px to 3px, color intensifies) — confirms "you can drag this" before clicking
- On drag: box resizes from the opposite anchor (e.g., dragging the East edge → West edge stays fixed, unless the user started from the handle on the East side)

### 5.5 Mouse Offset Capture

On `mousedown` for any drag/resize action:
- Record the offset between the cursor position and the anchor point (handle center for move, box corner/edge for resize)
- During `mousemove`, subtract this offset from cursor position to compute the new box geometry
- Prevents the box "jumping" to the cursor at the start of a drag

### 5.6 Box Selection

- A box becomes "selected" on click (mousedown on interactive zone → release without significant movement = selection, not drag)
- Selected box: outline becomes fully opaque, drag handle becomes visible, `×` delete appears
- Clicking outside all boxes (on background) → deselects current box, enters draw mode
- `Delete` key or `Backspace` → removes selected box and its crop preview

### 5.7 Delete

- **`×` button:** appears near top-right of selected box (outside the box). Click to remove box and crop.
- **Keyboard:** `Delete` or `Backspace` key removes the currently selected box

---

## 6. Crop Preview Panel (Right Side)

- Each bounding box has a corresponding crop preview card in the right panel
- Card shows: cropped image (from actual page pixels, always 100% scale regardless of canvas zoom) + a small label (e.g., "Crop 1")
- Updates **live** during resize/drag (re-extracts pixels using normalized coordinates)
- Cards stacked vertically (or in a grid if many crops)
- Cards are clickable (after commit) — see Section 7

---

## 7. Commit & Post-Commit Fine-Tuning

### 7.1 Commit

- "Commit All Crops" button at bottom of annotation page
- On click: POST all normalized bounding box coordinates to Flask backend
- Backend saves each crop as an individual PNG in `crops/<session_id>/`
- Files named: `crop_000.png`, `crop_001.png`, etc.
- `meta.json` updated with crop file paths and coordinates
- Right-panel cards now show the committed crop image and become clickable

### 7.2 Fine-Tuning (Post-Commit Trim)

- User clicks a committed crop card → opens a **trim modal**
- Modal contains:
  - The crop image displayed on its own canvas at 1:1 (or scaled to fit)
  - User draws ONE tighter bounding box on this crop to trim it further
  - Same cursor-driven interaction (edges/corners for resize, drag handle for move, background for draw/pan)
  - "Update" button saves the trimmed crop back to the same file path on disk
  - "Cancel" discards changes

---

## 8. Error Handling

| Scenario | Handling |
|---|---|
| PDF conversion fails (no Poppler) | Show error on upload page: "pdf2image requires Poppler. Install with `brew install poppler`" |
| LLM API call fails | Retry once; if still fails, mark page as "analysis failed" with retry button |
| LLM returns invalid JSON | Mark page as "analysis failed" with retry button; log raw response |
| Canvas rendering error | Show fallback error message, allow page retry |
| Crop save fails (disk full, permissions) | Show toast error on annotation page |
| Session files missing (user navigates with bad URL) | Show "Session not found" message with link back to upload |

---

## 9. Environment Dependencies

- **Python packages:** `flask`, `Pillow`, `pdf2image`, `openai` (for OpenRouter), `pydantic` (reuse from existing)
- **System:** Poppler (for `pdf2image` PDF rendering)
- **Browser:** Modern Chrome/Firefox/Safari with Canvas 2D API support
- **`.env` keys used by crop_app:**
  - `OPENROUTER_API_KEY` — OpenRouter API key
  - `PAGE_ANALYSIS_MODEL_ID` — model ID for complexity analysis (user-configured)

---

## 10. Testing Strategy

- **Unit tests** in `crop_app/tests/`:
  - `test_pdf_converter.py` — PDF → PNG conversion, page count, DPI
  - `test_llm.py` — mock LLM calls, test JSON parsing, error handling, retry logic
  - `test_crop_manager.py` — crop coordinate normalization, pixel extraction, file save
  - `test_app.py` — Flask route tests: upload, session creation, analysis, commit
- **Manual testing:**
  - Upload a multi-page PDF → verify all pages render at 300 DPI
  - Draw/resize/move/delete bounding boxes at various zoom levels
  - Verify crop previews update live and match committed files
  - Test fine-tuning modal on committed crops
  - Test across different page aspect ratios and complexity levels
