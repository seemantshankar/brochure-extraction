import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".stage_cache")

_locks = {}
_locks_lock = threading.Lock()


def _get_lock(key: str) -> threading.Lock:
    with _locks_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _cache_key(image_bytes: bytes, stage: str, model: str, extra: str = "") -> str:
    h = hashlib.sha256(image_bytes).hexdigest()[:16]
    sanitized_model = model.replace("/", "_")
    extras_hash = hashlib.sha256(extra.encode("utf-8")).hexdigest()[:8] if extra else ""
    parts = [h, stage, sanitized_model]
    if extras_hash:
        parts.append(extras_hash)
    return "_".join(parts)


def cached_call(
    image_bytes: bytes,
    stage: str,
    model: str,
    fn,
    force: bool = False,
    extra_key: str = "",
) -> dict:
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _cache_key(image_bytes, stage, model, extra_key)
    cache_file = Path(CACHE_DIR) / f"{key}.json"
    lock = _get_lock(key)

    with lock:
        if cache_file.exists() and not force:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)

    result = fn()

    with lock:
        fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            os.replace(tmp_path, cache_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    return result
