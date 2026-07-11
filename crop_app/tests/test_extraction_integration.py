from __future__ import annotations
import os
import sys
import time
import pytest
from PIL import Image
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager
from table_extractor.html_extractor import (
    _get_active_job,
    _cleanup_completed_jobs,
    set_output_root,
    _clear_extraction_in_progress,
)


@pytest.fixture
def app_client(tmp_path):
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
    Image.new("RGB", (60, 60), "blue").save(os.path.join(page_dir, "page_000.png"))
    sm.save_meta_atomic(sid, {
        "files": ["test.pdf"],
        "pages": [{
            "path": "page_000.png",
            "analysis_status": "done",
            "classification": "Simple",
            "crops": [],
        }],
        "extraction_tasks": [],
    })
    yield app.test_client(), sid, sm
    _clear_extraction_in_progress(sid)


def _mark_analyzed(sm, sid):
    meta = sm.load_meta(sid)
    for p in meta["pages"]:
        p["analysis_status"] = "done"
        p["classification"] = "Simple"
    sm.save_meta_atomic(sid, meta)


def test_no_automatic_auth_retry(app_client):
    client, sid, sm = app_client
    meta = sm.load_meta(sid)
    meta["pages"][0]["analysis_status"] = "done"
    meta["pages"][0]["classification"] = "Simple"
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "failed", "extraction_error": "bad key",
        "extraction_error_type": "auth", "fragment_path": None,
    }]
    sm.save_meta_atomic(sid, meta)

    resp = client.post(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert resp.get_json()["error_type"] == "auth"
    assert _get_active_job(sid) is None

    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp2 = client.post(f"/extract-html/{sid}?retry_nonretryable=true")
        assert resp2.status_code == 200
        assert resp2.get_json()["status"] == "started"
        job = _get_active_job(sid)
        assert job is not None
        job.done_event.wait(timeout=10)


def test_mutation_rejected_during_active_job(app_client):
    client, sid, sm = app_client
    _mark_analyzed(sm, sid)
    app = client.application
    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    Image.new("RGB", (20, 20)).save(os.path.join(crop_dir, "crop_000.png"))
    meta = sm.load_meta(sid)
    meta["pages"][0]["crops"] = [{"filename": "crop_000.png", "bbox": [0, 0.1, 1, 0.3]}]
    sm.save_meta_atomic(sid, meta)

    def slow(img, model):
        time.sleep(1.0)
        return "<p>x</p>"

    with patch("table_extractor.html_extractor.extract_crop_as_html", slow):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        resp2 = client.post(f"/trim/{sid}/crop_000.png", json={"bbox": [0, 0.1, 1, 0.3]})
        assert resp2.status_code == 409
    job = _get_active_job(sid)
    if job:
        job.done_event.wait(timeout=10)


def test_terminal_sse_after_cleanup(app_client):
    client, sid, sm = app_client
    _mark_analyzed(sm, sid)
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        job = _get_active_job(sid)
        job.done_event.wait(timeout=10)
    _cleanup_completed_jobs()
    assert _get_active_job(sid) is None
    resp = client.get(f"/extract-progress/{sid}")
    assert resp.status_code == 200
    assert "done" in resp.data.decode()


def test_delete_session_rejected_during_active_job(app_client):
    client, sid, sm = app_client
    _mark_analyzed(sm, sid)
    
    # Run a slow job
    def slow(img, model):
        time.sleep(1.0)
        return "<p>x</p>"
        
    with patch("table_extractor.html_extractor.extract_crop_as_html", slow):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        
        # Verify that DELETE /sessions/<sid> returns 409 Conflict
        del_resp = client.delete(f"/sessions/{sid}")
        assert del_resp.status_code == 409
        assert b"Cannot delete session with active extraction" in del_resp.data

    job = _get_active_job(sid)
    if job:
        job.done_event.wait(timeout=10)


def test_starting_extraction_deletes_stale_complete_marker(app_client):
    client, sid, sm = app_client
    _mark_analyzed(sm, sid)
    
    # Pre-write a stale complete marker
    app = client.application
    session_out_dir = os.path.join(app.config["EXTRACTED_DIR"], sid)
    os.makedirs(session_out_dir, exist_ok=True)
    complete_marker = os.path.join(session_out_dir, ".complete")
    with open(complete_marker, "w") as f:
        f.write('{"timestamp": 1234.5}')
    assert os.path.exists(complete_marker)
    
    # Start extraction: should detect that there are pending tasks (since page-0 needs extraction)
    # and immediately remove the complete marker under lock. Use a slow mock to keep the job active.
    def slow_extract(*args, **kwargs):
        time.sleep(0.5)
        return "<p>x</p>"

    with patch("table_extractor.html_extractor.extract_crop_as_html", slow_extract):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        
        # Wait a tiny bit for the background thread to run its setup and delete the marker
        time.sleep(0.1)
        
        # Check that complete marker is gone immediately
        assert not os.path.exists(complete_marker)
        
        job = _get_active_job(sid)
        if job:
            job.done_event.wait(timeout=10)
            
    # And after successful job completion, it writes a new complete marker
    assert os.path.exists(complete_marker)

