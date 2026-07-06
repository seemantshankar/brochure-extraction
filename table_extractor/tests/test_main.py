import os
import sys
import json
import subprocess
from PIL import Image
from table_extractor.main import run_pipeline
from table_extractor.schemas import Region, RegionType, ExtractedContent

def test_cli_help():
    # Pass current PYTHONPATH to the subprocess to allow resolving table_extractor
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.pathsep.join(filter(None, [os.getcwd(), env.get("PYTHONPATH", "")]))
    
    res = subprocess.run([sys.executable, "table_extractor/main.py", "--help"], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "--image" in res.stdout
    assert "--detection-model" in res.stdout
    assert "--refinement-method" in res.stdout

def test_run_pipeline_orchestration(mocker, tmp_path):
    # Mocking submodules
    mock_img = Image.new("RGB", (100, 100))
    mock_load_and_prep = mocker.patch("table_extractor.ingest.load_and_prep", return_value=[mock_img])
    
    region = Region(
        id="r0",
        label="Test Region",
        region_type=RegionType.RULED_TABLE,
        bbox=[0, 0, 1000, 1000],
        may_contain_subregions=True
    )
    mock_detect_regions = mocker.patch("table_extractor.detect.detect_regions", return_value=[region])
    
    subregion = Region(
        id="r0_s0",
        label="Subregion",
        region_type=RegionType.OTHER,
        bbox=[10, 10, 990, 990],
        may_contain_subregions=False,
        depth=1
    )
    mock_detect_subregions = mocker.patch("table_extractor.detect.detect_subregions", return_value=[subregion])
    
    mock_refine_region_zoom = mocker.patch("table_extractor.detect.refine_region_zoom", side_effect=lambda img, r, model, force=False: r)
    
    mock_crop_with_padding = mocker.patch("table_extractor.ingest.crop_with_padding", return_value=mock_img)
    
    extracted_content = ExtractedContent(
        region_id="r0",
        region_type=RegionType.RULED_TABLE,
        markdown="# Title\n| col1 |\n|---|",
        model_used="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 0.0001}
    )
    mock_extract_content = mocker.patch("table_extractor.extract.extract_content", return_value=extracted_content)
    
    mock_resolve_footnotes = mocker.patch("table_extractor.reconcile.resolve_footnotes")
    mock_self_consistency_check = mocker.patch("table_extractor.reconcile.self_consistency_check")
    mock_to_markdown = mocker.patch("table_extractor.reconcile.to_markdown", return_value="## Mock Markdown Output")
    
    mock_draw_overlay = mocker.patch("table_extractor.render.draw_overlay", return_value=mock_img)
    
    mocker.patch("table_extractor.detect.get_total_usage", return_value={"prompt_tokens": 10, "completion_tokens": 10, "cost_usd": 0.1})
    mocker.patch("table_extractor.extract.get_total_usage", return_value={"prompt_tokens": 10, "completion_tokens": 10, "cost_usd": 0.1})

    out_dir = str(tmp_path / "output")
    
    run_pipeline(
        image_path="dummy.png",
        out_dir=out_dir,
        detection_model="det-model",
        extraction_model="ext-model",
        verification_model="ver-model",
        force_from="extract"
    )
    
    # Assert call structures
    mock_load_and_prep.assert_called_once_with("dummy.png")
    mock_detect_regions.assert_called_once_with(mock_img, "det-model", force=False)
    mock_refine_region_zoom.assert_called_once_with(mock_img, region, "det-model", force=False)
    mock_detect_subregions.assert_called_once_with(mock_img, region, "det-model", depth=1, max_depth=2, force=False)
    
    # Check that extract was called with force=True because force_from="extract"
    # force_from="extract" -> force_detect=False, force_extract=True, force_reconcile=True.
    # The parent container region should be skipped, so call_count is 1.
    assert mock_extract_content.call_count == 1
    mock_extract_content.assert_called_once_with(mock_img, subregion, "ext-model", force=True)
    
    mock_resolve_footnotes.assert_called_once()
    mock_self_consistency_check.assert_called_once_with(
        regions=[region, subregion],
        img=mock_img,
        verification_model="ver-model",
        sample_rate=0.2,
        force=True
    )
    
    # Check that output files exist
    assert os.path.exists(os.path.join(out_dir, "overlay.png"))
    assert os.path.exists(os.path.join(out_dir, "extraction.md"))
    assert os.path.exists(os.path.join(out_dir, "regions.json"))
    assert os.path.exists(os.path.join(out_dir, "crops"))
    assert os.path.exists(os.path.join(out_dir, "crops", "page0_r0_ruled_table.png"))
    assert os.path.exists(os.path.join(out_dir, "crops", "page0_r0_s0_other.png"))
    
    # Verify contents of JSON file
    with open(os.path.join(out_dir, "regions.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["label"] == "Test Region"
    assert len(data[0]["children"]) == 1
    assert data[0]["children"][0]["label"] == "Subregion"


def test_cli_refinement_method_parsing(mocker):
    # Mock run_pipeline to intercept inputs
    mock_run = mocker.patch("table_extractor.main.run_pipeline")
    mocker.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake-key"})
    
    # Mock sys.argv to simulate CLI input
    mocker.patch("sys.argv", [
        "main.py",
        "--image", "test_img.png",
        "--out", "test_out",
        "--refinement-method", "ocr"
    ])
    
    from table_extractor.main import main
    main()
    
    mock_run.assert_called_once()
    kwargs = mock_run.call_args[1]
    assert kwargs["refinement_method"] == "ocr"


def test_run_pipeline_refinement_none(mocker, tmp_path):
    mock_img = Image.new("RGB", (100, 100))
    mock_load_and_prep = mocker.patch("table_extractor.ingest.load_and_prep", return_value=[mock_img])
    
    region = Region(
        id="r0",
        label="Test Region",
        region_type=RegionType.RULED_TABLE,
        bbox=[0, 0, 1000, 1000],
        may_contain_subregions=True
    )
    mock_detect_regions = mocker.patch("table_extractor.detect.detect_regions", return_value=[region])
    mock_detect_subregions = mocker.patch("table_extractor.detect.detect_subregions")
    mocker.patch("table_extractor.ingest.crop_with_padding", return_value=mock_img)
    
    extracted_content = ExtractedContent(
        region_id="r0",
        region_type=RegionType.RULED_TABLE,
        markdown="# Title\n| col1 |\n|---|",
        model_used="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 0.0001}
    )
    mocker.patch("table_extractor.extract.extract_content", return_value=extracted_content)
    mocker.patch("table_extractor.reconcile.resolve_footnotes")
    mocker.patch("table_extractor.reconcile.self_consistency_check")
    mocker.patch("table_extractor.reconcile.to_markdown", return_value="## Mock Markdown Output")
    mocker.patch("table_extractor.render.draw_overlay", return_value=mock_img)
    mocker.patch("table_extractor.detect.get_total_usage", return_value={})
    mocker.patch("table_extractor.extract.get_total_usage", return_value={})

    out_dir = str(tmp_path / "output_none")
    run_pipeline(
        image_path="dummy.png",
        out_dir=out_dir,
        detection_model="det-model",
        extraction_model="ext-model",
        verification_model="ver-model",
        refinement_method="none"
    )
    
    mock_detect_subregions.assert_not_called()


def test_run_pipeline_refinement_zoom_recursive(mocker, tmp_path):
    mock_img = Image.new("RGB", (100, 100))
    mock_load_and_prep = mocker.patch("table_extractor.ingest.load_and_prep", return_value=[mock_img])
    
    parent_region = Region(
        id="r0",
        label="Parent Table",
        region_type=RegionType.RULED_TABLE,
        bbox=[0, 0, 1000, 1000],
        may_contain_subregions=True
    )
    mock_detect_regions = mocker.patch("table_extractor.detect.detect_regions", return_value=[parent_region])
    
    child_region = Region(
        id="r0_s0",
        label="Child Table",
        region_type=RegionType.SECTION_GROUPED_TABLE,
        bbox=[10, 10, 990, 990],
        may_contain_subregions=False,
        depth=1
    )
    mock_detect_subregions = mocker.patch("table_extractor.detect.detect_subregions", return_value=[child_region])
    
    # We want refine_region_zoom to return a new region object to ensure we test replacement
    def dummy_refine(img, r, model, force=False):
        return Region(
            id=r.id,
            parent_id=r.parent_id,
            label=f"Refined {r.label}",
            region_type=r.region_type,
            bbox=[x + 5 for x in r.bbox],
            may_contain_subregions=r.may_contain_subregions,
            depth=r.depth
        )
    mock_refine_region_zoom = mocker.patch("table_extractor.detect.refine_region_zoom", side_effect=dummy_refine)
    mocker.patch("table_extractor.ingest.crop_with_padding", return_value=mock_img)
    
    extracted_content = ExtractedContent(
        region_id="r0",
        region_type=RegionType.RULED_TABLE,
        markdown="# Title\n| col1 |\n|---|",
        model_used="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 0.0001}
    )
    mocker.patch("table_extractor.extract.extract_content", return_value=extracted_content)
    mocker.patch("table_extractor.reconcile.resolve_footnotes")
    mocker.patch("table_extractor.reconcile.self_consistency_check")
    mocker.patch("table_extractor.reconcile.to_markdown", return_value="## Mock Markdown Output")
    mocker.patch("table_extractor.render.draw_overlay", return_value=mock_img)
    mocker.patch("table_extractor.detect.get_total_usage", return_value={})
    mocker.patch("table_extractor.extract.get_total_usage", return_value={})

    out_dir = str(tmp_path / "output_zoom")
    run_pipeline(
        image_path="dummy.png",
        out_dir=out_dir,
        detection_model="det-model",
        extraction_model="ext-model",
        verification_model="ver-model",
        refinement_method="zoom"
    )
    
    # Assert refine_region_zoom was called for both parent and child
    assert mock_refine_region_zoom.call_count == 2
    mock_refine_region_zoom.assert_any_call(mock_img, parent_region, "det-model", force=False)
    # The second call is on the child_region returned by detect_subregions
    mock_refine_region_zoom.assert_any_call(mock_img, child_region, "det-model", force=False)
    
    # Check that output JSON has refined coordinates and labels
    with open(os.path.join(out_dir, "regions.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["label"] == "Refined Parent Table"
    assert len(data[0]["children"]) == 1
    assert data[0]["children"][0]["label"] == "Refined Child Table"



