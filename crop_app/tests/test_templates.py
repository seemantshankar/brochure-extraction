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
