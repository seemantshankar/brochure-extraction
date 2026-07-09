import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def isolated_client(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")
    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm
    with app.test_client() as client:
        yield app, client


def test_health_returns_200(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_index_returns_200(client):
    response = client.get("/")
    assert response.status_code == 200


def test_sessions_page_returns_200(client):
    response = client.get("/sessions")
    assert response.status_code == 200
    assert b"Brochures" in response.data


def test_sessions_page_empty_when_no_sessions(isolated_client):
    app, client = isolated_client
    response = client.get("/sessions")
    assert response.status_code == 200
    data = response.data.decode("utf-8")
    assert "No brochures uploaded yet" in data


def test_sessions_page_lists_existing_sessions(isolated_client):
    app, client = isolated_client
    sid = app.session_manager.create_session()
    app.session_manager.save_meta(sid, {
        "files": ["brochure.pdf"],
        "pages": [
            {"path": "page_000.png", "classification": "Complex", "crops": []}
        ],
    })

    resp = client.get("/sessions")
    assert resp.status_code == 200
    assert b"brochure.pdf" in resp.data
    assert b"1" in resp.data

def test_delete_session_removes_uploads_and_crops(isolated_client):
    app, client = isolated_client
    sid = app.session_manager.create_session()
    app.session_manager.save_meta(sid, {
        'files': ['to_delete.pdf'],
        'pages': [
            {'path': 'page_000.png', 'classification': 'Complex', 'crops': []}
        ],
    })

    upload_dir = app.config['UPLOAD_DIR']
    crop_dir = app.config['CROP_DIR']
    assert os.path.isdir(os.path.join(upload_dir, sid))
    os.makedirs(os.path.join(crop_dir, sid), exist_ok=True)
    assert os.path.isdir(os.path.join(crop_dir, sid))

    resp = client.delete(f'/sessions/{sid}')
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True

    assert not os.path.exists(os.path.join(upload_dir, sid))
    assert not os.path.exists(os.path.join(crop_dir, sid))


def test_delete_missing_session_returns_404(client):
    resp = client.delete('/sessions/does-not-exist')
    assert resp.status_code == 404


def test_save_page_endpoint(isolated_client, tmp_path):
    app, client = isolated_client

    # Route writes to app.config["EXTRACTED_DIR"] when set, so keep it hermetic.
    extracted_dir = tmp_path / "extracted"
    app.config["EXTRACTED_DIR"] = str(extracted_dir)

    sid = app.session_manager.create_session()
    app.session_manager.save_meta(sid, {
        "files": ["brochure.pdf"],
        "pages": [
            {"path": "page_000.png", "classification": "Simple", "crops": []}
        ],
    })

    session_dir = extracted_dir / sid
    os.makedirs(session_dir, exist_ok=True)
    with open(os.path.join(session_dir, "page-0.html"), "w", encoding="utf-8") as f:
        f.write("<p>original</p>")

    resp = client.post(
        f"/save-page/{sid}/0",
        data="<p>edited</p>",
        content_type="text/html",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}

    with open(os.path.join(session_dir, "page-0.html"), "r", encoding="utf-8") as f:
        assert f.read() == "<p>edited</p>"

