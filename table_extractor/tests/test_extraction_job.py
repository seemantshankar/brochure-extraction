from __future__ import annotations
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import patch
from PIL import Image

from table_extractor.html_extractor import (
    ExtractionJob,
    _start_extraction_job,
    _get_active_job,
    _cleanup_completed_jobs,
    _output_complete,
    derive_required_tasks,
    reconcile_tasks,
)


class _FakeSM:
    def __init__(self, base):
        self.base = base
        self._store = {}
        self._locks = {}

    def get_session_dir(self, sid):
        return os.path.join(self.base, sid)

    def get_page_dir(self, sid):
        return os.path.join(self.base, sid, "pages")

    def get_extraction_fragments_dir(self, sid):
        d = os.path.join(self.base, sid, "extraction_fragments")
        os.makedirs(d, exist_ok=True)
        return d

    def load_meta(self, sid):
        return self._store[sid]

    def save_meta_atomic(self, sid, meta):
        self._store[sid] = meta

    def metadata_lock(self, sid):
        import threading
        if sid not in self._locks:
            self._locks[sid] = threading.Lock()
        return self._locks[sid]


def _make_session(base, sid, pages, analyzed=True):
    session_dir = os.path.join(base, sid)
    os.makedirs(os.path.join(session_dir, "pages"), exist_ok=True)
    meta = {"files": ["t.pdf"], "pages": pages, "extraction_tasks": []}
    if analyzed:
        for p in pages:
            p.setdefault("analysis_status", "done")
    sm = _FakeSM(base)
    sm._store[sid] = meta
    return sm, meta


def test_job_extracts_all_and_writes_complete(tmp_path):
    sid = "job1"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    # The job writes .complete under the global output root; point it at the test dir.
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>frag</p>"):
        job = _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                    str(tmp_path / sid / "pages"),
                                    str(tmp_path / "out"), "m")
        import time
        job.done_event.wait(timeout=10)
    assert _output_complete(sid) is True
    out_dir = os.path.join(str(tmp_path / "out"), sid)
    assert os.path.exists(os.path.join(out_dir, "page-0.html"))
    assert os.path.exists(os.path.join(out_dir, "index.html"))


def test_job_resumes_skipping_extracted(tmp_path):
    sid = "job2"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    # Pre-write a fragment + mark task extracted
    frag = os.path.join(sm.get_extraction_fragments_dir(sid), "page-0.html")
    open(frag, "w").write("<p>already</p>")
    desired = derive_required_tasks(meta)
    reconcile_tasks(meta, desired, sm.get_extraction_fragments_dir(sid))
    meta["extraction_tasks"][0]["extraction_status"] = "extracted"
    meta["extraction_tasks"][0]["fragment_path"] = "extraction_fragments/page-0.html"
    sm.save_meta_atomic(sid, meta)

    calls = {"n": 0}

    def fake(img, model):
        calls["n"] += 1
        return "<p>new</p>"

    with patch("table_extractor.html_extractor.extract_crop_as_html", fake):
        job = _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                    str(tmp_path / sid / "pages"),
                                    str(tmp_path / "out"), "m")
        import time
        job.done_event.wait(timeout=10)
    assert calls["n"] == 0  # skipped because already extracted
    out_dir = os.path.join(str(tmp_path / "out"), sid)
    with open(os.path.join(out_dir, "page-0.html")) as f:
        assert "already" in f.read()


def test_duplicate_job_rejected(tmp_path):
    sid = "job3"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                              str(tmp_path / sid / "pages"),
                              str(tmp_path / "out"), "m")
        try:
            _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                  str(tmp_path / sid / "pages"),
                                  str(tmp_path / "out"), "m")
            pytest.fail("expected RuntimeError")
        except RuntimeError as e:
            assert "already running" in str(e)
    import time
    j = _get_active_job(sid)
    j.done_event.wait(timeout=10)


def test_cleanup_completed_jobs_removes_registry_entry(tmp_path):
    sid = "job4"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        job = _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                    str(tmp_path / sid / "pages"),
                                    str(tmp_path / "out"), "m")
        import time
        job.done_event.wait(timeout=10)
        _cleanup_completed_jobs()
        assert _get_active_job(sid) is None
