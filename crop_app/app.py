import os
import shutil
import datetime
from flask import Flask, request, jsonify, redirect, url_for, send_file, render_template
from werkzeug.utils import secure_filename
from session_manager import SessionManager
from crop_manager import CropManager
from pdf_converter import pdf_to_pages, upgrade_page_to_hires
from llm import analyze_page

UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


def format_datetime(unix_ts):
    return datetime.datetime.fromtimestamp(unix_ts).strftime("%b %d, %Y %H:%M")


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
                    "complex": p["complex"],
                    "labels": p["labels"],
                    "path": p["path"],
                    "has_draft": "draft" in p,
                }
                for i, p in enumerate(meta["pages"])
            ],
        )

    @app.route("/upload", methods=["POST"])
    def upload():
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
                        "complex": None,
                        "labels": [],
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
                    "complex": None,
                    "labels": [],
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
            if page_info["complex"] is not None:
                continue

            page_path = os.path.join(page_dir, page_info["path"])
            if not os.path.exists(page_path):
                page_info["complex"] = False
                page_info["labels"] = []
                page_info["error"] = "Page file missing"
                continue

            result = analyze_page(page_path)
            page_info["complex"] = result["complex"]
            page_info["labels"] = result["labels"]
            if result.get("error"):
                page_info["error"] = result["error"]

            if result["complex"] and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:
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
        return jsonify(_sm.load_meta(session_id))

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

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
