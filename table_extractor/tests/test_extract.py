import os
import pytest
from PIL import Image
from table_extractor.schemas import Region, RegionType
from table_extractor.extract import extract_content
import table_extractor.cache as cache

@pytest.fixture(autouse=True)
def mock_cache_dir(tmp_path):
    orig_dir = cache.CACHE_DIR
    cache.CACHE_DIR = str(tmp_path / ".stage_cache")
    yield
    cache.CACHE_DIR = orig_dir

@pytest.fixture(autouse=True)
def mock_fetch_cost(mocker):
    return mocker.patch("table_extractor.extract._fetch_generation_cost", return_value=0.0)

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
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r0", label="Specs", region_type=RegionType.RULED_TABLE,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.RULED_TABLE
    assert content.table_json == {
        "columns": ["Features", "LXi"],
        "rows": [{"Features": "Power Steering", "LXi": "yes"}]
    }
    assert content.model_used == "test-model"

def test_extract_bullet_panel(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "tier_name": "HTX [In addition to HTK (O)]",
      "inherits_from": "HTK (O)",
      "features": ["Sunroof", "LED DRLs"]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r1", label="Bullet Features", region_type=RegionType.BULLET_PANEL,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.BULLET_PANEL
    assert content.markdown == "### Trim: HTX [In addition to HTK (O)] [Inherits from HTK (O)]\n- Sunroof\n- LED DRLs"
    assert content.model_used == "test-model"

def test_extract_swatch_grid(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "colors": [
        {"swatch_name": "Aurora Black Pearl", "tone_type": "single"},
        {"swatch_name": "Glacier White Pearl with Black Roof", "tone_type": "dual", "roof_color": "Black", "body_color": "Glacier White Pearl"}
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r2", label="Colors", region_type=RegionType.SWATCH_GRID,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.SWATCH_GRID
    assert content.items_json == [
        {"swatch_name": "Aurora Black Pearl", "tone_type": "single"},
        {"swatch_name": "Glacier White Pearl with Black Roof", "tone_type": "dual", "roof_color": "Black", "body_color": "Glacier White Pearl"}
    ]
    assert content.model_used == "test-model"

def test_extract_stat_cards(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "cards": [
        {"card_label": "WagonR Petrol 1L", "variant": "MT", "value": "24.35", "unit": "km/l"}
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r3", label="Stats", region_type=RegionType.STAT_CARDS,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.STAT_CARDS
    assert content.items_json == [
        {"card_label": "WagonR Petrol 1L", "variant": "MT", "value": "24.35", "unit": "km/l"}
    ]
    assert content.model_used == "test-model"

def test_extract_technical_drawing(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "view": "front",
      "measurements": [
        {"label": "width", "value": 1790, "unit": "mm"}
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r4", label="Dimensions", region_type=RegionType.TECHNICAL_DRAWING,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.TECHNICAL_DRAWING
    assert content.drawing_json == {
        "view": "front",
        "measurements": [
            {"label": "width", "value": 1790, "unit": "mm"}
        ]
    }
    assert content.model_used == "test-model"

def test_extract_footnote_block(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "footnotes": [
        {"marker": "*", "text": "T&C Apply"}
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r5", label="Footnotes", region_type=RegionType.FOOTNOTE_BLOCK,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.FOOTNOTE_BLOCK
    assert content.items_json == [
        {"marker": "*", "text": "T&C Apply"}
    ]
    assert content.model_used == "test-model"

def test_extract_api_retries_and_fails(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_client.chat.completions.create.side_effect = Exception("API Error")

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r6", label="Specs", region_type=RegionType.RULED_TABLE,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    try:
        extract_content(img, region, "test-model", force=True)
        assert False, "Should raise exception"
    except Exception as e:
        assert "API Error" in str(e)
        # Should have called API twice (retries)
        assert mock_client.chat.completions.create.call_count == 2

def test_extract_uses_cache(mocker, tmp_path):
    # Override cache directory for isolation
    import table_extractor.cache as cache
    orig_dir = cache.CACHE_DIR
    cache.CACHE_DIR = os.path.join(tmp_path, ".stage_cache")

    try:
        mock_client = mocker.patch("table_extractor.extract.client")
        mock_choice = mocker.MagicMock()
        mock_tool_call = mocker.MagicMock()
        mock_tool_call.function.arguments = """
        {
          "columns": ["Features", "LXi"],
          "rows": [{"Features": "Power Steering", "LXi": "yes"}]
        }
        """
        mock_choice.message.tool_calls = [mock_tool_call]
        mock_resp = mocker.MagicMock()
        mock_resp.id = None
        mock_resp.choices = [mock_choice]
        mock_resp.usage.prompt_tokens = 100
        mock_resp.usage.completion_tokens = 50
        mock_client.chat.completions.create.return_value = mock_resp

        img = Image.new("RGB", (100, 100))
        region = Region(
            id="r7", label="Specs", region_type=RegionType.RULED_TABLE,
            bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
        )
        
        # First call: cache miss, calls client
        content1 = extract_content(img, region, "test-model")
        assert mock_client.chat.completions.create.call_count == 1
        
        # Second call: cache hit, does not call client
        content2 = extract_content(img, region, "test-model")
        assert mock_client.chat.completions.create.call_count == 1
        assert content1.table_json == content2.table_json

        # Force call: cache update, calls client again
        content3 = extract_content(img, region, "test-model", force=True)
        assert mock_client.chat.completions.create.call_count == 2
    finally:
        cache.CACHE_DIR = orig_dir

def test_extract_icon_badge(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "badge_name": "Premium Quality",
      "description": "Indicates high standard materials used"
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r8", label="Badge", region_type=RegionType.ICON_BADGE,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.ICON_BADGE
    assert content.markdown == "**Premium Quality**: Indicates high standard materials used"
    assert content.model_used == "test-model"

def test_extract_icon_badge_no_description(mocker):
    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "badge_name": "Eco Friendly"
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r9", label="Badge", region_type=RegionType.ICON_BADGE,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    content = extract_content(img, region, "test-model", force=True)
    assert content.region_type == RegionType.ICON_BADGE
    assert content.markdown == "**Eco Friendly**: "
    assert content.model_used == "test-model"

def test_extract_usage_tracking(mocker):
    from table_extractor.extract import get_total_usage, total_extraction_usage, extract_content
    
    # Reset usage dict
    total_extraction_usage["prompt_tokens"] = 0
    total_extraction_usage["completion_tokens"] = 0
    total_extraction_usage["cost_usd"] = 0.0

    mock_client = mocker.patch("table_extractor.extract.client")
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "badge_name": "Eco Friendly"
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_resp = mocker.MagicMock()
    mock_resp.id = "gen-abc"
    mock_resp.choices = [mock_choice]
    mock_resp.usage.prompt_tokens = 100
    mock_resp.usage.completion_tokens = 50
    mock_client.chat.completions.create.return_value = mock_resp

    mocker.patch("table_extractor.extract._fetch_generation_cost", return_value=0.0)

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r9", label="Badge", region_type=RegionType.ICON_BADGE,
        bbox=[0, 0, 1000, 1000], may_contain_subregions=False, depth=0
    )
    
    extract_content(img, region, "test-model", force=True)

    usage = get_total_usage()
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    # Cost = 100 * 0.000003 + 50 * 0.000015 = 0.0003 + 0.00075 = 0.00105
    assert pytest.approx(usage["cost_usd"]) == 0.00105
