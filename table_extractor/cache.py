import hashlib
import json
import os
from pathlib import Path

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".stage_cache")

def _cache_key(image_bytes: bytes, stage: str, model: str) -> str:
    h = hashlib.sha256(image_bytes).hexdigest()[:16]
    sanitized_model = model.replace("/", "_")
    return f"{h}_{stage}_{sanitized_model}"

def cached_call(image_bytes: bytes, stage: str, model: str, fn, force: bool = False) -> dict:
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _cache_key(image_bytes, stage, model)
    cache_file = Path(CACHE_DIR) / f"{key}.json"

    if cache_file.exists() and not force:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    result = fn()
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result
