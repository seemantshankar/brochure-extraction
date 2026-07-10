from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_extractor import (
    on_crop_mutation,
    _clear_extraction_in_progress,
)


class _FakeSM:
    def __init__(self, base):
        self.base = base

    def get_extraction_fragments_dir(self, sid):
        d = os.path.join(self.base, sid, "extraction_fragments")
        os.makedirs(d, exist_ok=True)
        return d


def test_first_crop_task_shape_transition(tmp_path):
    sid = "s1"
    _clear_extraction_in_progress(sid)
    sm = _FakeSM(str(tmp_path))
    meta = {
        "files": ["t.pdf"],
        "pages": [{"analysis_status": "done", "classification": "Complex", "crops": []}],
        "extraction_tasks": [],
    }
    fdir = sm.get_extraction_fragments_dir(sid)
    with open(os.path.join(fdir, "page-0.html"), "w") as f:
        f.write("<p>whole</p>")
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "extracted", "extraction_error": None,
        "extraction_error_type": None, "fragment_path": "extraction_fragments/page-0.html",
    }]
    meta["pages"][0]["crops"] = [{"filename": "crop_003.png", "bbox": [0, 0.1, 1, 0.3]}]
    on_crop_mutation(meta, sm, sid, str(tmp_path / "out"))
    ids = [t["task_id"] for t in meta["extraction_tasks"]]
    assert "page-0" not in ids
    assert "crop_003" in ids
    crop_task = [t for t in meta["extraction_tasks"] if t["task_id"] == "crop_003"][0]
    assert crop_task["extraction_status"] == "pending"
    assert not os.path.exists(os.path.join(fdir, "page-0.html"))


def test_last_crop_task_shape_transition(tmp_path):
    sid = "s2"
    _clear_extraction_in_progress(sid)
    sm = _FakeSM(str(tmp_path))
    meta = {
        "files": ["t.pdf"],
        "pages": [{"analysis_status": "done", "classification": "Complex",
                   "crops": [{"filename": "crop_003.png", "bbox": [0, 0.1, 1, 0.3]}]}],
        "extraction_tasks": [],
    }
    fdir = sm.get_extraction_fragments_dir(sid)
    with open(os.path.join(fdir, "crop_003.html"), "w") as f:
        f.write("<p>crop</p>")
    meta["extraction_tasks"] = [{
        "task_id": "crop_003", "page_idx": 0, "kind": "crop",
        "crop_filename": "crop_003.png", "extraction_status": "extracted",
        "extraction_error": None, "extraction_error_type": None,
        "fragment_path": "extraction_fragments/crop_003.html",
    }]
    meta["pages"][0]["crops"] = []
    on_crop_mutation(meta, sm, sid, str(tmp_path / "out"))
    ids = [t["task_id"] for t in meta["extraction_tasks"]]
    assert "crop_003" not in ids
    assert "page-0" in ids
    page_task = [t for t in meta["extraction_tasks"] if t["task_id"] == "page-0"][0]
    assert page_task["extraction_status"] == "pending"
    assert not os.path.exists(os.path.join(fdir, "crop_003.html"))
