import os
import pytest
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from session_manager import SessionManager


@pytest.fixture
def manager(tmp_path):
    return SessionManager(str(tmp_path / "uploads"), str(tmp_path / "crops"))


def test_create_session_returns_uuid(manager):
    sid = manager.create_session()
    assert len(sid) == 36  # UUID format


def test_create_session_creates_dirs(manager):
    sid = manager.create_session()
    session_dir = os.path.join(manager.upload_dir, sid)
    assert os.path.isdir(session_dir)
    assert os.path.isdir(os.path.join(session_dir, "pages"))
    assert os.path.isdir(os.path.join(session_dir, "original"))


def test_save_and_load_meta(manager):
    sid = manager.create_session()
    data = {"pages": [{"path": "p0.png", "complex": True, "labels": ["table"]}]}
    manager.save_meta(sid, data)
    loaded = manager.load_meta(sid)
    assert loaded == data


def test_load_meta_returns_none_if_missing(manager):
    assert manager.load_meta("nonexistent") is None


def test_get_page_dir(manager):
    sid = manager.create_session()
    page_dir = manager.get_page_dir(sid)
    assert page_dir.endswith("pages")
    assert os.path.isdir(page_dir)


def test_get_crop_dir(manager):
    sid = manager.create_session()
    crop_dir = manager.get_crop_dir(sid)
    assert crop_dir.endswith(sid)
    assert os.path.isdir(crop_dir)
