import pytest
from PIL import Image
from table_extractor.schemas import Region, RegionType, ExtractedContent
from table_extractor.reconcile import resolve_footnotes, self_consistency_check, to_markdown

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
    
    # Check that footnote resolutions are correctly populated
    assert table_region.extracted.usage["footnote_resolutions"] == {
        "*": "1.0L Engine variant only."
    }
    # Unresolved marker should produce warning
    table_content.markdown = "text^ Missing footnote"
    resolve_footnotes([table_region, fn_region])
    assert table_region.extracted.usage["footnote_resolutions"]["^"] == "WARNING: Unresolved footnote text"

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

def test_self_consistency_check_match(mocker):
    # Mock extract_content to return identical content
    mock_extract = mocker.patch("table_extractor.reconcile.extract_content")
    
    primary_content = ExtractedContent(
        region_id="r1", region_type=RegionType.BULLET_PANEL,
        markdown="* Feature A", model_used="primary-model", usage={}
    )
    region = Region(
        id="r1", label="Bullets", region_type=RegionType.BULLET_PANEL,
        bbox=[100, 100, 200, 200], may_contain_subregions=False, extracted=primary_content
    )
    
    verify_content = ExtractedContent(
        region_id="r1", region_type=RegionType.BULLET_PANEL,
        markdown="* Feature A", model_used="verify-model", usage={}
    )
    mock_extract.return_value = verify_content

    img = Image.new("RGB", (300, 300))
    self_consistency_check([region], img, "verify-model", sample_rate=1.0)
    
    assert region.extracted.confidence_flag is False

def test_self_consistency_check_mismatch(mocker):
    # Mock extract_content to return different content
    mock_extract = mocker.patch("table_extractor.reconcile.extract_content")
    
    primary_content = ExtractedContent(
        region_id="r1", region_type=RegionType.BULLET_PANEL,
        markdown="* Feature A", model_used="primary-model", usage={}
    )
    region = Region(
        id="r1", label="Bullets", region_type=RegionType.BULLET_PANEL,
        bbox=[100, 100, 200, 200], may_contain_subregions=False, extracted=primary_content
    )
    
    verify_content = ExtractedContent(
        region_id="r1", region_type=RegionType.BULLET_PANEL,
        markdown="* Feature B", model_used="verify-model", usage={}
    )
    mock_extract.return_value = verify_content

    img = Image.new("RGB", (300, 300))
    self_consistency_check([region], img, "verify-model", sample_rate=1.0)
    
    assert region.extracted.confidence_flag is True

def test_self_consistency_check_api_error(mocker):
    # Mock extract_content to raise an error
    mock_extract = mocker.patch("table_extractor.reconcile.extract_content", side_effect=RuntimeError("API Error"))
    
    primary_content = ExtractedContent(
        region_id="r1", region_type=RegionType.BULLET_PANEL,
        markdown="* Feature A", model_used="primary-model", usage={}
    )
    region = Region(
        id="r1", label="Bullets", region_type=RegionType.BULLET_PANEL,
        bbox=[100, 100, 200, 200], may_contain_subregions=False, extracted=primary_content
    )

    img = Image.new("RGB", (300, 300))
    self_consistency_check([region], img, "verify-model", sample_rate=1.0)
    
    # Fallback to confidence_flag = True on error
    assert region.extracted.confidence_flag is True

def test_to_markdown_comprehensive():
    # 1. Swatch Grid region (items_json)
    swatch_content = ExtractedContent(
        region_id="r2", region_type=RegionType.SWATCH_GRID,
        items_json=[{"swatch_name": "Red", "tone_type": "single"}],
        model_used="test", usage={}
    )
    swatch_region = Region(
        id="r2", label="Colors", region_type=RegionType.SWATCH_GRID,
        bbox=[0, 0, 10, 10], may_contain_subregions=False, extracted=swatch_content
    )

    # 2. Technical drawing region (drawing_json)
    drawing_content = ExtractedContent(
        region_id="r3", region_type=RegionType.TECHNICAL_DRAWING,
        drawing_json={
            "view": "front",
            "measurements": [{"label": "width", "value": 1780, "unit": "mm"}]
        },
        model_used="test", usage={}
    )
    drawing_region = Region(
        id="r3", label="Drawing", region_type=RegionType.TECHNICAL_DRAWING,
        bbox=[0, 0, 10, 10], may_contain_subregions=False, extracted=drawing_content
    )

    # 3. Nesting parent-child structure
    child_content = ExtractedContent(
        region_id="r4-1", region_type=RegionType.BULLET_PANEL,
        markdown="* Child bullet", model_used="test", usage={}
    )
    child_region = Region(
        id="r4-1", label="ChildPanel", region_type=RegionType.BULLET_PANEL,
        bbox=[0, 0, 10, 10], may_contain_subregions=False, parent_id="r4", extracted=child_content
    )
    
    parent_content = ExtractedContent(
        region_id="r4", region_type=RegionType.OTHER,
        markdown="Parent text", model_used="test", usage={}
    )
    parent_region = Region(
        id="r4", label="ParentPanel", region_type=RegionType.OTHER,
        bbox=[0, 0, 100, 100], may_contain_subregions=True, children=[child_region], extracted=parent_content
    )

    # 4. Region with confidence mismatch warning flag and resolved footnotes
    warning_content = ExtractedContent(
        region_id="r5", region_type=RegionType.BULLET_PANEL,
        markdown="* Warning bullet", model_used="test", usage={"footnote_resolutions": {"*": "Resolved text"}},
        confidence_flag=True
    )
    warning_region = Region(
        id="r5", label="WarningPanel", region_type=RegionType.BULLET_PANEL,
        bbox=[0, 0, 10, 10], may_contain_subregions=False, extracted=warning_content
    )

    regions = [swatch_region, drawing_region, parent_region, warning_region]
    md = to_markdown(regions)

    # Asserts
    assert "Colors" in md
    assert "- Item 1: swatch_name: Red, tone_type: single" in md
    
    assert "Drawing" in md
    assert "Technical Drawing view: **front**" in md
    assert "- width: 1780 mm" in md
    
    assert "ParentPanel" in md
    assert "ParentPanel > ChildPanel" in md
    assert "Parent text" in md
    assert "* Child bullet" in md
    
    assert "WarningPanel" in md
    assert "[!WARNING]" in md
    assert "Self-consistency verification check disagreed" in md
    assert "Footnotes:" in md
    assert "* *****: Resolved text" in md

def test_flatten_columns_and_pipe_table_nested():
    from table_extractor.reconcile import _table_to_pipe_table, flatten_columns
    
    # Nested headers test
    cols = [
        "Feature",
        {"group": "Dimensions", "sub_columns": ["Length", "Width"]},
        {"name": "Engine Specs", "columns": ["CC", {"group": "Valves", "sub_columns": ["Intake", "Exhaust"]}]}
    ]
    
    flat = flatten_columns(cols)
    assert flat == [
        "Feature",
        "Dimensions > Length",
        "Dimensions > Width",
        "Engine Specs > CC",
        "Engine Specs > Valves > Intake",
        "Engine Specs > Valves > Exhaust"
    ]
    
    # Pipe table nested test
    table_dict = {
        "columns": cols,
        "rows": [
            {
                "Feature": "Airbag",
                "Dimensions": {"Length": "4500", "Width": "1800"},
                "CC": "1500",
                "Valves": {"Intake": "2", "Exhaust": "2"}
            }
        ]
    }
    
    pipe_table = _table_to_pipe_table(table_dict)
    assert "Feature | Dimensions > Length | Dimensions > Width | Engine Specs > CC | Engine Specs > Valves > Intake | Engine Specs > Valves > Exhaust" in pipe_table
    assert "Airbag | 4500 | 1800 | 1500 | 2 | 2" in pipe_table

def test_footnote_regex_markdown_stripping():
    from table_extractor.reconcile import _extract_markers_from_value
    
    # **bold** shouldn't match trailing asterisks as markers
    assert _extract_markers_from_value("This is **bold** text.") == []
    # *bullet shouldn't match
    assert _extract_markers_from_value("* Bullet point") == []
    # ### Header shouldn't match
    assert _extract_markers_from_value("### Header") == []
    # [link](url) shouldn't match
    assert _extract_markers_from_value("[link](url)") == []
    
    # Valid marker attached to word/number
    assert _extract_markers_from_value("Note*") == ["*"]
    assert _extract_markers_from_value("Value**") == ["**"]
    assert _extract_markers_from_value("100#") == ["#"]
    assert _extract_markers_from_value("data^") == ["^"]

def test_self_consistency_check_drawing_json(mocker):
    # Mock extract_content to return drawing_json mismatch
    mock_extract = mocker.patch("table_extractor.reconcile.extract_content")
    
    primary_content = ExtractedContent(
        region_id="r1", region_type=RegionType.TECHNICAL_DRAWING,
        drawing_json={"view": "front", "measurements": []},
        model_used="primary-model", usage={}
    )
    region = Region(
        id="r1", label="Drawing", region_type=RegionType.TECHNICAL_DRAWING,
        bbox=[100, 100, 200, 200], may_contain_subregions=False, extracted=primary_content
    )
    
    verify_content = ExtractedContent(
        region_id="r1", region_type=RegionType.TECHNICAL_DRAWING,
        drawing_json={"view": "rear", "measurements": []},
        model_used="verify-model", usage={}
    )
    mock_extract.return_value = verify_content

    img = Image.new("RGB", (300, 300))
    self_consistency_check([region], img, "verify-model", sample_rate=1.0)
    
    assert region.extracted.confidence_flag is True


def test_section_heading_rendering_and_sorting():
    # Create top-level regions out of spatial order
    r_draw = Region(
        id="r_draw",
        label="Drawing A",
        region_type=RegionType.TECHNICAL_DRAWING,
        bbox=[100, 200, 200, 300],
        may_contain_subregions=False,
        extracted=ExtractedContent(
            region_id="r_draw",
            region_type=RegionType.TECHNICAL_DRAWING,
            drawing_json={"view": "front", "measurements": []},
            model_used="test",
            usage={}
        )
    )
    r_head = Region(
        id="r_head",
        label="Dimensions (in mm)",
        region_type=RegionType.SECTION_HEADING,
        bbox=[100, 100, 200, 150], # sits above drawing
        may_contain_subregions=False
    )
    
    md = to_markdown([r_draw, r_head])
    # Sibling sort must place r_head before r_draw
    assert "Dimensions (in mm)" in md
    assert md.index("Dimensions (in mm)") < md.index("Drawing A")

