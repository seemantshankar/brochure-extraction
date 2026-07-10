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
