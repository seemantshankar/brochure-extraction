import os
import sys
import json
import shutil
import logging
import datetime
from flask import Flask, request, jsonify, redirect, url_for, send_file, render_template, Response

logger = logging.getLogger(__name__)
from werkzeug.utils import secure_filename
from session_manager import SessionManager
from crop_manager import CropManager
from pdf_converter import pdf_to_pages, upgrade_page_to_hires
from llm import analyze_page

UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


def format_datetime(unix_ts):
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
    app = Flask(__name__)
    app.jinja_env.filters["datetime"] = format_datetime

    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    env_path = os.path.join(project_root, ".env")

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v_raw = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v_raw)

    app.config["UPLOAD_DIR"] = os.path.join(project_root, "uploads")
    app.config["CROP_DIR"] = os.path.join(project_root, "crops")
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm

    @app.route("/health")
    def health():
        return {"status": "ok"}

    @app.route("/", methods=["GET"])
    def index():
        return render_template("index.html")

    @app.route("/annotate/<session_id>", methods=["GET"])
    def annotate_page(session_id):
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
        _sm = app.session_manager
        page_dir = _sm.get_page_dir(session_id)
        filepath = os.path.join(page_dir, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "Page not found"}), 404
        return send_file(filepath, mimetype="image/png")

    @app.route("/commit/<session_id>", methods=["POST"])
    def commit_crops(session_id):
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
        cm = CropManager(app.config["CROP_DIR"])
        session_crop_dir = os.path.join(cm.crop_root, session_id)
        crop_path = os.path.join(session_crop_dir, crop_filename)
        if not os.path.exists(crop_path):
            return jsonify({"error": "Crop not found"}), 404
        return send_file(crop_path, mimetype="image/png")

    @app.route("/sessions", methods=["GET"])
    def list_sessions():
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
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        def generate():
            yield f"data: {json.dumps({'status': 'starting'})}\n\n"

            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            from table_extractor.html_extractor import run_extraction

            try:
                for event in run_extraction(
                    session_id=session_id,
                    sm=_sm,
                    crop_root=app.config["CROP_DIR"],
                    model=os.environ.get("DATA_EXTRACTION_MODEL_ID", "qwen/qwen3.7-plus"),
                ):
                    if event["status"] == "done":
                        result_html = event["html"]
                        out_dir = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "static", "extracted", session_id,
                        )
                        os.makedirs(out_dir, exist_ok=True)
                        out_path = os.path.join(out_dir, "extraction.html")
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(result_html)

                        yield f"data: {json.dumps({'status': 'done'})}\n\n"
                    else:
                        yield f"data: {json.dumps(event)}\n\n"

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error("HTML extraction failed for session %s: %s\n%s", session_id, e, tb)
                yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

        return Response(generate(), mimetype="text/event-stream")

    @app.route("/extracted/<session_id>/extraction.html", methods=["GET"])
    def serve_extracted_html(session_id):
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "extracted", session_id, "extraction.html",
        )
        if not os.path.exists(out_path):
            return "Extraction not found. Please run extraction first.", 404
        return send_file(out_path, mimetype="text/html")

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
