import pytest
import os
import shutil
from PIL import Image
import table_extractor.cache as cache
from table_extractor.schemas import Region, RegionType
from table_extractor.detect import detect_regions, detect_subregions, normalize_bbox_list

@pytest.fixture(autouse=True)
def temp_cache(tmp_path):
    test_dir = os.path.join(tmp_path, ".stage_cache")
    orig_dir = cache.CACHE_DIR
    cache.CACHE_DIR = test_dir
    yield
    cache.CACHE_DIR = orig_dir
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

@pytest.fixture(autouse=True)
def reset_usage():
    from table_extractor.detect import total_detection_usage
    total_detection_usage["prompt_tokens"] = 0
    total_detection_usage["completion_tokens"] = 0
    total_detection_usage["cost_usd"] = 0.0

def test_detect_regions_calls_openrouter(mocker):
    # Mock the OpenAI client call
    mock_client = mocker.patch("table_extractor.detect._client")
    
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

def test_detect_subregions_recursive(mocker):
    # Mock the OpenAI client call
    mock_client = mocker.patch("table_extractor.detect._client")
    
    # We want to mock 2 calls:
    # 1. Level 1: returns one subregion with may_contain_subregions = True
    # 2. Level 2: returns a subregion with may_contain_subregions = False
    
    mock_choice_l1 = mocker.MagicMock()
    mock_tool_call_l1 = mocker.MagicMock()
    mock_tool_call_l1.function.arguments = """
    {
      "regions": [
        {
          "label": "Subregion L1",
          "region_type": "bullet_panel",
          "bbox": [100, 100, 700, 700],
          "may_contain_subregions": true
        }
      ]
    }
    """
    mock_choice_l1.message.tool_calls = [mock_tool_call_l1]
    
    mock_choice_l2 = mocker.MagicMock()
    mock_tool_call_l2 = mocker.MagicMock()
    mock_tool_call_l2.function.arguments = """
    {
      "regions": [
        {
          "label": "Subregion L2",
          "region_type": "other",
          "bbox": [100, 100, 700, 700],
          "may_contain_subregions": false
        }
      ]
    }
    """
    mock_choice_l2.message.tool_calls = [mock_tool_call_l2]
    
    # Set side effect to return Level 1 then Level 2
    mock_response_l1 = mocker.MagicMock()
    mock_response_l1.choices = [mock_choice_l1]
    
    mock_response_l2 = mocker.MagicMock()
    mock_response_l2.choices = [mock_choice_l2]
    
    mock_client.chat.completions.create.side_effect = [mock_response_l1, mock_response_l2]
    
    img = Image.new("RGB", (100, 100))
    parent_region = Region(
        id="r0",
        label="Parent",
        region_type=RegionType.RULED_TABLE,
        bbox=[50, 50, 950, 950],
        may_contain_subregions=True,
        depth=0
    )
    
    subregions = detect_subregions(img, parent_region, "test-model", depth=1, max_depth=2)
    
    # Verify structure
    assert len(subregions) == 1
    sub_l1 = subregions[0]
    assert sub_l1.label == "Subregion L1"
    assert sub_l1.depth == 1
    assert sub_l1.bbox == pytest.approx([121.6, 121.6, 539.2, 539.2])
    assert len(sub_l1.children) == 1
    
    sub_l2 = sub_l1.children[0]
    assert sub_l2.label == "Subregion L2"
    assert sub_l2.id == "r0_s0_s0"
    assert sub_l2.depth == 2
    assert sub_l2.parent_id == "r0_s0"
    assert sub_l2.bbox == pytest.approx([121.6, 121.6, 539.2, 539.2])

def test_detect_subregions_respects_max_depth(mocker):
    mock_client = mocker.patch("table_extractor.detect._client")
    
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "regions": [
        {
          "label": "Subregion L2",
          "region_type": "swatch_grid",
          "bbox": [100, 100, 700, 700],
          "may_contain_subregions": true
        }
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response = mocker.MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response
    
    img = Image.new("RGB", (100, 100))
    parent_region = Region(
        id="r0",
        label="Parent",
        region_type=RegionType.RULED_TABLE,
        bbox=[50, 50, 950, 950],
        may_contain_subregions=True,
        depth=0
    )
    
    # Call with max_depth=1
    subregions = detect_subregions(img, parent_region, "test-model", depth=1, max_depth=1)
    
    # Even though subregions returned have may_contain_subregions=True, depth is 1 which equals max_depth.
    # Therefore, recursion to depth 2 should not happen.
    assert len(subregions) == 1
    assert subregions[0].bbox == [100.0, 100.0, 700.0, 700.0]
    assert len(subregions[0].children) == 0

def test_detect_usage_tracking(mocker):
    from table_extractor.detect import get_total_usage, total_detection_usage
    
    mock_client = mocker.patch("table_extractor.detect._client")
    
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "regions": []
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response = mocker.MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 200
    mock_response.usage.completion_tokens = 100
    mock_response.id = "gen-123"
    
    mock_client.chat.completions.create.return_value = mock_response

    # Mock _fetch_generation_cost to return 0.0 to test estimation fallback
    mocker.patch("table_extractor.detect._fetch_generation_cost", return_value=0.0)

    img = Image.new("RGB", (100, 100))
    detect_regions(img, "test-model", force=True)

    usage = get_total_usage()
    assert usage["prompt_tokens"] == 200
    assert usage["completion_tokens"] == 100
    # Cost = 200 * 0.000003 + 100 * 0.000015 = 0.0006 + 0.0015 = 0.0021
    assert pytest.approx(usage["cost_usd"]) == 0.0021

def test_normalize_bbox_list():
    # Case 1: Already normalized (0-1000 scale)
    bboxes = [[10.0, 20.0, 950.0, 980.0]]
    res = normalize_bbox_list(bboxes, 3504, 2478)
    assert res == [[10.0, 20.0, 950.0, 980.0]]

    # Case 2: Downscaled pixel coordinates (e.g. 2247x1560 scale on 3504x2478 image)
    # shorter side 2478 > 1560 -> scale = 1560/2478 = 0.6295 -> w_space = 2206, h_space = 1560
    # Returned coordinates up to 2247 and 1560
    bboxes = [
        [40.0, 117.0, 758.0, 1029.0],
        [40.0, 1440.0, 2247.0, 1560.0]
    ]
    res = normalize_bbox_list(bboxes, 3504, 2478)
    # The coordinate space should be (2247, 1560)
    # 40 / 2247 * 1000 = 17.80
    # 117 / 1560 * 1000 = 75.00
    # 2247 / 2247 * 1000 = 1000.0
    # 1560 / 1560 * 1000 = 1000.0
    assert pytest.approx(res[0][0], abs=0.1) == 17.8
    assert pytest.approx(res[0][1], abs=0.1) == 75.0
    assert pytest.approx(res[1][2], abs=0.1) == 1000.0
    assert pytest.approx(res[1][3], abs=0.1) == 1000.0

    # Case 3: Original pixel coordinates (e.g. 3504x2478 scale on 3504x2478 image)
    bboxes = [
        [40.0, 117.0, 3500.0, 2450.0]
    ]
    res = normalize_bbox_list(bboxes, 3504, 2478)
    # space_w = 3504, space_h = 2478
    assert pytest.approx(res[0][0], abs=0.1) == (40.0 / 3504.0 * 1000.0)
    assert pytest.approx(res[0][2], abs=0.1) == (3500.0 / 3504.0 * 1000.0)

    # Case 4: Small image (800x600) with pixel coordinates (e.g. max_x = 780, max_y = 580)
    # Since max_x <= 1005 and max_y <= 1005, but max_x <= w and max_y <= h, it detects as pixel coordinates
    bboxes = [
        [100.0, 100.0, 780.0, 580.0]
    ]
    res = normalize_bbox_list(bboxes, 800, 600)
    # scaled by 800 and 600
    assert pytest.approx(res[0][0], abs=0.1) == (100.0 / 800.0 * 1000.0)
    assert pytest.approx(res[0][2], abs=0.1) == (780.0 / 800.0 * 1000.0)
    assert pytest.approx(res[0][3], abs=0.1) == (580.0 / 600.0 * 1000.0)

def test_detect_duplicate_subregion_filtering(mocker):
    # Mock OpenAI client
    mock_client = mocker.patch("table_extractor.detect._client")

    # Mock response returning a duplicate subregion (covering 81% of crop area, IoU = 0.81)
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "regions": [
        {
          "label": "Duplicate Subregion",
          "region_type": "ruled_table",
          "bbox": [0, 0, 900, 900],
          "may_contain_subregions": false
        }
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response = mocker.MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response

    img = Image.new("RGB", (100, 100))
    parent_region = Region(
        id="r0",
        label="Parent Table",
        region_type=RegionType.RULED_TABLE,
        bbox=[100, 100, 900, 900],
        may_contain_subregions=True,
        depth=0
    )

    sub = detect_subregions(img, parent_region, "test-model", depth=1, max_depth=2, force=True)
    # The duplicate subregion should be filtered out, so list should be empty
    assert len(sub) == 0

def test_refine_region_zoom(mocker):
    # Mock OpenAI client
    mock_client = mocker.patch("table_extractor.detect._client")

    # Mock response returning a refined tighter bounding box
    mock_choice = mocker.MagicMock()
    mock_tool_call = mocker.MagicMock()
    mock_tool_call.function.arguments = """
    {
      "regions": [
        {
          "label": "Refined Table",
          "region_type": "ruled_table",
          "bbox": [100, 100, 900, 900],
          "may_contain_subregions": false
        }
      ]
    }
    """
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response = mocker.MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response

    # Test coordinate mapping
    # Image size: 100x100. Let's trace the math:
    # Region bbox: [100, 100, 900, 900]
    # pad_pct = 0.08, pw = 800, ph = 800, pad_w = 64, pad_h = 64
    # gx0_crop = max(0.0, 100 - 64) = 36.0
    # gy0_crop = max(0.0, 100 - 64) = 36.0
    # gx1_crop = min(1000.0, 900 + 64) = 964.0
    # gy1_crop = min(1000.0, 900 + 64) = 964.0
    # pw_padded = 964.0 - 36.0 = 928.0
    # ph_padded = 964.0 - 36.0 = 928.0
    # The vision model returned refined bbox: [100, 100, 900, 900] inside crop.
    # Refined coordinates calculation:
    # sx0 = 100, sy0 = 100, sx1 = 900, sy1 = 900 (assuming normalized/no scale issues in crop)
    # gx0 = gx0_crop + (sx0 / 1000 * pw_padded) = 36.0 + 0.1 * 928.0 = 128.8
    # gy0 = gy0_crop + (sy0 / 1000 * ph_padded) = 36.0 + 0.1 * 928.0 = 128.8
    # gx1 = gx0_crop + (sx1 / 1000 * pw_padded) = 36.0 + 0.9 * 928.0 = 36.0 + 835.2 = 871.2
    # gy1 = gy0_crop + (sy1 / 1000 * ph_padded) = 36.0 + 0.9 * 928.0 = 36.0 + 835.2 = 871.2

    img = Image.new("RGB", (100, 100))
    region = Region(
        id="r0",
        label="Coarse Table",
        region_type=RegionType.RULED_TABLE,
        bbox=[100, 100, 900, 900],
        may_contain_subregions=False,
        depth=0
    )

    from table_extractor.detect import refine_region_zoom
    refined = refine_region_zoom(img, region, "test-model", force=True)

    assert refined.label == "Refined Table"
    assert refined.bbox == pytest.approx([128.8, 128.8, 871.2, 871.2])


def test_safe_get_regions_stringified():
    from table_extractor.detect import _safe_get_regions
    raw_json = {
        "regions": '[{"label": "Test", "region_type": "ruled_table", "bbox": [0,0,10,10], "may_contain_subregions": false}]\n</invoke>\n'
    }
    res = _safe_get_regions(raw_json)
    assert len(res) == 1
    assert res[0]["label"] == "Test"


