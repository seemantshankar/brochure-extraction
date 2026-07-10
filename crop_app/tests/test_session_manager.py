import os
import pytest
import sys
import json
import threading
import tempfile

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
    data = {"pages": [{"path": "p0.png", "classification": "Complex"}]}
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


def test_save_meta_atomic_creates_no_tmp_leftover(manager):
    sid = manager.create_session()
    data = {"pages": []}
    with manager.metadata_lock(sid):
        manager.save_meta_atomic(sid, data)
    session_dir = os.path.join(manager.upload_dir, sid)
    leftovers = [f for f in os.listdir(session_dir) if f.endswith(".json.tmp")]
    assert leftovers == []
    assert manager.load_meta(sid) == data


def test_metadata_lock_isolation_between_sessions(manager):
    a = manager.metadata_lock("aaa")
    b = manager.metadata_lock("bbb")
    assert a is not b


def test_metadata_lock_returns_same_object_per_session(manager):
    a = manager.metadata_lock("aaa")
    b = manager.metadata_lock("aaa")
    assert a is b


def test_concurrent_writes_under_lock_are_consistent(manager):
    sid = manager.create_session()
    with manager.metadata_lock(sid):
        manager.save_meta_atomic(sid, {"counter": 0, "pages": []})

    def worker():
        for _ in range(50):
            with manager.metadata_lock(sid):
                meta = manager.load_meta(sid)
                meta["counter"] += 1
                manager.save_meta_atomic(sid, meta)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert manager.load_meta(sid)["counter"] == 200


def test_get_extraction_fragments_dir(manager):
    sid = manager.create_session()
    frag_dir = manager.get_extraction_fragments_dir(sid)
    assert frag_dir.endswith(os.path.join(sid, "extraction_fragments"))
    assert os.path.isdir(frag_dir)
