import os
import sys
from unittest.mock import patch

from PIL import Image
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app


@pytest.fixture
def app_with_session(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.config["CROP_DIR"], exist_ok=True)

    from session_manager import SessionManager
    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm

    sid = sm.create_session()
    page_dir = sm.get_page_dir(sid)
    img_path = os.path.join(page_dir, "page_000.png")
    Image.new("RGB", (200, 300), "white").save(img_path)

    sm.save_meta(sid, {
        "pages": [{
            "path": "page_000.png",
            "complex": None,
            "labels": [],
            "crops": [],
            "pdf_path": None,
            "pdf_page": None,
        }]
    })

    with app.test_client() as client:
        yield client, sid


@pytest.fixture
def app_with_pdf_session(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as rc
    from session_manager import SessionManager

    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.config["CROP_DIR"], exist_ok=True)

    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm

    sid = sm.create_session()
    session_dir = sm.get_session_dir(sid)
    page_dir = sm.get_page_dir(sid)
    original_dir = sm.get_original_dir(sid)

    pdf_path = os.path.join(original_dir, "test.pdf")
    c = rc.Canvas(pdf_path, pagesize=letter)
    c.drawString(100, 700, "Page 1")
    c.showPage()
    c.drawString(100, 700, "Page 2")
    c.save()

    img_path = os.path.join(page_dir, "page_000.png")
    Image.new("RGB", (1275, 1650), "white").save(img_path)

    sm.save_meta(sid, {
        "pages": [{
            "path": "page_000.png",
            "complex": None,
            "labels": [],
            "crops": [],
            "pdf_path": os.path.relpath(pdf_path, session_dir),
            "pdf_page": 0,
        }]
    })

    with app.test_client() as client:
        yield client, sid


def test_analyze_endpoint_returns_updated_meta(app_with_session):
    client, sid = app_with_session

    mock_result = {"complex": True, "labels": ["table"], "error": None}
    with patch("app.analyze_page", return_value=mock_result):
        resp = client.post(f"/analyze/{sid}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["pages"][0]["complex"] is True
    assert "table" in data["pages"][0]["labels"]


def test_analyze_invalid_session(app_with_session):
    client, _ = app_with_session
    resp = client.post("/analyze/nonexistent-uuid")
    assert resp.status_code == 404


def test_serve_page_image(app_with_session):
    client, sid = app_with_session
    resp = client.get(f"/pages/{sid}/page_000.png")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/")


def test_get_session_meta(app_with_session):
    client, sid = app_with_session
    resp = client.get(f"/session/{sid}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "pages" in data
    assert len(data["pages"]) == 1


def test_complex_non_pdf_page_does_not_trigger_upgrade(app_with_session):
    client, sid = app_with_session

    mock_result = {"complex": True, "labels": ["table"], "error": None}
    with patch("app.analyze_page", return_value=mock_result), \
         patch("app.upgrade_page_to_hires") as mock_upgrade:
        resp = client.post(f"/analyze/{sid}")

    mock_upgrade.assert_not_called()
    data = resp.get_json()
    assert data["pages"][0]["complex"] is True
    assert "upgraded" not in data["pages"][0]


def test_simple_pdf_page_does_not_trigger_upgrade(app_with_pdf_session):
    client, sid = app_with_pdf_session

    mock_result = {"complex": False, "labels": [], "error": None}
    with patch("app.analyze_page", return_value=mock_result), \
         patch("app.upgrade_page_to_hires") as mock_upgrade:
        resp = client.post(f"/analyze/{sid}")

    mock_upgrade.assert_not_called()
    data = resp.get_json()
    assert data["pages"][0]["complex"] is False


def test_complex_pdf_page_triggers_upgrade(app_with_pdf_session):
    client, sid = app_with_pdf_session

    mock_result = {"complex": True, "labels": ["table"], "error": None}
    with patch("app.analyze_page", return_value=mock_result), \
         patch("app.upgrade_page_to_hires") as mock_upgrade:
        mock_upgrade.return_value = "/fake/page_000.png"
        resp = client.post(f"/analyze/{sid}")

    mock_upgrade.assert_called_once()
    call_args = mock_upgrade.call_args
    assert call_args[0][2] == 0

    data = resp.get_json()
    assert data["pages"][0]["complex"] is True
    assert data["pages"][0]["upgraded"] is True


def test_upgrade_failure_records_error(app_with_pdf_session):
    client, sid = app_with_pdf_session

    mock_result = {"complex": True, "labels": ["table"], "error": None}
    with patch("app.analyze_page", return_value=mock_result), \
         patch("app.upgrade_page_to_hires", side_effect=RuntimeError("poppler crashed")):
        resp = client.post(f"/analyze/{sid}")

    data = resp.get_json()
    assert data["pages"][0]["complex"] is True
    assert data["pages"][0]["upgrade_error"] == "poppler crashed"
