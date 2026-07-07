import os
import sys
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager


@pytest.fixture
def client_ready_session(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")

    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm
    sid = sm.create_session()

    page_dir = sm.get_page_dir(sid)
    img = Image.new("RGB", (200, 300), "blue")
    img.save(os.path.join(page_dir, "page_000.png"))

    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    crop = Image.new("RGB", (50, 50), "red")
    crop.save(os.path.join(crop_dir, "crop_000.png"))

    sm.save_meta(sid, {
        "files": ["test.pdf"],
        "pages": [{
            "path": "page_000.png",
            "classification": "Complex",
            "crops": [{"path": "crop_000.png", "filename": "crop_000.png", "bbox": [0.1, 0.1, 0.5, 0.5]}],
        }],
    })

    with app.test_client() as client:
        yield client, sid


def test_extract_html_progress_page_renders(client_ready_session):
    client, sid = client_ready_session
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 200
    assert b"Initializing extraction" in resp.data


def test_extract_html_block_when_draft_present(client_ready_session):
    client, sid = client_ready_session
    sm = client.application.session_manager
    meta = sm.load_meta(sid)
    meta["pages"][0]["draft"] = [{"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}]
    sm.save_meta(sid, meta)

    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert b"uncommitted changes" in resp.data


def test_extract_progress_sse_streams_starting(client_ready_session):
    client, sid = client_ready_session

    with pytest.MonkeyPatch.context() as m:
        import table_extractor.html_extractor as hx
        # Mock run_extraction as an iterable generator of event dicts
        m.setattr(
            hx,
            "run_extraction",
            lambda **kw: iter([
                {"status": "starting"},
                {"status": "progress", "page": 0, "totalPages": 1, "log": "Processing..."},
                {"status": "done", "html": "<html><body>ok</body></html>"},
            ]),
        )

        resp = client.get(f"/extract-progress/{sid}")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")

        data = b"".join(resp.iter_encoded()).decode()
        assert "starting" in data
        assert "done" in data


def test_extracted_html_serving_requires_file(client_ready_session):
    client, sid = client_ready_session
    resp = client.get(f"/extracted/{sid}/extraction.html")
    assert resp.status_code == 404
