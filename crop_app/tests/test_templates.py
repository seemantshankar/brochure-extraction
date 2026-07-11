from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app


def test_sessions_renders_with_status():
    app = create_app()
    with app.app_context():
        html = app.jinja_env.get_template("sessions.html").render(sessions=[{
            "id": "x", "name": "f.pdf", "files": ["f.pdf"], "page_count": 1,
            "crop_count": 0, "uploaded_at": 0,
            "analysis_status": "Done", "extraction_status": "No tasks extracted",
        }])
    assert "Analysis: Done" in html
    assert "Extraction: No tasks extracted" in html


def test_extract_progress_renders_retry_control():
    app = create_app()
    with app.app_context():
        html = app.jinja_env.get_template("extract_progress.html").render(session_id="x")
    assert 'id="retry-btn"' in html
    assert "Retry" in html


def test_annotation_loads_shared_image_viewer():
    template = open("crop_app/templates/annotate.html", encoding="utf-8").read()
    assert '/static/js/image_viewer.js' in template


def test_shared_image_viewer_supports_review_mode():
    script = open("crop_app/static/js/image_viewer.js", encoding="utf-8").read()
    assert "ImageViewer.create" in script
    assert 'mode === "review"' in script
    assert "ResizeObserver" in script
