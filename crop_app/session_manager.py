import os
import json
import uuid
import tempfile
import threading
from typing import Optional


class SessionManager:
    def __init__(self, upload_dir: str, crop_dir: str):
        self.upload_dir = upload_dir
        self.crop_dir = crop_dir
        os.makedirs(upload_dir, exist_ok=True)
        os.makedirs(crop_dir, exist_ok=True)
        self._session_locks = {}
        self._locks_lock = threading.Lock()

    def create_session(self) -> str:
        """Create a new session directory. Returns session_id (UUID)."""
        sid = str(uuid.uuid4())
        session_dir = os.path.join(self.upload_dir, sid)
        os.makedirs(os.path.join(session_dir, "pages"), exist_ok=True)
        os.makedirs(os.path.join(session_dir, "original"), exist_ok=True)
        return sid

    def save_meta(self, session_id: str, data: dict) -> None:
        with self.metadata_lock(session_id):
            self.save_meta_atomic(session_id, data)

    def metadata_lock(self, session_id):
        """Get (creating if needed) the per-session threading.Lock for meta writes."""
        with self._locks_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def save_meta_atomic(self, session_id, meta):
        """Write meta.json atomically via temp file + os.replace.

        MUST be called while holding metadata_lock(session_id).
        """
        session_dir = self.get_session_dir(session_id)
        os.makedirs(session_dir, exist_ok=True)
        meta_path = os.path.join(session_dir, "meta.json")

        fd, tmp_path = tempfile.mkstemp(dir=session_dir, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, meta_path)
        except BaseException:
            try:
                os.close(fd)  # in case fdopen didn't close it
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_extraction_fragments_dir(self, session_id):
        """Return (and create) the extraction_fragments directory for a session."""
        session_dir = self.get_session_dir(session_id)
        fragments_dir = os.path.join(session_dir, "extraction_fragments")
        os.makedirs(fragments_dir, exist_ok=True)
        return fragments_dir

    def load_meta(self, session_id: str) -> Optional[dict]:
        meta_path = os.path.join(self.upload_dir, session_id, "meta.json")
        if not os.path.exists(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_session_dir(self, session_id: str) -> str:
        return os.path.join(self.upload_dir, session_id)

    def get_page_dir(self, session_id: str) -> str:
        return os.path.join(self.upload_dir, session_id, "pages")

    def get_original_dir(self, session_id: str) -> str:
        return os.path.join(self.upload_dir, session_id, "original")

    def get_crop_dir(self, session_id: str) -> str:
        crop_dir = os.path.join(self.crop_dir, session_id)
        os.makedirs(crop_dir, exist_ok=True)
        return crop_dir

    def list_sessions(self) -> list:
        """List all session IDs found in the upload directory, sorted by newest first."""
        if not os.path.isdir(self.upload_dir):
            return []
        sessions = []
        for name in os.listdir(self.upload_dir):
            sid_dir = os.path.join(self.upload_dir, name)
            if os.path.isdir(sid_dir):
                sessions.append(name)
        sessions.sort(key=lambda s: os.path.getmtime(os.path.join(self.upload_dir, s)), reverse=True)
        return sessions

    def session_exists(self, session_id: str) -> bool:
        return os.path.isdir(self.get_session_dir(session_id))
