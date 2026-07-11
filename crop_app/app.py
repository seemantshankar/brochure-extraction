"""Flask web application for brochure upload, cropping, analysis, and HTML extraction."""
from __future__ import annotations
import os
import sys
import shutil
import logging
import datetime
from flask import Flask, request, jsonify, redirect, send_file, render_template, Response

logger = logging.getLogger(__name__)

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

env_path = os.path.join(_project_root, ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)

from werkzeug.utils import secure_filename  # noqa: E402
from session_manager import SessionManager  # noqa: E402
from crop_manager import CropManager  # noqa: E402
from pdf_converter import pdf_to_pages, upgrade_page_to_hires  # noqa: E402
from llm import analyze_page  # noqa: E402

UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

_EMBED_CSS_INJECT = """\
.embedded-review .page-nav { display: none; }
.embedded-review .document-canvas { width: auto; margin-left: 0; padding: 16px; overflow: visible; }
.embedded-review .page { width: 100%; max-width: none; margin: 0; padding: 0; box-shadow: none; border-radius: 0; }
.embedded-review .page-label { display: none; }
.page, .page * { min-width: 0; }
p, li, dd, dt, h1, h2, h3, h4, h5, h6, th, td { overflow-wrap: anywhere; word-break: break-word; }
.table-scroll-wrap { max-width: 100%; }
table { table-layout: fixed; }
"""

_EMBED_JS_INJECT = """\
<script>
var _sp = new URLSearchParams(window.location.search);
if (_sp.get("embed") === "1") {
  document.documentElement.classList.add("embedded-review");
  document.body.classList.add("embedded-review");
}
</script>
"""


def format_datetime(unix_ts):
    """Format a Unix timestamp as a human-readable date/time string."""
    return datetime.datetime.fromtimestamp(unix_ts).strftime("%b %d, %Y %H:%M")


def normalize_classification(page):
    """Return a page's classification, falling back to the legacy
    `complex`/`labels` contract so sessions created before the
    `classification` field existed still render correctly (Complex/Simple)
    instead of as a non-clickable "Pending"."""
    cls = page.get("classification")
    if cls is not None:
        return cls
    if "complex" in page:
        return "Complex" if page.get("complex") else "Simple"
    return None


def _sse_event(payload: dict) -> str:
    import json
    return f"data: {json.dumps(payload)}\n\n"


def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.jinja_env.filters["datetime"] = format_datetime

    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)

    app.config["UPLOAD_DIR"] = os.path.join(project_root, "uploads")
    app.config["CROP_DIR"] = os.path.join(project_root, "crops")
    app.config["EXTRACTED_DIR"] = os.path.join(
        base_dir, "static", "extracted"
    )
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm

    from table_extractor.html_extractor import (
        _start_extraction_job,
        _get_active_job,
        _output_complete,
        _remove_output_marker,
        set_output_root,
        on_crop_mutation,
        _is_extraction_in_progress,
        ExtractionInProgressError,
        normalize_legacy_meta,
    )
    set_output_root(app.config["EXTRACTED_DIR"])

    @app.route("/health")
    def health():
        """Return a health check response."""
        return {"status": "ok"}

    @app.route("/", methods=["GET"])
    def index():
        """Render the upload landing page."""
        return render_template("index.html")

    @app.route("/annotate/<session_id>", methods=["GET"])
    def annotate_page(session_id):
        """Render the annotation page for a session."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        page_index = request.args.get("page", 0, type=int)
        meta = _sm.load_meta(session_id)

        if page_index >= len(meta["pages"]):
            return "Page index out of range", 404

        return render_template(
            "annotate.html",
            session_id=session_id,
            page_data=meta["pages"][page_index],
            all_pages=[
                {
                    "index": i,
                    "classification": normalize_classification(p),
                    "path": p["path"],
                    "has_draft": "draft" in p,
                }
                for i, p in enumerate(meta["pages"])
            ],
        )

    @app.route("/upload", methods=["POST"])
    def upload():
        """Upload PDF or image files and create a new session."""
        _sm = app.session_manager
        files = request.files.getlist("files")
        if not files or files[0].filename == "":
            return jsonify({"error": "No files provided"}), 400

        session_id = _sm.create_session()
        original_dir = _sm.get_original_dir(session_id)
        page_dir = _sm.get_page_dir(session_id)
        session_dir = _sm.get_session_dir(session_id)
        pages_meta = []
        page_counter = 0

        for f in files:
            if not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in UPLOAD_EXTENSIONS:
                continue

            safe_name = secure_filename(f.filename)
            saved_path = os.path.join(original_dir, safe_name)
            f.save(saved_path)

            if ext == ".pdf":
                pages = pdf_to_pages(saved_path, page_dir, start_index=page_counter)
                pdf_relpath = os.path.relpath(saved_path, session_dir)
                for page_idx, page_path in enumerate(pages):
                    pages_meta.append({
                        "path": os.path.relpath(page_path, page_dir),
                        "classification": None,
                        "crops": [],
                        "pdf_path": pdf_relpath,
                        "pdf_page": page_idx,
                    })
                page_counter += len(pages)
            else:
                from PIL import Image
                img = Image.open(saved_path)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                page_name = f"page_{page_counter:03d}.png"
                page_path = os.path.join(page_dir, page_name)
                img.save(page_path, "PNG")
                pages_meta.append({
                    "path": os.path.relpath(page_path, page_dir),
                    "classification": None,
                    "crops": [],
                    "pdf_path": None,
                    "pdf_page": None,
                })
                page_counter += 1

        meta = {
            "files": [f.filename for f in files if f.filename],
            "pages": pages_meta,
        }
        _sm.save_meta(session_id, meta)

        return jsonify({"session_id": session_id, "page_count": page_counter})

    @app.route("/analyze/<session_id>", methods=["POST"])
    def analyze_session(session_id):
        """Analyze all unclassified pages in a session."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        page_dir = _sm.get_page_dir(session_id)
        session_dir = _sm.get_session_dir(session_id)

        # 1. Read snapshot: identify pages needing analysis (no lock held)
        meta_snapshot = _sm.load_meta(session_id)
        pending = []
        for page_idx, page_info in enumerate(meta_snapshot["pages"]):
            if page_info.get("analysis_status") == "done":
                continue
            page_path = os.path.join(page_dir, page_info["path"])
            pending.append((page_idx, page_path, page_info))

        # 2. Call the LLM WITHOUT holding the lock (slow I/O outside critical section) and persist immediately
        for page_idx, page_path, page_snapshot in pending:
            if not os.path.exists(page_path):
                result = {"classification": None, "error": "Page file missing"}
            else:
                result = analyze_page(page_path)
                # HIRES_PAGES (default off): when enabled, Complex pages are
                # re-rendered at 300 DPI for higher-fidelity crops. When off,
                # crops stay at the 150 DPI used for analysis — which the LLM
                # already handles, and which keeps small vision models from
                # returning blank output on oversized 300 DPI crops.
                if os.environ.get("HIRES_PAGES", "false").lower() == "true" and \
                   result.get("classification") == "Complex" and \
                   page_snapshot.get("pdf_path") and page_snapshot.get("pdf_page") is not None:
                    try:
                        pdf_full_path = os.path.join(session_dir, page_snapshot["pdf_path"])
                        upgrade_page_to_hires(pdf_full_path, page_path, page_snapshot["pdf_page"])
                    except Exception as e:
                        result = dict(result)
                        result["upgrade_error"] = str(e)

            # Persist immediately under lock
            with _sm.metadata_lock(session_id):
                meta = _sm.load_meta(session_id)
                if page_idx < len(meta["pages"]):
                    page_info = meta["pages"][page_idx]
                    if result.get("classification") is None:
                        page_info["analysis_status"] = "error"
                        page_info["analysis_error"] = result.get("error")
                    else:
                        page_info["analysis_status"] = "done"
                        page_info["classification"] = result["classification"]
                        page_info["analysis_error"] = None
                        if result.get("upgrade_error"):
                            page_info["upgrade_error"] = result["upgrade_error"]
                    _sm.save_meta_atomic(session_id, meta)

        # 3. Reload current meta to return the fully updated copy
        with _sm.metadata_lock(session_id):
            meta = _sm.load_meta(session_id)
        return jsonify(meta)

    @app.route("/session/<session_id>", methods=["GET"])
    def get_session(session_id):
        """Return session metadata as JSON."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        meta = _sm.load_meta(session_id)
        for page in meta.get("pages", []):
            if page.get("classification") is None:
                page["classification"] = normalize_classification(page)
        return jsonify(meta)

    @app.route("/pages/<session_id>/<filename>", methods=["GET"])
    def serve_page(session_id, filename):
        """Serve a page image file."""
        _sm = app.session_manager
        page_dir = _sm.get_page_dir(session_id)
        filepath = os.path.join(page_dir, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "Page not found"}), 404
        return send_file(filepath, mimetype="image/png")

    @app.route("/commit/<session_id>", methods=["POST"])
    def commit_crops(session_id):
        """Save new crop regions for a page and persist them."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        data = request.get_json()
        if not data or "page_index" not in data or "crops" not in data:
            return jsonify({"error": "Missing page_index or crops"}), 400
        with _sm.metadata_lock(session_id):
            if _is_extraction_in_progress(session_id):
                return jsonify({"status": "error", "message": "Extraction in progress"}), 409
            meta = _sm.load_meta(session_id)
            fragments_dir = _sm.get_extraction_fragments_dir(session_id)
            crop_dir = os.path.join(app.config["CROP_DIR"], session_id)
            meta = normalize_legacy_meta(meta, fragments_dir, crop_dir)
            page_index = data["page_index"]
            if not isinstance(page_index, int) or page_index < 0 or page_index >= len(meta["pages"]):
                return jsonify({"error": "Invalid page_index"}), 400
            page_info = meta["pages"][page_index]
            page_path = os.path.join(_sm.get_page_dir(session_id), page_info["path"])
            cm = CropManager(app.config["CROP_DIR"])
            existing = page_info.get("crops", [])
            existing_keys = {
                (round(c["bbox"][0], 6), round(c["bbox"][1], 6),
                 round(c["bbox"][2], 6), round(c["bbox"][3], 6))
                for c in existing
            }
            next_id = meta.get("next_crop_id", 0)
            newly_saved = []
            for item in data["crops"]:
                bbox = item["bbox"]
                key = (round(bbox[0], 6), round(bbox[1], 6),
                       round(bbox[2], 6), round(bbox[3], 6))
                if key in existing_keys:
                    continue
                filename = f"crop_{next_id:03d}.png"
                crop_path = cm.save_crop(session_id, page_path, bbox, filename=filename)
                crop_filename = os.path.basename(crop_path)
                record = {"path": crop_filename, "filename": crop_filename, "bbox": bbox}
                newly_saved.append(record)
                existing.append(record)
                next_id += 1
            page_info["crops"] = existing
            meta["next_crop_id"] = next_id
            if "draft" in page_info:
                del page_info["draft"]
            on_crop_mutation(meta, _sm, session_id, app.config["EXTRACTED_DIR"])
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"crops": existing, "added": newly_saved, "page_index": page_index})

    @app.route("/trim/<session_id>/<crop_filename>", methods=["POST"])
    def trim_crop(session_id, crop_filename):
        """Trim an existing crop to a new bounding box."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        data = request.get_json()
        if not data or "bbox" not in data:
            return jsonify({"error": "Missing bbox"}), 400
        with _sm.metadata_lock(session_id):
            if _is_extraction_in_progress(session_id):
                return jsonify({"status": "error", "message": "Extraction in progress"}), 409
            meta = _sm.load_meta(session_id)
            fragments_dir = _sm.get_extraction_fragments_dir(session_id)
            crop_dir = os.path.join(app.config["CROP_DIR"], session_id)
            meta = normalize_legacy_meta(meta, fragments_dir, crop_dir)
            task_id = os.path.splitext(crop_filename)[0]
            for task in meta.get("extraction_tasks", []):
                if task["task_id"] == task_id:
                    task["extraction_status"] = "pending"
                    task["extraction_error"] = None
                    task["extraction_error_type"] = None
                    frag_path = os.path.join(fragments_dir, f"{task_id}.html")
                    if os.path.exists(frag_path):
                        os.unlink(frag_path)
                    task["fragment_path"] = None
                    break
            recorded_crops = set()
            for page in meta.get("pages", []):
                for crop in page.get("crops", []):
                    recorded_crops.add(crop.get("filename") or crop.get("path"))
            if crop_filename not in recorded_crops:
                return jsonify({"error": "Crop not found in metadata"}), 404

            cm = CropManager(app.config["CROP_DIR"])
            session_crop_dir = os.path.realpath(os.path.join(cm.crop_root, session_id))
            crop_path = os.path.realpath(os.path.join(session_crop_dir, crop_filename))
            if not crop_path.startswith(session_crop_dir + os.sep):
                return jsonify({"error": "Access denied"}), 403

            if not os.path.exists(crop_path):
                return jsonify({"error": "Crop not found"}), 404
            cm.trim_crop(crop_path, data["bbox"])
            on_crop_mutation(meta, _sm, session_id, app.config["EXTRACTED_DIR"])
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"path": crop_path, "filename": crop_filename})

    @app.route("/delete-crop/<session_id>", methods=["POST"])
    def delete_crop(session_id):
        """Delete a crop file and remove it from session metadata."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        data = request.get_json()
        if not data or "page_index" not in data or "filename" not in data:
            return jsonify({"error": "Missing page_index or filename"}), 400
        with _sm.metadata_lock(session_id):
            if _is_extraction_in_progress(session_id):
                return jsonify({"status": "error", "message": "Extraction in progress"}), 409
            meta = _sm.load_meta(session_id)
            fragments_dir = _sm.get_extraction_fragments_dir(session_id)
            crop_dir = os.path.join(app.config["CROP_DIR"], session_id)
            meta = normalize_legacy_meta(meta, fragments_dir, crop_dir)
            page_index = data["page_index"]
            if not isinstance(page_index, int) or page_index < 0 or page_index >= len(meta["pages"]):
                return jsonify({"error": "Invalid page_index"}), 400
            page_info = meta["pages"][page_index]
            filename = data["filename"]
            page_crops = {c.get("filename") or c.get("path") for c in page_info.get("crops", [])}
            if filename not in page_crops:
                return jsonify({"error": "Crop not found in page metadata"}), 404

            cm = CropManager(app.config["CROP_DIR"])
            session_crop_dir = os.path.realpath(os.path.join(cm.crop_root, session_id))
            crop_path = os.path.realpath(os.path.join(session_crop_dir, filename))
            if not crop_path.startswith(session_crop_dir + os.sep):
                return jsonify({"error": "Access denied"}), 403

            if os.path.exists(crop_path):
                os.remove(crop_path)
            before = len(page_info.get("crops", []))
            page_info["crops"] = [c for c in page_info.get("crops", []) if (c.get("filename") or c.get("path")) != filename]
            removed = before - len(page_info["crops"])
            on_crop_mutation(meta, _sm, session_id, app.config["EXTRACTED_DIR"])
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"ok": True, "removed": removed})

    @app.route("/crops/<session_id>/<crop_filename>", methods=["GET"])
    def serve_crop(session_id, crop_filename):
        """Serve a crop image file."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        # Verify the filename is recorded in metadata before serving (membership check).
        meta = _sm.load_meta(session_id)
        recorded_crops = set()
        for page in meta.get("pages", []):
            for crop in page.get("crops", []):
                recorded_crops.add(crop.get("filename") or crop.get("path"))
        if crop_filename not in recorded_crops:
            return jsonify({"error": "Crop not found in metadata"}), 404
        # Realpath-containment check (path traversal guard).
        cm = CropManager(app.config["CROP_DIR"])
        session_crop_dir = os.path.realpath(os.path.join(cm.crop_root, session_id))
        crop_path = os.path.realpath(os.path.join(session_crop_dir, crop_filename))
        if not crop_path.startswith(session_crop_dir + os.sep):
            return jsonify({"error": "Access denied"}), 403
        if not os.path.exists(crop_path):
            return jsonify({"error": "Crop not found"}), 404
        return send_file(crop_path, mimetype="image/png")

    @app.route("/sessions", methods=["GET"])
    def list_sessions():
        """Render the sessions list page."""
        _sm = app.session_manager
        sessions = []
        for sid in _sm.list_sessions():
            meta = _sm.load_meta(sid)
            if not meta:
                continue
            session_dir = _sm.get_session_dir(sid)
            pages = meta.get("pages", [])
            done = sum(1 for p in pages if p.get("analysis_status") == "done")
            errored = sum(1 for p in pages if p.get("analysis_status") == "error")
            if done == 0:
                analysis_status = "Not classified"
            elif errored:
                analysis_status = f"Partial ({errored} errors)"
            elif done == len(pages):
                analysis_status = "Done"
            else:
                analysis_status = f"{done} of {len(pages)} pages classified"
            tasks = meta.get("extraction_tasks", [])
            extracted = sum(1 for t in tasks if t.get("extraction_status") == "extracted")
            if not tasks:
                extraction_status = "No tasks extracted"
            elif _output_complete(sid):
                extraction_status = "Done"
            elif extracted == len(tasks):
                extraction_status = "Interrupted — click to resume"
            else:
                extraction_status = f"{extracted} of {len(tasks)} tasks extracted"
            page_count = len(pages)
            crop_count = sum(len(p.get("crops", [])) for p in pages)
            files = meta.get("files", [])
            name = files[0] if files else sid
            sessions.append({
                "id": sid, "name": name, "files": files,
                "page_count": page_count, "crop_count": crop_count,
                "uploaded_at": os.path.getmtime(session_dir),
                "analysis_status": analysis_status,
                "extraction_status": extraction_status,
            })
        return render_template("sessions.html", sessions=sessions)

    @app.route("/save-draft/<session_id>", methods=["POST"])
    def save_draft(session_id):
        """Save draft crop boxes for a page without committing them."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json()
        if not data or "page_index" not in data or "boxes" not in data:
            return jsonify({"error": "Missing page_index or boxes"}), 400

        page_index = data["page_index"]
        with _sm.metadata_lock(session_id):
            meta = _sm.load_meta(session_id)
            if not isinstance(page_index, int) or page_index < 0 or page_index >= len(meta["pages"]):
                return jsonify({"error": "Invalid page_index"}), 400

            page_info = meta["pages"][page_index]
            page_info["draft"] = data["boxes"]
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"ok": True})

    @app.route("/clear-draft/<session_id>", methods=["POST"])
    def clear_draft(session_id):
        """Remove draft crop boxes from a page."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json()
        if not data or "page_index" not in data:
            return jsonify({"error": "Missing page_index"}), 400

        page_index = data["page_index"]
        with _sm.metadata_lock(session_id):
            meta = _sm.load_meta(session_id)
            if not isinstance(page_index, int) or page_index < 0 or page_index >= len(meta["pages"]):
                return jsonify({"error": "Invalid page_index"}), 400

            page_info = meta["pages"][page_index]
            if "draft" in page_info:
                del page_info["draft"]
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"ok": True})

    @app.route("/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id):
        """Delete a session and all associated files."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404

        with _sm.metadata_lock(session_id):
            if _is_extraction_in_progress(session_id):
                return jsonify({"status": "error", "message": "Cannot delete session with active extraction"}), 409

            session_dir = _sm.get_session_dir(session_id)
            crop_dir = _sm.get_crop_dir(session_id)

            shutil.rmtree(session_dir, ignore_errors=True)
            shutil.rmtree(crop_dir, ignore_errors=True)

        return jsonify({"ok": True})

    @app.route("/extract-html/<session_id>", methods=["GET"])
    def extract_html_page(session_id):
        """Render the HTML extraction progress page for a session."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return render_template("error.html", message="Session not found"), 400
        # Normalize legacy metadata so that sessions with a valid `classification`
        # but no `analysis_status` are promoted before the analysis gate below.
        with _sm.metadata_lock(session_id):
            meta = _sm.load_meta(session_id)
            fragments_dir = _sm.get_extraction_fragments_dir(session_id)
            crop_dir = os.path.join(app.config["CROP_DIR"], session_id)
            meta = normalize_legacy_meta(meta, fragments_dir, crop_dir)
            _sm.save_meta_atomic(session_id, meta)
        for page in meta.get("pages", []):
            if page.get("draft") and len(page["draft"]) > 0:
                return render_template("error.html", message="You have uncommitted changes. Please commit them before extracting HTML."), 400
        analyzed = all(p.get("analysis_status") == "done" for p in meta.get("pages", []))
        if not analyzed:
            return render_template("error.html", message="Please analyze all pages before extracting HTML."), 400
        if _output_complete(session_id):
            return redirect(f"/review/{session_id}?page=0")
        return render_template("extract_progress.html", session_id=session_id)

    @app.route("/extract-html/<session_id>", methods=["POST"])
    def start_extraction(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        retry_nonretryable = request.args.get("retry_nonretryable", "false") == "true"
        with _sm.metadata_lock(session_id):
            meta = _sm.load_meta(session_id)
            fragments_dir = _sm.get_extraction_fragments_dir(session_id)
            crop_dir = os.path.join(app.config["CROP_DIR"], session_id)
            meta = normalize_legacy_meta(meta, fragments_dir, crop_dir)
            _sm.save_meta_atomic(session_id, meta)

            if not all(p.get("analysis_status") == "done" for p in meta.get("pages", [])):
                return jsonify({"status": "error", "message": "Not all pages analyzed"}), 400
            tasks = meta.get("extraction_tasks", [])
            if not retry_nonretryable:
                auth_failed = [t for t in tasks
                               if t["extraction_status"] == "failed"
                               and t.get("extraction_error_type") in ("auth", "credits")]
                if auth_failed:
                    return jsonify({
                        "status": "error",
                        "message": "Auth/credit failure. Call with ?retry_nonretryable=true after fixing.",
                        "error_type": auth_failed[0]["extraction_error_type"],
                    }), 400
            # Remove stale .complete marker synchronously, while still holding the
            # metadata lock, so any viewer arriving before the background thread runs
            # cannot be served stale output.
            incomplete_tasks = [t for t in tasks if t.get("extraction_status") != "extracted"]
            if incomplete_tasks:
                _remove_output_marker(session_id, app.config["EXTRACTED_DIR"])
            try:
                _start_extraction_job(
                    session_id, _sm, app.config["CROP_DIR"], _sm.get_page_dir(session_id),
                    app.config["EXTRACTED_DIR"], os.environ["DATA_EXTRACTION_MODEL_ID"],
                    retry_nonretryable=retry_nonretryable,
                )
            except ExtractionInProgressError:
                return jsonify({"status": "error", "message": "Extraction already running"}), 409
        return jsonify({"status": "started"})

    @app.route("/extract-progress/<session_id>", methods=["GET"])
    def extract_progress_sse(session_id):
        """Stream HTML extraction progress as server-sent events."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        def generate():
            yield _sse_event({"status": "starting"})
            last_logged_completed = -1
            while True:
                meta = _sm.load_meta(session_id)
                if meta is None:
                    yield _sse_event({"status": "error", "message": "Session no longer exists"})
                    return
                tasks = meta.get("extraction_tasks", [])
                completed = sum(1 for t in tasks if t["extraction_status"] == "extracted")
                total = len(tasks)
                job = _get_active_job(session_id)
                if job is None:
                    if _output_complete(session_id):
                        yield _sse_event({"status": "done", "progress": completed, "total": total})
                        return
                    failed = [t for t in tasks if t["extraction_status"] == "failed"]
                    if failed:
                        terminal_err = next(
                            (t for t in failed if t["extraction_error_type"] in ("auth", "credits")),
                            failed[0])
                        yield _sse_event({
                            "status": "error",
                            "error_type": terminal_err.get("extraction_error_type") or "retryable",
                            "message": terminal_err.get("extraction_error") or "Tasks failed",
                            "progress": completed, "total": total,
                        })
                        return
                    if total == 0 or completed == 0:
                        yield _sse_event({"status": "idle", "message": "Extraction not started"})
                        return
                    yield _sse_event({
                        "status": "paused", "progress": completed, "total": total,
                        "message": "Extraction interrupted. Click Retry to resume.",
                    })
                    return
                if job.done_event.is_set():
                    if job.result == "cancelled":
                        yield _sse_event({"status": "cancelled", "progress": completed, "total": total})
                    elif job.result == "error":
                        yield _sse_event({
                            "status": "error", "error_type": job.error_type or "retryable",
                            "message": job.error_message or "Tasks failed",
                            "progress": completed, "total": total,
                        })
                    else:
                        yield _sse_event({"status": "done", "progress": completed, "total": total})
                        yield _sse_event({"status": "done", "progress": completed, "total": total})
                    return
                event = {
                    "status": "progress", "progress": completed, "total": total,
                }
                if completed != last_logged_completed:
                    event["log"] = f"Extracted {completed}/{total} regions..."
                    last_logged_completed = completed
                yield _sse_event(event)
                job.done_event.wait(timeout=0.5)

        return Response(generate(), mimetype="text/event-stream")

    def _save_page_html(session_id, page_idx, edited_html):
        """Persist edited per-page HTML to
        <EXTRACTED_DIR>/<session_id>/page-<page_idx>.html.

        Returns a Flask response tuple (body, status). Shared by the
        POST /save-page route and the POST branch of serve_extracted_page so
        the per-page edit JS works whether it POSTs to /save-page/... or to the
        page URL itself."""
        out_dir = app.config["EXTRACTED_DIR"]
        base_dir = os.path.realpath(out_dir)
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not session_dir.startswith(base_dir + os.sep):
            return jsonify({"status": "error", "message": "Invalid session id"}), 400

        out_path = os.path.realpath(
            os.path.join(session_dir, f"page-{page_idx}.html")
        )
        if not out_path.startswith(session_dir + os.sep):
            return jsonify({"status": "error", "message": "Invalid page index"}), 400

        os.makedirs(session_dir, exist_ok=True)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(edited_html)
        except OSError as e:
            return jsonify({"status": "error", "message": str(e)}), 500

        return jsonify({"status": "ok"})

    @app.route("/save-page/<session_id>/<int:page_idx>", methods=["POST"])
    def save_page(session_id, page_idx):
        """Persist edited HTML for a single page via the dedicated save endpoint."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"status": "error", "message": "Session not found"}), 404

        meta = _sm.load_meta(session_id)
        total_pages = len(meta.get("pages", []))
        if page_idx < 0 or page_idx >= total_pages:
            return jsonify({"status": "error", "message": "Invalid page index"}), 400

        edited_html = request.get_data(as_text=True)
        if not edited_html:
            return jsonify({"status": "error", "message": "Empty body"}), 400

        return _save_page_html(session_id, page_idx, edited_html)

    @app.route("/extracted/<session_id>/extraction.html", methods=["GET"])
    def serve_extracted_html(session_id):
        """Serve the index page for an extracted session."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        base_dir = os.path.realpath(app.config["EXTRACTED_DIR"])
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not session_dir.startswith(base_dir + os.sep):
            return "Session not found", 404
        if not os.path.isdir(session_dir):
            return "Extraction not found. Please run extraction first.", 404

        complete_marker = os.path.join(session_dir, ".complete")
        if not os.path.exists(complete_marker):
            return "Extraction not complete. Please retry.", 404

        index_path = os.path.join(session_dir, "index.html")
        if os.path.exists(index_path):
            return send_file(index_path, mimetype="text/html")

        return "Extraction not found. Please run extraction first.", 404

    @app.route("/extracted/<session_id>/page-<int:page_idx>.html", methods=["GET", "POST"])
    def serve_extracted_page(session_id, page_idx):
        """Serve or update a single extracted page HTML file."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        if request.method == "POST":
            edited_html = request.get_data(as_text=True)
            if not edited_html:
                return jsonify({"status": "error", "message": "Empty body"}), 400

            meta = _sm.load_meta(session_id)
            total_pages = len(meta.get("pages", []))
            if page_idx < 0 or page_idx >= total_pages:
                return jsonify({"status": "error", "message": "Invalid page index"}), 400

            return _save_page_html(session_id, page_idx, edited_html)

        base_dir = os.path.realpath(app.config["EXTRACTED_DIR"])
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not session_dir.startswith(base_dir + os.sep):
            return "Session not found", 404

        if not os.path.isdir(session_dir):
            return "Extraction not found. Please run extraction first.", 404

        complete_marker = os.path.join(session_dir, ".complete")
        if not os.path.exists(complete_marker):
            return "Extraction not complete. Please retry.", 404

        out_path = os.path.realpath(
            os.path.join(session_dir, f"page-{page_idx}.html")
        )
        if not out_path.startswith(session_dir + os.sep):
            return "Page not found", 404
        if os.path.exists(out_path):
            embed_mode = request.args.get("embed") == "1"
            if embed_mode:
                with open(out_path, "r", encoding="utf-8") as _f:
                    html_content = _f.read()
                if ".embedded-review" not in html_content:
                    inject = "<style>" + _EMBED_CSS_INJECT + "</style>\n" + _EMBED_JS_INJECT
                    html_content = html_content.replace("</head>", inject + "\n</head>")
                return Response(html_content, mimetype="text/html")
            return send_file(out_path, mimetype="text/html")

        return "Page not found", 404

    @app.route("/review/<session_id>", methods=["GET"])
    def review_workspace(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id) or not _output_complete(session_id):
            return "Extraction not found. Please run extraction first.", 404
        meta = _sm.load_meta(session_id)
        page_idx = request.args.get("page", 0, type=int)
        pages = meta.get("pages", [])
        if page_idx is None or page_idx < 0 or page_idx >= len(pages):
            return "Page not found", 404
        return render_template("review.html", session_id=session_id,
                               pages=[{"path": page["path"]} for page in pages],
                               initial_page=page_idx)

    return app


if __name__ == "__main__":
    app = create_app()
    # use_reloader=False: the extraction job runs in a background thread that
    # cannot survive a process restart, so auto-restarting on file changes
    # would kill in-flight extractions and corrupt their state.
    app.run(debug=True, use_reloader=False, port=5000)
