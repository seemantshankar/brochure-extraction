"""Flask web application for brochure upload, cropping, analysis, and HTML extraction."""
import os
import sys
import json
import shutil
import logging
import datetime
import threading
from flask import Flask, request, jsonify, redirect, url_for, send_file, render_template, Response

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

from werkzeug.utils import secure_filename
from session_manager import SessionManager
from crop_manager import CropManager
from pdf_converter import pdf_to_pages, upgrade_page_to_hires
from llm import analyze_page
from table_extractor.html_extractor import run_extraction

UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


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

        meta = _sm.load_meta(session_id)
        page_dir = _sm.get_page_dir(session_id)
        session_dir = _sm.get_session_dir(session_id)
        updated = False

        for page_info in meta["pages"]:
            if page_info.get("classification") is not None:
                continue

            page_path = os.path.join(page_dir, page_info["path"])
            if not os.path.exists(page_path):
                page_info["classification"] = "Complex"
                page_info["error"] = "Page file missing"
                updated = True
                continue

            result = analyze_page(page_path)
            page_info["classification"] = result.get("classification", "Complex")
            if result.get("error"):
                page_info["error"] = result["error"]

            if page_info["classification"] == "Complex" and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:
                try:
                    pdf_full_path = os.path.join(session_dir, page_info["pdf_path"])
                    upgrade_page_to_hires(pdf_full_path, page_path, page_info["pdf_page"])
                    page_info["upgraded"] = True
                except Exception as e:
                    page_info["upgrade_error"] = str(e)

            updated = True

        if updated:
            _sm.save_meta(session_id, meta)

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

        meta = _sm.load_meta(session_id)
        page_index = data["page_index"]
        if page_index >= len(meta["pages"]):
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

        newly_saved = []
        for item in data["crops"]:
            bbox = item["bbox"]
            key = (round(bbox[0], 6), round(bbox[1], 6),
                   round(bbox[2], 6), round(bbox[3], 6))
            if key in existing_keys:
                continue
            crop_path = cm.save_crop(session_id, page_path, bbox)
            crop_filename = os.path.basename(crop_path)
            record = {
                "path": crop_filename,
                "filename": crop_filename,
                "bbox": bbox,
            }
            newly_saved.append(record)
            existing.append(record)

        page_info["crops"] = existing
        if "draft" in page_info:
            del page_info["draft"]
        _sm.save_meta(session_id, meta)

        return jsonify({"crops": existing, "added": newly_saved, "page_index": page_index})

    @app.route("/trim/<session_id>/<crop_filename>", methods=["POST"])
    def trim_crop(session_id, crop_filename):
        """Trim an existing crop to a new bounding box."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404

        cm = CropManager(app.config["CROP_DIR"])
        session_crop_dir = os.path.join(cm.crop_root, session_id)
        crop_path = os.path.join(session_crop_dir, crop_filename)

        if not os.path.exists(crop_path):
            return jsonify({"error": "Crop not found"}), 404

        data = request.get_json()
        if not data or "bbox" not in data:
            return jsonify({"error": "Missing bbox"}), 400

        cm.trim_crop(crop_path, data["bbox"])

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

        meta = _sm.load_meta(session_id)
        page_index = data["page_index"]
        if page_index >= len(meta["pages"]):
            return jsonify({"error": "Invalid page_index"}), 400

        page_info = meta["pages"][page_index]
        filename = data["filename"]

        cm = CropManager(app.config["CROP_DIR"])
        crop_path = os.path.join(cm.crop_root, session_id, filename)
        if os.path.exists(crop_path):
            os.remove(crop_path)

        before = len(page_info.get("crops", []))
        page_info["crops"] = [
            c for c in page_info.get("crops", []) if c.get("filename") != filename
        ]
        removed = before - len(page_info["crops"])

        _sm.save_meta(session_id, meta)
        return jsonify({"ok": True, "removed": removed})

    @app.route("/crops/<session_id>/<crop_filename>", methods=["GET"])
    def serve_crop(session_id, crop_filename):
        """Serve a crop image file."""
        cm = CropManager(app.config["CROP_DIR"])
        session_crop_dir = os.path.join(cm.crop_root, session_id)
        crop_path = os.path.join(session_crop_dir, crop_filename)
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
            page_count = len(meta.get("pages", []))
            crop_count = sum(len(p.get("crops", [])) for p in meta.get("pages", []))
            files = meta.get("files", [])
            name = files[0] if files else sid
            sessions.append({
                "id": sid,
                "name": name,
                "files": files,
                "page_count": page_count,
                "crop_count": crop_count,
                "uploaded_at": os.path.getmtime(session_dir),
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

        meta = _sm.load_meta(session_id)
        page_index = data["page_index"]
        if page_index >= len(meta["pages"]):
            return jsonify({"error": "Invalid page_index"}), 400

        page_info = meta["pages"][page_index]
        page_info["draft"] = data["boxes"]
        _sm.save_meta(session_id, meta)
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

        meta = _sm.load_meta(session_id)
        page_index = data["page_index"]
        if page_index >= len(meta["pages"]):
            return jsonify({"error": "Invalid page_index"}), 400

        page_info = meta["pages"][page_index]
        if "draft" in page_info:
            del page_info["draft"]
        _sm.save_meta(session_id, meta)
        return jsonify({"ok": True})

    @app.route("/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id):
        """Delete a session and all associated files."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404

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
        meta = _sm.load_meta(session_id)
        for page in meta.get("pages", []):
            if page.get("draft") and len(page["draft"]) > 0:
                return render_template("error.html", message="You have uncommitted changes. Please commit them before extracting HTML."), 400
        if not any(page.get("crops") for page in meta.get("pages", [])):
            return render_template("error.html", message="No crops have been committed. Please commit at least one crop region before extracting HTML."), 400
        return render_template("extract_progress.html", session_id=session_id)

    @app.route("/extract-progress/<session_id>", methods=["GET"])
    def extract_progress_sse(session_id):
        """Stream HTML extraction progress as server-sent events."""
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        def generate():
            """Yield SSE events for the extraction pipeline."""
            yield f"data: {json.dumps({'status': 'starting'})}\n\n"

            cancel_event = threading.Event()

            try:
                for event in run_extraction(
                    session_id=session_id,
                    sm=_sm,
                    crop_root=app.config["CROP_DIR"],
                    model=os.environ["DATA_EXTRACTION_MODEL_ID"],
                    cancel_event=cancel_event,
                ):
                    if event["status"] == "done":
                        yield f"data: {json.dumps({'status': 'done'})}\n\n"
                    else:
                        yield f"data: {json.dumps(event)}\n\n"

            except GeneratorExit:
                cancel_event.set()
                logger.info("Client disconnected from SSE stream for session %s", session_id)
                raise
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error("HTML extraction failed for session %s: %s\n%s", session_id, e, tb)
                yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

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
        if not session_dir.startswith(base_dir):
            return jsonify({"status": "error", "message": "Invalid session id"}), 400

        out_path = os.path.realpath(
            os.path.join(session_dir, f"page-{page_idx}.html")
        )
        if not out_path.startswith(session_dir):
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

        base_dir = app.config["EXTRACTED_DIR"]
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not session_dir.startswith(base_dir + os.sep):
            return "Session not found", 404
        if not os.path.isdir(session_dir):
            return "Extraction not found. Please run extraction first.", 404

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

        base_dir = app.config["EXTRACTED_DIR"]
        session_dir = os.path.realpath(os.path.join(base_dir, session_id))
        if not os.path.isdir(session_dir):
            return "Extraction not found. Please run extraction first.", 404

        out_path = os.path.realpath(
            os.path.join(session_dir, f"page-{page_idx}.html")
        )
        if not out_path.startswith(session_dir + os.sep):
            return "Page not found", 404
        if os.path.exists(out_path):
            return send_file(out_path, mimetype="text/html")

        return "Page not found", 404

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
