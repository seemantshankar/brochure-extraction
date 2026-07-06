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
    SECTION_HEADING = "section_heading"
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
    overlap_warning: bool = False

Region.model_rebuild()
