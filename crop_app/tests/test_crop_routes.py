import os
import pytest
import sys
import json
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager


@pytest.fixture
def client_with_session(tmp_path):
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

    sm.save_meta(sid, {
        "pages": [{"path": "page_000.png", "classification": "Complex", "crops": []}]
    })

    with app.test_client() as client:
        yield client, sid


def test_commit_creates_crops(client_with_session):
    client, sid = client_with_session
    bboxes = [{"bbox": [0.1, 0.1, 0.4, 0.4]}, {"bbox": [0.5, 0.5, 0.9, 0.9]}]
    resp = client.post(
        f"/commit/{sid}",
        data=json.dumps({"page_index": 0, "crops": bboxes}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["crops"]) == 2
    crop_root = client.application.config["CROP_DIR"]
    for c in data["crops"]:
        assert os.path.exists(os.path.join(crop_root, sid, c["filename"]))


def test_commit_updates_meta(client_with_session):
    client, sid = client_with_session
    bboxes = [{"bbox": [0.0, 0.0, 0.5, 0.5]}]
    client.post(
        f"/commit/{sid}",
        data=json.dumps({"page_index": 0, "crops": bboxes}),
        content_type="application/json",
    )
    resp = client.get(f"/session/{sid}")
    meta = resp.get_json()
    assert len(meta["pages"][0]["crops"]) == 1


def test_trim_endpoint(client_with_session):
    client, sid = client_with_session
    bboxes = [{"bbox": [0.0, 0.0, 1.0, 1.0]}]
    commit_resp = client.post(
        f"/commit/{sid}",
        data=json.dumps({"page_index": 0, "crops": bboxes}),
        content_type="application/json",
    )
    crop_filename = commit_resp.get_json()["crops"][0]["filename"]
    crop_path = os.path.join(client.application.config["CROP_DIR"], sid, crop_filename)

    img_before = Image.open(crop_path)
    assert img_before.size == (200, 300)

    trim_bbox = [0.25, 0.25, 0.75, 0.75]
    resp = client.post(
        f"/trim/{sid}/{crop_filename}",
        data=json.dumps({"bbox": trim_bbox}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    img_after = Image.open(crop_path)
    assert img_after.size == (100, 150)


def test_serve_crop_image(client_with_session):
    client, sid = client_with_session
    bboxes = [{"bbox": [0.0, 0.0, 0.5, 0.5]}]
    commit_resp = client.post(
        f"/commit/{sid}",
        data=json.dumps({"page_index": 0, "crops": bboxes}),
        content_type="application/json",
    )
    crop_filename = commit_resp.get_json()["crops"][0]["filename"]
    resp = client.get(f"/crops/{sid}/{crop_filename}")
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/")


def test_save_draft_persists_boxes(client_with_session):
    client, sid = client_with_session
    boxes = [
        {"x0": 0.1, "y0": 0.1, "x1": 0.3, "y1": 0.3},
        {"x0": 0.5, "y0": 0.5, "x1": 0.7, "y1": 0.7},
    ]
    resp = client.post(
        f"/save-draft/{sid}",
        data=json.dumps({"page_index": 0, "boxes": boxes}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    meta = client.application.session_manager.load_meta(sid)
    assert meta["pages"][0]["draft"] == boxes


def test_clear_draft_removes_draft(client_with_session):
    client, sid = client_with_session
    boxes = [{"x0": 0.1, "y0": 0.1, "x1": 0.3, "y1": 0.3}]
    client.post(
        f"/save-draft/{sid}",
        data=json.dumps({"page_index": 0, "boxes": boxes}),
        content_type="application/json",
    )

    resp = client.post(
        f"/clear-draft/{sid}",
        data=json.dumps({"page_index": 0}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    meta = client.application.session_manager.load_meta(sid)
    assert "draft" not in meta["pages"][0]


def test_commit_clears_draft(client_with_session):
    client, sid = client_with_session
    boxes = [{"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}]
    client.post(
        f"/save-draft/{sid}",
        data=json.dumps({"page_index": 0, "boxes": boxes}),
        content_type="application/json",
    )

    commit_resp = client.post(
        f"/commit/{sid}",
        data=json.dumps({"page_index": 0, "crops": [{"bbox": [0.1, 0.1, 0.5, 0.5]}]}),
        content_type="application/json",
    )
    assert commit_resp.status_code == 200

    meta = client.application.session_manager.load_meta(sid)
    assert "draft" not in meta["pages"][0]
    assert len(meta["pages"][0]["crops"]) == 1
    assert meta["pages"][0]["crops"][0]["filename"] == "crop_000.png"


def test_delete_crop_removes_meta_and_file(client_with_session):
    client, sid = client_with_session
    bboxes = [{"bbox": [0.0, 0.0, 0.5, 0.5]}, {"bbox": [0.5, 0.5, 1.0, 1.0]}]
    commit_resp = client.post(
        f"/commit/{sid}",
        data=json.dumps({"page_index": 0, "crops": bboxes}),
        content_type="application/json",
    )
    assert commit_resp.status_code == 200
    commit_data = commit_resp.get_json()
    assert len(commit_data["crops"]) == 2
    crop_root = client.application.config["CROP_DIR"]
    crop_filename = commit_data["crops"][0]["filename"]
    crop_path = os.path.join(crop_root, sid, crop_filename)
    assert os.path.exists(crop_path)

    delete_resp = client.post(
        f"/delete-crop/{sid}",
        data=json.dumps({"page_index": 0, "filename": crop_filename}),
        content_type="application/json",
    )
    assert delete_resp.status_code == 200
    assert delete_resp.get_json()["removed"] == 1
    assert not os.path.exists(crop_path)

    meta = client.application.session_manager.load_meta(sid)
    assert len(meta["pages"][0]["crops"]) == 1
    assert meta["pages"][0]["crops"][0]["filename"] != crop_filename


def test_trim_rejects_unrecorded_crop_or_traversal(client_with_session):
    client, sid = client_with_session
    
    # 1. Unrecorded crop
    resp = client.post(
        f"/trim/{sid}/unrecorded_crop.png",
        data=json.dumps({"bbox": [0, 0, 1, 1]}),
        content_type="application/json",
    )
    assert resp.status_code == 404
    
    # 2. Path traversal attempt
    resp = client.post(
        f"/trim/{sid}/../../stray.png",
        data=json.dumps({"bbox": [0, 0, 1, 1]}),
        content_type="application/json",
    )
    assert resp.status_code == 404  # Rejects because it's not a recorded crop first


def test_delete_crop_rejects_unrecorded_crop_or_traversal(client_with_session):
    client, sid = client_with_session
    
    # 1. Unrecorded crop
    resp = client.post(
        f"/delete-crop/{sid}",
        data=json.dumps({"page_index": 0, "filename": "unrecorded_crop.png"}),
        content_type="application/json",
    )
    assert resp.status_code == 404
    
    # 2. Path traversal attempt
    resp = client.post(
        f"/delete-crop/{sid}",
        data=json.dumps({"page_index": 0, "filename": "../../stray.png"}),
        content_type="application/json",
    )
    assert resp.status_code == 404  # Rejects because it's not in page crops


def test_delete_crop_realpath_containment_checks(client_with_session):
    client, sid = client_with_session
    meta = client.application.session_manager.load_meta(sid)
    meta["pages"][0]["crops"] = [{"filename": "../../passwd", "bbox": [0, 0, 1, 1]}]
    client.application.session_manager.save_meta_atomic(sid, meta)
    
    # Delete crop should return 403 Forbidden because path points outside crop dir
    resp2 = client.post(
        f"/delete-crop/{sid}",
        data=json.dumps({"page_index": 0, "filename": "../../passwd"}),
        content_type="application/json",
    )
    assert resp2.status_code == 403



