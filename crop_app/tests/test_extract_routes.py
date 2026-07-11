from __future__ import annotations
import os
import sys
import pytest
from PIL import Image
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager
from table_extractor.html_extractor import (
    set_output_root,
    _get_active_job,
)


@pytest.fixture
def client_ready_session(tmp_path):
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
    Image.new("RGB", (200, 300), "blue").save(os.path.join(page_dir, "page_000.png"))
    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    Image.new("RGB", (50, 50), "red").save(os.path.join(crop_dir, "crop_000.png"))
    sm.save_meta(sid, {
        "files": ["test.pdf"],
        "pages": [{
            "path": "page_000.png",
            "analysis_status": "done",
            "classification": "Complex",
            "crops": [{"path": "crop_000.png", "filename": "crop_000.png", "bbox": [0.1, 0.1, 0.5, 0.5]}],
        }],
    })
    with app.test_client() as client:
        yield client, sid, sm


def test_extract_html_progress_page_renders(client_ready_session):
    client, sid, sm = client_ready_session
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 200
    assert b"Initializing extraction" in resp.data


def test_extract_html_block_when_draft_present(client_ready_session):
    client, sid, sm = client_ready_session
    meta = sm.load_meta(sid)
    meta["pages"][0]["draft"] = [{"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}]
    sm.save_meta_atomic(sid, meta)
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert b"uncommitted changes" in resp.data


def test_extract_html_block_when_analysis_incomplete(client_ready_session):
    client, sid, sm = client_ready_session
    meta = sm.load_meta(sid)
    meta["pages"][0]["analysis_status"] = "pending"
    meta["pages"][0]["classification"] = None
    sm.save_meta_atomic(sid, meta)
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert b"analyze all pages" in resp.data


def test_post_extract_html_starts_job(client_ready_session):
    client, sid, sm = client_ready_session
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"
    job = _get_active_job(sid)
    if job:
        job.done_event.wait(timeout=10)


def test_extract_progress_sse_streams_terminal_from_disk(client_ready_session):
    client, sid, sm = client_ready_session
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        job = _get_active_job(sid)
        job.done_event.wait(timeout=10)
    resp = client.get(f"/extract-progress/{sid}")
    assert resp.status_code == 200
    assert "done" in resp.data.decode()


def test_extracted_html_serving_requires_complete_marker(client_ready_session):
    client, sid, sm = client_ready_session
    resp = client.get(f"/extracted/{sid}/extraction.html")
    assert resp.status_code == 404


def _mark_extraction_complete(app, sid):
    output = os.path.join(app.config["EXTRACTED_DIR"], sid)
    os.makedirs(output, exist_ok=True)
    open(os.path.join(output, ".complete"), "w", encoding="utf-8").close()


def test_review_workspace_requires_completed_extraction(client_ready_session):
    client, sid, _ = client_ready_session
    assert client.get(f"/review/{sid}").status_code == 404


def test_review_workspace_renders_current_page(client_ready_session):
    client, sid, _ = client_ready_session
    _mark_extraction_complete(client.application, sid)
    response = client.get(f"/review/{sid}?page=0")
    assert response.status_code == 200
    assert b"review-canvas" in response.data
    assert b'"initialPage": 0' in response.data


def test_review_workspace_rejects_out_of_range_page(client_ready_session):
    client, sid, _ = client_ready_session
    _mark_extraction_complete(client.application, sid)
    assert client.get(f"/review/{sid}?page=1").status_code == 404
