from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_extractor import (
    derive_required_tasks,
    reconcile_tasks,
    on_crop_mutation,
    _is_extraction_in_progress,
    _set_extraction_in_progress,
    _clear_extraction_in_progress,
)


def _meta(pages):
    return {"pages": pages, "extraction_tasks": []}


def test_derive_simple_page_task():
    meta = _meta([{"analysis_status": "done", "classification": "Simple", "crops": []}])
    tasks = derive_required_tasks(meta)
    assert tasks == [{
        "task_id": "page-0", "page_idx": 0, "kind": "page", "image_source": "page",
    }]


def test_derive_complex_crop_tasks_sorted_by_y():
    meta = _meta([{
        "analysis_status": "done", "classification": "Complex",
        "crops": [
            {"filename": "crop_002.png", "bbox": [0, 0.8, 1, 1.0]},
            {"filename": "crop_001.png", "bbox": [0, 0.0, 1, 0.2]},
        ],
    }])
    tasks = derive_required_tasks(meta)
    assert [t["task_id"] for t in tasks] == ["crop_001", "crop_002"]
    assert all(t["kind"] == "crop" for t in tasks)


def test_derive_skips_unanalyzed_pages():
    meta = _meta([{"analysis_status": "pending", "classification": None, "crops": []}])
    assert derive_required_tasks(meta) == []


def test_derive_complex_no_crops_falls_back_to_page_task():
    meta = _meta([{"analysis_status": "done", "classification": "Complex", "crops": []}])
    tasks = derive_required_tasks(meta)
    assert tasks[0]["task_id"] == "page-0"


def test_reconcile_preserves_extracted_status_with_fragment(tmp_path):
    fragments_dir = str(tmp_path / "frag")
    os.makedirs(fragments_dir, exist_ok=True)
    open(os.path.join(fragments_dir, "page-0.html"), "w").write("<p>x</p>")
    meta = _meta([{"analysis_status": "done", "classification": "Simple", "crops": []}])
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "extracted", "extraction_error": None,
        "extraction_error_type": None, "fragment_path": "extraction_fragments/page-0.html",
    }]
    desired = derive_required_tasks(meta)
    reconcile_tasks(meta, desired, fragments_dir)
    assert meta["extraction_tasks"][0]["extraction_status"] == "extracted"


def test_reconcile_resets_extracted_when_fragment_missing(tmp_path):
    fragments_dir = str(tmp_path / "frag")
    os.makedirs(fragments_dir, exist_ok=True)
    meta = _meta([{"analysis_status": "done", "classification": "Simple", "crops": []}])
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "extracted", "extraction_error": None,
        "extraction_error_type": None, "fragment_path": "extraction_fragments/page-0.html",
    }]
    desired = derive_required_tasks(meta)
    reconcile_tasks(meta, desired, fragments_dir)
    assert meta["extraction_tasks"][0]["extraction_status"] == "pending"


def test_reconcile_upgrades_pending_when_fragment_exists(tmp_path):
    fragments_dir = str(tmp_path / "frag")
    os.makedirs(fragments_dir, exist_ok=True)
    open(os.path.join(fragments_dir, "page-0.html"), "w").write("<p>x</p>")
    meta = _meta([{"analysis_status": "done", "classification": "Simple", "crops": []}])
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "pending", "extraction_error": None,
        "extraction_error_type": None, "fragment_path": None,
    }]
    desired = derive_required_tasks(meta)
    reconcile_tasks(meta, desired, fragments_dir)
    assert meta["extraction_tasks"][0]["extraction_status"] == "extracted"


class _FakeSM:
    def __init__(self, fragments_dir):
        self._fragments_dir = fragments_dir

    def get_extraction_fragments_dir(self, session_id):
        return self._fragments_dir


def test_on_crop_mutation_removes_stale_tasks_and_marker(tmp_path):
    fragments_dir = str(tmp_path / "frag")
    os.makedirs(fragments_dir, exist_ok=True)
    open(os.path.join(fragments_dir, "old_crop.html"), "w").write("<p>stale</p>")
    output_dir = str(tmp_path / "out")
    session_dir = os.path.join(output_dir, "sid")
    os.makedirs(session_dir, exist_ok=True)
    marker = os.path.join(session_dir, ".complete")
    open(marker, "w").write("{}")

    sm = _FakeSM(fragments_dir)
    meta = _meta([{"analysis_status": "done", "classification": "Simple", "crops": []}])
    meta["extraction_tasks"] = [{
        "task_id": "old_crop", "page_idx": 0, "kind": "crop",
        "crop_filename": "old_crop.png", "extraction_status": "extracted",
        "extraction_error": None, "extraction_error_type": None,
        "fragment_path": "extraction_fragments/old_crop.html",
    }]
    on_crop_mutation(meta, sm, "sid", output_dir)
    ids = [t["task_id"] for t in meta["extraction_tasks"]]
    assert "old_crop" not in ids
    assert not os.path.exists(marker)
    assert not os.path.exists(os.path.join(fragments_dir, "old_crop.html"))


def test_extraction_in_progress_guard():
    _set_extraction_in_progress("s1")
    assert _is_extraction_in_progress("s1") is True
    _clear_extraction_in_progress("s1")
    assert _is_extraction_in_progress("s1") is False
