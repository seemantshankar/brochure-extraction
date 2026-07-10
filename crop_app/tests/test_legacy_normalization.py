from __future__ import annotations
import os, sys
from unittest.mock import patch
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app import create_app
from session_manager import SessionManager
from table_extractor.html_extractor import (
    normalize_legacy_meta,
    set_output_root,
    _get_active_job,
)


def test_normalize_legacy_classification_becomes_done():
    meta = {"pages": [{"path": "p.png", "classification": "Simple", "crops": []}]}
    out = normalize_legacy_meta(meta)
    assert out["pages"][0]["analysis_status"] == "done"


def test_normalize_missing_extraction_tasks_built():
    meta = {"pages": [{"path": "p.png", "classification": "Simple", "crops": []}]}
    out = normalize_legacy_meta(meta)
    assert "extraction_tasks" in out
    assert out["extraction_tasks"][0]["task_id"] == "page-0"


def test_normalize_next_crop_id_from_existing_crops(tmp_path):
    crop_dir = os.path.join(str(tmp_path), "crops", "sid")
    os.makedirs(crop_dir, exist_ok=True)
    # A legacy session that already has crop_003.png (and a stray crop_001.png)
    from PIL import Image
    Image.new("RGB", (10, 10)).save(os.path.join(crop_dir, "crop_003.png"))
    Image.new("RGB", (10, 10)).save(os.path.join(crop_dir, "crop_001.png"))
    meta = {"pages": [{"path": "p.png", "classification": "Complex", "crops": [
        {"filename": "crop_003.png", "bbox": [0, 0.1, 1, 0.3]},
    ]}]}
    out = normalize_legacy_meta(meta, crop_dir=crop_dir)
    # max numeric id is 3 -> next_crop_id must be 4 (no ID reuse)
    assert out.get("next_crop_id") == 4


def test_normalize_next_crop_id_zero_when_no_crops(tmp_path):
    crop_dir = os.path.join(str(tmp_path), "crops", "sid")
    os.makedirs(crop_dir, exist_ok=True)
    meta = {"pages": [{"path": "p.png", "classification": "Simple", "crops": []}]}
    out = normalize_legacy_meta(meta, crop_dir=crop_dir)
    assert out.get("next_crop_id") == 0


@pytest.fixture
def client_legacy_session(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")
    app.config["EXTRACTED_DIR"] = str(tmp_path / "extracted")
    set_output_root(app.config["EXTRACTED_DIR"])
    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm
    sid = sm.create_session()
    page_dir = sm.get_page_dir(sid)
    # A legacy page with classification but missing analysis_status
    Image.new("RGB", (200, 300), "blue").save(os.path.join(page_dir, "page_000.png"))
    # Save a legacy metadata file
    sm.save_meta(sid, {
        "files": ["test.pdf"],
        "pages": [{
            "path": "page_000.png",
            "classification": "Simple",
            "crops": [],
        }],
    })
    # Also create crop_dir for sid
    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    with app.test_client() as client:
        yield client, sid, sm


def test_post_extract_html_normalizes_legacy_session(client_legacy_session):
    client, sid, sm = client_legacy_session
    # Verify that before POST, page doesn't have analysis_status, extraction_tasks is missing
    meta = sm.load_meta(sid)
    assert "analysis_status" not in meta["pages"][0]
    assert "extraction_tasks" not in meta
    assert "next_crop_id" not in meta

    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"
        # Wait for the job to complete or at least check the metadata
        job = _get_active_job(sid)
        if job:
            job.done_event.wait(timeout=10)

    # Now verify the saved metadata is normalized
    normalized_meta = sm.load_meta(sid)
    assert normalized_meta["pages"][0]["analysis_status"] == "done"
    assert "extraction_tasks" in normalized_meta
    assert normalized_meta["extraction_tasks"][0]["task_id"] == "page-0"
    assert normalized_meta["next_crop_id"] == 0


def test_commit_normalizes_legacy_session(client_legacy_session):
    client, sid, sm = client_legacy_session
    # Prior to commit, metadata is legacy
    meta = sm.load_meta(sid)
    assert "next_crop_id" not in meta
    assert "extraction_tasks" not in meta
    
    # Post to commit a crop
    resp = client.post(f"/commit/{sid}", json={
        "page_index": 0,
        "crops": [
            {"bbox": [0.1, 0.1, 0.5, 0.5]}
        ]
    })
    assert resp.status_code == 200
    
    # Verify that metadata has been normalized and saved
    normalized_meta = sm.load_meta(sid)
    assert normalized_meta["pages"][0]["analysis_status"] == "done"
    assert "extraction_tasks" in normalized_meta
    # The committed crop becomes part of the crops, wait, because it had next_crop_id normalized to 0
    # and then incremented, the new next_crop_id must be 1.
    assert normalized_meta["next_crop_id"] == 1
    assert len(normalized_meta["pages"][0]["crops"]) == 1
