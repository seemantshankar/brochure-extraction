# Web App Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `crop_app` web pipeline resumable and crash-safe: every unit of work is persisted to disk immediately, a dropped connection resumes from exactly where it left off, transient LLM failures retry, and permanent failures (auth/credits) require explicit user action.

**Architecture:** A background `ExtractionJob` thread (independent of SSE clients, which are pure subscribers) extracts each task and writes an atomic fragment to disk. `meta.json` gains a top-level `extraction_tasks` array plus per-page `analysis_status`; all meta writes go through `temp file + os.replace` under a per-session lock. A `.complete` marker published atomically after assembly is the sole signal that output is ready. Crop mutations invalidate stale fragments and the completion marker.

**Tech Stack:** Python 3, Flask, `openai` SDK (OpenRouter), `Pillow`, `pytest`, `unittest.mock`.

## Global Constraints

These apply to EVERY task. Copy verbatim from the spec.

- "CLI pipeline (`table_extractor/main.py`) is already cache-idempotent and is out of scope." — do NOT touch it.
- Fragment filename = stable task id = crop filename without extension: `crop_003.html` for crops, `page-{page_idx}.html` for Simple whole-page tasks. Never index by crop-list position.
- `next_crop_id` is a persistent counter in `meta.json`; it NEVER decreases and deleting a crop does NOT decrement it (no ID reuse).
- Every `meta.json` mutation is crash-safe: write to a temp file, then `os.replace` (atomic), performed while holding `metadata_lock(session_id)`.
- `POST /extract-html/<session_id>` is the ONLY way to start or retry an extraction job. `GET /extract-progress/<session_id>` NEVER starts, resumes, or mutates anything — it only observes disk + job state.
- SSE client disconnect (`GeneratorExit`) does NOT cancel the job; the job keeps running in the background.
- All-Simple sessions MAY run extraction. Remove the "at least one committed crop" prerequisite. New prerequisite: all pages must have `analysis_status == "done"`.
- There is NO per-page `extraction_status`. Page completion is derived from the top-level `extraction_tasks` array.
- The `.complete` marker is the sole signal that output is ready; all output-serving routes MUST check it.
- `retry_with_backoff(max_attempts=3)` means at most 3 TOTAL attempts (1 initial + 2 retries), not 3 retries.
- Non-retryable errors (`auth`, `credits`) are NOT auto-retried. Recovery requires an explicit user "Retry" (POST with `?retry_nonretryable=true`).
- New `meta.json` fields are additive; legacy sessions without them must still work (defaults: analysis `pending`, extraction `pending`).

## File Structure

| File | Responsibility |
|---|---|
| `table_extractor/retry.py` | **NEW.** LLM exception hierarchy (`PipelineCallError`, `RetryableError`, `NonRetryableError`, `AuthError`, `CreditsExhaustedError`, `BlankResponseError`, `MalformedOutputError`), `classify_api_error`, `retry_with_backoff`, `is_blank_fragment`. |
| `crop_app/session_manager.py` | Add `metadata_lock()`, `save_meta_atomic()`, `get_extraction_fragments_dir()`. Keep all existing methods. |
| `table_extractor/cache.py` | Already exports `CACHE_DIR` and `_cache_key`. Confirm importable from `html_extractor.py`. No behavioral change. |
| `crop_app/llm.py` | `analyze_page()` wraps the API call in `cached_call` + `retry_with_backoff`; on failure returns `{"classification": None, "error": ...}` (never `"Complex"`); malformed JSON raises `MalformedOutputError` (retryable). |
| `crop_app/crop_manager.py` | `save_crop()` accepts an explicit `filename` (so `app.py` owns the persistent `next_crop_id`). Old file-count counter behavior removed. |
| `table_extractor/html_extractor.py` | **REWRITE.** Add `derive_required_tasks()`, `reconcile_tasks()`, `on_crop_mutation()`, `ExtractionJob` (+ `_execute_extraction`, `_extract_task`, `_run_assembly`), extraction-in-progress guard set, job registry. `extract_crop_as_html()` adds blank detection + stale-cache bypass. Keep `run_extraction` exported? **NO** — it is replaced by `ExtractionJob`; remove `run_extraction`. |
| `crop_app/app.py` | Use atomic meta writes; analyze route persists per-page immediately; `commit`/`trim`/`delete-crop` run `on_crop_mutation` invalidation under lock (return 409 if job running); new `POST /extract-html` start route + `retry_nonretryable` query; remove crop prerequisite; rewrite SSE route to be observational; output-serving routes check `.complete`. |
| `crop_app/templates/sessions.html` | Show analysis + extraction status indicators derived from `meta.json`. |
| `crop_app/templates/annotate.html` | Show per-page analysis error badges. |
| `crop_app/templates/extract_progress.html` | Handle resume / error-type / non-retryable banner / explicit retry button. |

---

## Task 1: LLM exception hierarchy and retry utilities (`table_extractor/retry.py`)

**Files:**
- Create: `table_extractor/retry.py`
- Test: `table_extractor/tests/test_retry.py`

**Interfaces:**
- Produces: `PipelineCallError`, `RetryableError`, `NonRetryableError`, `AuthError`, `CreditsExhaustedError`, `BlankResponseError`, `MalformedOutputError`, `classify_api_error(exc) -> PipelineCallError`, `retry_with_backoff(fn, *, max_attempts, base_delay, max_delay, jitter)`, `is_blank_fragment(fragment: str) -> bool`.
- Consumes: `openai` exception classes (`APIStatusError`, `APIConnectionError`, `APITimeoutError`, `RateLimitError`, `AuthenticationError`, `PermissionDeniedError`).

- [ ] **Step 1: Write the failing test**

```python
# table_extractor/tests/test_retry.py
import time
from unittest.mock import MagicMock

import pytest

from table_extractor.retry import (
    PipelineCallError,
    RetryableError,
    NonRetryableError,
    AuthError,
    CreditsExhaustedError,
    BlankResponseError,
    MalformedOutputError,
    classify_api_error,
    retry_with_backoff,
    is_blank_fragment,
)
```


```python
def test_is_blank_fragment_variants():
    assert is_blank_fragment("") is True
    assert is_blank_fragment("   \n  ") is True
    assert is_blank_fragment("<p>hi</p>") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_retry.py -v`
Expected: ERROR (import of `table_extractor.retry` fails — module does not exist).

- [ ] **Step 3: Write the module implementation**

```python
# table_extractor/retry.py
"""LLM call error hierarchy, classification, and retry-with-backoff utilities."""
import random
import time

from openai import (
    APIStatusError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    AuthenticationError,
    PermissionDeniedError,
)


class PipelineCallError(Exception):
    """Base for pipeline LLM call failures."""

    def __init__(self, error_type: str, message: str, cause: Exception = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.cause = cause


class RetryableError(PipelineCallError):
    """Transient error; caller should retry with backoff."""

    def __init__(self, message: str, cause: Exception = None, retry_after: float = None):
        super().__init__("retryable", message, cause)
        self.retry_after = retry_after  # seconds, or None for exponential backoff


class NonRetryableError(PipelineCallError):
    """Permanent error; caller must not retry."""
    pass


class AuthError(NonRetryableError):
    def __init__(self, message: str, cause: Exception = None):
        super().__init__("auth", message, cause)


class CreditsExhaustedError(NonRetryableError):
    def __init__(self, message: str, cause: Exception = None):
        super().__init__("credits", message, cause)


class BlankResponseError(RetryableError):
    """LLM returned empty/blank output."""
    pass


class MalformedOutputError(RetryableError):
    """LLM output could not be parsed into valid JSON/expected structure."""

    def __init__(self, message: str, cause: Exception = None):
        super().__init__(message, cause)
        self.error_type = "malformed_output"


def _parse_retry_after(response) -> float | None:
    """Parse Retry-After header (seconds) from an HTTP response, capped at max_delay."""
    if response is None:
        return None
    val = response.headers.get("Retry-After")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def classify_api_error(exc: Exception) -> PipelineCallError:
    """Classify an OpenAI SDK exception into a PipelineCallError."""
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return RetryableError(f"Connection/timeout error: {exc}", cause=exc)
    elif isinstance(exc, RateLimitError):
        retry_after = _parse_retry_after(exc.response)
        return RetryableError(f"Rate limited: {exc}", cause=exc, retry_after=retry_after)
    elif isinstance(exc, AuthenticationError):
        return AuthError(f"Authentication failed: {exc}", cause=exc)
    elif isinstance(exc, PermissionDeniedError):
        return AuthError(f"Permission denied: {exc}", cause=exc)
    elif isinstance(exc, APIStatusError):
        sc = exc.status_code
        if 500 <= sc < 600:
            return RetryableError(f"Server error ({sc}): {exc}", cause=exc)
        elif sc == 429:
            retry_after = _parse_retry_after(exc.response)
            return RetryableError(f"Rate limited ({sc}): {exc}", cause=exc, retry_after=retry_after)
        elif sc == 402:
            return CreditsExhaustedError(f"Insufficient credits: {exc}", cause=exc)
        else:
            return NonRetryableError(f"API error ({sc}): {exc}", cause=exc)
    else:
        return NonRetryableError(f"Unexpected error: {exc}", cause=exc)


def retry_with_backoff(
    fn: callable,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.5,
):
    """Execute fn() with exponential backoff on RetryableError.

    max_attempts=3 means at most 3 total attempts (initial + 2 retries).
    Non-retryable errors propagate immediately. Retry-After is respected if present.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except RetryableError as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            if e.retry_after is not None:
                delay = min(e.retry_after, max_delay)
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.random() * jitter
            time.sleep(delay)
        except NonRetryableError as e:
            raise
        except Exception as e:
            classified = classify_api_error(e)
            if isinstance(classified, RetryableError):
                last_exc = classified
                if attempt == max_attempts - 1:
                    raise classified
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.random() * jitter
                time.sleep(delay)
            else:
                raise classified
    # Should be unreachable; last_exc is set if we exhausted retries.
    raise last_exc


def is_blank_fragment(fragment: str) -> bool:
    """Return True if the fragment carries no meaningful content."""
    if not fragment or not fragment.strip():
        return True
    return False
```

- [ ] **Step 4: Add behavior tests for retry + classify**

Append to `table_extractor/tests/test_retry.py`:

```python
def test_retry_exhausts_after_max_attempts():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RetryableError("boom")

    with pytest.raises(RetryableError):
        retry_with_backoff(flaky, max_attempts=3, base_delay=0.001, max_delay=0.01, jitter=0.0)
    assert calls["n"] == 3  # exactly 3 total attempts


def test_retry_succeeds_on_second_attempt():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableError("boom")
        return "ok"

    result = retry_with_backoff(flaky, max_attempts=3, base_delay=0.001, max_delay=0.01, jitter=0.0)
    assert result == "ok"
    assert calls["n"] == 2


def test_non_retryable_propagates_immediately():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AuthError("bad key")

    with pytest.raises(AuthError):
        retry_with_backoff(boom, max_attempts=3)
    assert calls["n"] == 1  # never retried


def test_classify_auth_and_credits():
    resp = MagicMock()
    resp.status_code = 401
    resp.headers = {}
    assert isinstance(classify_api_error(AuthenticationError("x", response=resp, body=None)), AuthError)

    resp402 = MagicMock()
    resp402.status_code = 402
    resp402.headers = {}
    assert isinstance(classify_api_error(APIStatusError("x", response=resp402, body=None)), CreditsExhaustedError)


def test_classify_5xx_is_retryable():
    resp = MagicMock()
    resp.status_code = 503
    resp.headers = {}
    err = classify_api_error(APIStatusError("x", response=resp, body=None))
    assert isinstance(err, RetryableError)


def test_blank_response_error_is_retryable():
    e = BlankResponseError("empty")
    assert isinstance(e, RetryableError)
    assert e.error_type == "retryable"


def test_malformed_output_error_type():
    e = MalformedOutputError("bad json")
    assert e.error_type == "malformed_output"
    assert isinstance(e, RetryableError)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_retry.py -v`
Expected: PASS (all tests green).

- [ ] **Step 6: Commit**

```bash
git add table_extractor/retry.py table_extractor/tests/test_retry.py
git commit -m "feat: add LLM error hierarchy, classify_api_error, retry_with_backoff, is_blank_fragment"
```

---

## Task 2: Atomic, locked meta writes and fragments dir (`session_manager.py`)

**Files:**
- Modify: `crop_app/session_manager.py` (add three methods; keep existing ones)
- Test: `crop_app/tests/test_session_manager.py`

**Interfaces:**
- Produces: `SessionManager.metadata_lock(session_id) -> threading.Lock`, `SessionManager.save_meta_atomic(session_id, meta)`, `SessionManager.get_extraction_fragments_dir(session_id) -> str`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Append to `crop_app/tests/test_session_manager.py`:

```python
import threading
import tempfile


def test_save_meta_atomic_creates_no_tmp_leftover(manager):
    sid = manager.create_session()
    data = {"pages": []}
    manager.save_meta_atomic(sid, data)
    session_dir = os.path.join(manager.upload_dir, sid)
    leftovers = [f for f in os.listdir(session_dir) if f.endswith(".json.tmp")]
    assert leftovers == []
    assert manager.load_meta(sid) == data


def test_metadata_lock_isolation_between_sessions(manager):
    a = manager.metadata_lock("aaa")
    b = manager.metadata_lock("bbb")
    assert a is not b


def test_metadata_lock_returns_same_object_per_session(manager):
    a = manager.metadata_lock("aaa")
    b = manager.metadata_lock("aaa")
    assert a is b


def test_concurrent_writes_under_lock_are_consistent(manager):
    sid = manager.create_session()
    manager.save_meta_atomic(sid, {"counter": 0, "pages": []})

    def worker():
        for _ in range(50):
            with manager.metadata_lock(sid):
                meta = manager.load_meta(sid)
                meta["counter"] += 1
                manager.save_meta_atomic(sid, meta)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert manager.load_meta(sid)["counter"] == 200


def test_get_extraction_fragments_dir(manager):
    sid = manager.create_session()
    frag_dir = manager.get_extraction_fragments_dir(sid)
    assert frag_dir.endswith(os.path.join(sid, "extraction_fragments"))
    assert os.path.isdir(frag_dir)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_session_manager.py -v`
Expected: FAIL (AttributeError: 'SessionManager' has no attribute 'save_meta_atomic').

- [ ] **Step 3: Write the implementation**

Add to `crop_app/session_manager.py`, after the existing `save_meta` method (import `tempfile` and `threading` at the top):

```python
import os
import json
import uuid
import tempfile
import threading
from typing import Optional
```

```python
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
```

Also add the two instance dicts in `__init__`:

```python
    def __init__(self, upload_dir: str, crop_dir: str):
        self.upload_dir = upload_dir
        self.crop_dir = crop_dir
        self._session_locks = {}
        self._locks_lock = threading.Lock()
        os.makedirs(upload_dir, exist_ok=True)
        os.makedirs(crop_dir, exist_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_session_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crop_app/session_manager.py crop_app/tests/test_session_manager.py
git commit -m "feat: add metadata_lock, save_meta_atomic, get_extraction_fragments_dir to SessionManager"
```

---

## Task 3: Confirm `cache.py` exports are importable

**Files:**
- Test: `table_extractor/tests/test_cache.py` (add one test)

**Interfaces:**
- Consumes: `table_extractor.cache.CACHE_DIR`, `table_extractor.cache._cache_key`.

- [ ] **Step 1: Write the test**

Append to `table_extractor/tests/test_cache.py`:

```python
from table_extractor.cache import CACHE_DIR, _cache_key


def test_cache_key_stable_and_extra_key_sensitive():
    k1 = _cache_key(b"abc", "html_extract", "m1", "promptA")
    k2 = _cache_key(b"abc", "html_extract", "m1", "promptA")
    k3 = _cache_key(b"abc", "html_extract", "m1", "promptB")
    assert k1 == k2
    assert k1 != k3
    assert isinstance(CACHE_DIR, str)
```

- [ ] **Step 2: Run the test**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_cache.py -v`
Expected: PASS (both `CACHE_DIR` and `_cache_key` already exist and are importable). If the file does not exist, create it with exactly the code above.

- [ ] **Step 3: Commit**

```bash
git add table_extractor/tests/test_cache.py
git commit -m "test: confirm cache exports CACHE_DIR and _cache_key are importable"
```

---

## Task 4: Idempotent, cached, retrying page analysis (`llm.py`)

**Files:**
- Modify: `crop_app/llm.py`
- Test: `crop_app/tests/test_llm.py`

**Interfaces:**
- Produces: `analyze_page(image_path: str) -> dict` returning `{"classification": "Simple"|"Complex"|None, "error": str|None}`. On API failure returns `{"classification": None, "error": ...}` (NEVER `"Complex"`). Malformed JSON raises `MalformedOutputError` inside the retry wrapper, which after exhaustion returns `{"classification": None, "error": "Malformed output: ..."}`.
- Consumes: `table_extractor.cache.cached_call`, `table_extractor.retry.retry_with_backoff`, `table_extractor.retry.MalformedOutputError`, `table_extractor.retry.PipelineCallError`.

- [ ] **Step 1: Write the failing tests**

Append to `crop_app/tests/test_llm.py`:

```python
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from llm import analyze_page, _parse_response_strict
from table_extractor.retry import MalformedOutputError


def test_analyze_page_returns_none_classification_on_api_failure(tmp_path):
    img = os.path.join(str(tmp_path), "p.png")
    from PIL import Image
    Image.new("RGB", (10, 10)).save(img)

    def boom():
        raise RuntimeError("network down")

    with patch("llm._get_client") as get_client:
        get_client.return_value.chat.completions.create.side_effect = boom
        result = analyze_page(img)
    assert result["classification"] is None
    assert "network down" in result["error"]


def test_parse_response_strict_raises_on_bad_json():
    with pytest.raises(MalformedOutputError):
        _parse_response_strict("not json at all <<<")


def test_parse_response_strict_raises_on_invalid_classification():
    with pytest.raises(MalformedOutputError):
        _parse_response_strict('{"classification": "Maybe"}')


def test_parse_response_strict_accepts_valid(tmp_path):
    img = os.path.join(str(tmp_path), "p.png")
    from PIL import Image
    Image.new("RGB", (10, 10)).save(img)
    with patch("llm._get_client") as get_client:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"classification": "Simple"}'
        get_client.return_value.chat.completions.create.return_value = resp
        result = analyze_page(img)
    assert result["classification"] == "Simple"
```

Add import at top of `test_llm.py` if missing: `import pytest`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_llm.py -v`
Expected: FAIL (analyze_page still returns `"Complex"` on error; `_parse_response_strict` does not exist).

- [ ] **Step 3: Implement**

Replace the body of `analyze_page` and `_parse_response` in `crop_app/llm.py` with:

```python
import io
import json
import base64
import os
from openai import OpenAI
from PIL import Image
from table_extractor.cache import cached_call
from table_extractor.retry import (
    retry_with_backoff,
    MalformedOutputError,
    PipelineCallError,
)
```

Keep `_load_env()`, `_get_client()`, `MODEL_ID`, `ANALYSIS_PROMPT` unchanged.

```python
def analyze_page(image_path: str) -> dict:
    """Send a page image to the LLM and return classification.

    Returns: {"classification": "Simple"|"Complex"|None, "error": str|None}
    On API failure returns classification None (never "Complex").
    """
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    image_bytes = buf.getvalue()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    def _call_api():
        """Inner API call, wrapped in retry_with_backoff."""
        response = _get_client().chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": ANALYSIS_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]}
            ],
            reasoning_effort="minimal",
        )
        raw_content = response.choices[0].message.content or ""
        return _parse_response_strict(raw_content)

    try:
        result = cached_call(
            image_bytes=image_bytes,
            stage="analyze",
            model=MODEL_ID,
            fn=lambda: [retry_with_backoff(_call_api)],
            force=False,
            extra_key=ANALYSIS_PROMPT,
        )
        return result[0]
    except PipelineCallError as e:
        return {"classification": None, "error": e.message}
    except Exception as e:
        return {"classification": None, "error": str(e)}


def _parse_response_strict(raw: str) -> dict:
    """Parse LLM JSON response. Raises MalformedOutputError on failure."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise MalformedOutputError(f"Failed to parse JSON: {raw[:200]}")

    if not isinstance(data, dict):
        raise MalformedOutputError(f"Expected JSON object: {raw[:200]}")

    classification = data.get("classification")
    if classification not in ("Simple", "Complex"):
        raise MalformedOutputError(f"Invalid classification value: {raw[:200]}")

    return {"classification": classification, "error": None}
```

Delete the old `_parse_response` function (replaced by `_parse_response_strict`). Keep `analyze_pages` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_llm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crop_app/llm.py crop_app/tests/test_llm.py
git commit -m "feat: analyze_page uses cached_call + retry_with_backoff, returns None (not Complex) on failure"
```

---

## Task 5: Persistent `next_crop_id` counter (`crop_manager.py`)

**Files:**
- Modify: `crop_app/crop_manager.py`
- Test: `crop_app/tests/test_crop_manager.py`

**Interfaces:**
- Produces: `CropManager.save_crop(session_id, page_path, normalized_bbox, filename=None) -> str`. When `filename` is given, it is used verbatim (so `app.py` owns `next_crop_id`). When `filename` is `None`, behavior falls back to the legacy file-count counter (kept only for any non-`app.py` callers; `app.py` will always pass `filename`).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `crop_app/tests/test_crop_manager.py`:

```python
def test_save_crop_uses_explicit_filename(tmp_path):
    cm = CropManager(str(tmp_path / "crops"))
    page = str(tmp_path / "page.png")
    from PIL import Image
    Image.new("RGB", (40, 40)).save(page)
    path = cm.save_crop("s1", page, [0, 0, 1, 1], filename="crop_003.png")
    assert path.endswith("crop_003.png")
    assert os.path.exists(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_crop_manager.py -v`
Expected: FAIL (TypeError: save_crop() got unexpected keyword argument 'filename').

- [ ] **Step 3: Implement**

In `crop_app/crop_manager.py`, change `save_crop` signature and body:

```python
    def save_crop(self, session_id: str, page_path: str, normalized_bbox: list, filename: str = None) -> str:
        """Extract a crop and save to crops/<session_id>/<filename> (or crop_NNN.png if filename omitted).

        Returns the absolute path to the saved file.
        """
        crop_img = self.extract_crop(page_path, normalized_bbox)
        crop_dir = os.path.join(self.crop_root, session_id)
        os.makedirs(crop_dir, exist_ok=True)
        if filename is None:
            idx = self._next_crop_index(session_id)
            filename = f"crop_{idx:03d}.png"
        filepath = os.path.join(crop_dir, filename)
        crop_img.save(filepath, "PNG")
        return filepath
```

Leave `_next_crop_index` unchanged (still used as fallback).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_crop_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crop_app/crop_manager.py crop_app/tests/test_crop_manager.py
git commit -m "feat: CropManager.save_crop accepts explicit filename for persistent crop ids"
```

---

## Task 6: Pure task-model functions (`html_extractor.py`)

**Files:**
- Modify: `table_extractor/html_extractor.py` (add functions; do NOT yet remove `run_extraction`)
- Test: `table_extractor/tests/test_task_model.py` (NEW)

**Interfaces:**
- Produces:
  - `derive_required_tasks(meta: dict) -> list[dict]`
  - `reconcile_tasks(meta: dict, desired_tasks: list, fragments_dir: str) -> None`  **(fragments_dir is a required positional argument — there is NO module-level default; callers must pass the real `sm.get_extraction_fragments_dir(session_id)`)**
  - `on_crop_mutation(meta: dict, sm, session_id: str, output_dir: str) -> None`
  - `_is_extraction_in_progress(session_id) -> bool`, `_set_extraction_in_progress(session_id)`, `_clear_extraction_in_progress(session_id)`
- Consumes: `SessionManager.get_extraction_fragments_dir`, `SessionManager` (for `on_crop_mutation`), `os`.

- [ ] **Step 1: Write the failing tests**

Create `table_extractor/tests/test_task_model.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_task_model.py -v`
Expected: ERROR (import fails — functions do not exist).

- [ ] **Step 3: Implement**

Add to `table_extractor/html_extractor.py` (imports: add `import os`, `import threading`, and `from table_extractor.retry import PipelineCallError` if not present; add `from session_manager import SessionManager` is NOT needed here — `on_crop_mutation` receives `sm`):

```python
import os
import threading


# --- Extraction-in-progress guard (module level) ---
_extraction_in_progress = set()
_extraction_lock = threading.Lock()


def _is_extraction_in_progress(session_id: str) -> bool:
    with _extraction_lock:
        return session_id in _extraction_in_progress


def _set_extraction_in_progress(session_id: str):
    with _extraction_lock:
        _extraction_in_progress.add(session_id)


def _clear_extraction_in_progress(session_id: str):
    with _extraction_lock:
        _extraction_in_progress.discard(session_id)


def _remove_output_marker(session_id: str, output_dir: str) -> None:
    session_dir = os.path.join(output_dir, session_id)
    marker_path = os.path.join(session_dir, ".complete")
    if os.path.exists(marker_path):
        os.unlink(marker_path)


def on_crop_mutation(meta: dict, sm, session_id: str, output_dir: str) -> None:
    """Invalidate stale fragments/tasks after a crop mutation. In-place on meta."""
    if _is_extraction_in_progress(session_id):
        raise RuntimeError("Extraction job is running — cancel it or wait before mutating crops")
    desired_tasks = derive_required_tasks(meta)
    desired_ids = {t["task_id"] for t in desired_tasks}
    existing_tasks = meta.get("extraction_tasks", [])
    fragments_dir = sm.get_extraction_fragments_dir(session_id)

    for task in existing_tasks:
        if task["task_id"] not in desired_ids:
            frag_path = task.get("fragment_path")
            if frag_path:
                full_path = os.path.join(fragments_dir, os.path.basename(frag_path))
                if os.path.exists(full_path):
                    os.unlink(full_path)

    existing_by_id = {t["task_id"]: t for t in existing_tasks}
    final = []
    for desired in desired_tasks:
        tid = desired["task_id"]
        if tid in existing_by_id:
            final.append(existing_by_id[tid])
        else:
            final.append({
                **desired, "extraction_status": "pending", "extraction_error": None,
                "extraction_error_type": None, "fragment_path": None,
            })
    meta["extraction_tasks"] = final
    _remove_output_marker(session_id, output_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_task_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_extractor.py table_extractor/tests/test_task_model.py
git commit -m "feat: add derive_required_tasks, reconcile_tasks, on_crop_mutation, in-progress guard"
```

---

## Task 7: Blank-response detection + stale cache bypass (`html_extractor.py`)

**Files:**
- Modify: `table_extractor/html_extractor.py` (`extract_crop_as_html`)
- Test: `table_extractor/tests/test_blank_cache.py` (NEW)

**Interfaces:**
- Produces: `extract_crop_as_html(crop_image: PIL.Image.Image, model: str) -> str` — now raises `BlankResponseError` (retryable) when the cleaned fragment is blank, and deletes a stale blank cache entry then re-calls so a fresh non-blank result is cached.
- Consumes: `table_extractor.cache._cache_key`, `table_extractor.cache.CACHE_DIR`, `table_extractor.retry.BlankResponseError`, `table_extractor.retry.is_blank_fragment`, `table_extractor.retry.retry_with_backoff`.

- [ ] **Step 1: Write the failing test**

Create `table_extractor/tests/test_blank_cache.py`:

```python
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import patch
from PIL import Image
from table_extractor.html_extractor import extract_crop_as_html
from table_extractor.cache import CACHE_DIR, _cache_key
from table_extractor.retry import BlankResponseError


def _make_img():
    return Image.new("RGB", (20, 20), "green")


def test_blank_response_raises_and_retries_then_succeeds():
    calls = {"n": 0}

    def fake_call():
        calls["n"] += 1
        if calls["n"] == 1:
            return ["", {}]  # blank
        return ["<p>real</p>", {}]

    with patch("table_extractor.html_extractor._get_client") as gc, \
         patch("table_extractor.html_extractor.load_full_prompt", return_value="P"):
        resp = __import__("unittest.mock").MagicMock()
        resp.choices = [__import__("unittest.mock").MagicMock()]
        resp.usage = None
        gc.return_value.chat.completions.create.return_value = resp
        # Force _call to use our fake_call via cached_call's fn
        with patch("table_extractor.html_extractor.cached_call", side_effect=lambda **kw: kw["fn"]()):
            result = extract_crop_as_html(_make_img(), "m")
    assert result == "<p>real</p>"


def test_stale_blank_cache_deleted_and_overwritten(tmp_path):
    # Pre-populate a blank cache entry for a given image+model+prompt
    img = _make_img()
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    key = _cache_key(img_bytes, "html_extract", "m", "PROMPT")
    cache_file = os.path.join(CACHE_DIR, f"{key}.json")
    with open(cache_file, "w") as f:
        json.dump(["", {}], f)

    def fake_call():
        return ["<p>fresh</p>", {}]

    with patch("table_extractor.html_extractor.load_full_prompt", return_value="PROMPT"), \
         patch("table_extractor.html_extractor.cached_call", side_effect=lambda **kw: kw["fn"]()):
        result = extract_crop_as_html(img, "m")

    assert result == "<p>fresh</p>"
    # Stale blank cache file removed (re-call did not re-create it as blank)
    assert not os.path.exists(cache_file) or json.load(open(cache_file))[0] == "<p>fresh</p>"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_blank_cache.py -v`
Expected: FAIL (blank responses are silently returned, no `BlankResponseError`, no cache deletion).

- [ ] **Step 3: Implement**

Replace the `extract_crop_as_html` body's `_call` and surrounding logic:

```python
from table_extractor.cache import cached_call, CACHE_DIR, _cache_key
from table_extractor.retry import (
    retry_with_backoff,
    BlankResponseError,
    is_blank_fragment,
)
```

```python
def extract_crop_as_html(crop_image: Image.Image, model: str) -> str:
    """Send one crop image to the LLM and return an HTML fragment string."""
    system_prompt = load_full_prompt()

    buf = io.BytesIO()
    crop_image.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    def _call():
        """Make the cached LLM call for this crop."""
        response = _get_client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
            ],
            max_tokens=8192,
        )
        raw_content = response.choices[0].message.content or ""
        html_fragment = clean_up_html_fragment(raw_content)
        if is_blank_fragment(html_fragment):
            raise BlankResponseError("LLM returned an empty/blank HTML fragment")
        usage_meta = {}
        if response.usage:
            usage_meta = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        return [html_fragment, usage_meta]

    def _cached_extract():
        return cached_call(
            image_bytes=img_bytes,
            stage="html_extract",
            model=model,
            fn=lambda: retry_with_backoff(_call),
            force=False,
            extra_key=system_prompt,
        )

    result = _cached_extract()

    # Stale blank cache invalidation
    if is_blank_fragment(result[0]):
        cache_key = _cache_key(img_bytes, "html_extract", model, system_prompt)
        cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
        if os.path.exists(cache_file):
            os.unlink(cache_file)
        result = _cached_extract()

    return result[0]
```

Note: `cached_call` already exists and is referenced; ensure it is imported. Previously it was imported as `from table_extractor.cache import cached_call`. Keep that import and add `CACHE_DIR, _cache_key`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_blank_cache.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_extractor.py table_extractor/tests/test_blank_cache.py
git commit -m "feat: blank-response detection + stale blank cache deletion in extract_crop_as_html"
```

---

## Task 8: Background `ExtractionJob`, assembly, `.complete` marker, job registry (`html_extractor.py`)

**Files:**
- Modify: `table_extractor/html_extractor.py` (add `ExtractionJob`, registry, helpers; **remove `run_extraction`**)
- Test: `table_extractor/tests/test_extraction_job.py` (NEW)

**Interfaces:**
- Produces:
  - `ExtractionJob(session_id, sm, crop_root, page_dir, output_dir, model, max_workers=4, retry_nonretryable=False)`
  - `ExtractionJob.run()` (background thread entry)
  - `_start_extraction_job(session_id, sm, crop_root, page_dir, output_dir, model, max_workers=4, retry_nonretryable=False) -> ExtractionJob` (raises `RuntimeError` "Job already running for session" if one is live)
  - `_get_active_job(session_id) -> ExtractionJob | None`
  - `_cleanup_completed_jobs()`
  - `_run_assembly(self, meta)` (deletes `.complete`, reads fragments, calls `write_page_files`, writes `.complete`)
  - `_output_complete(session_id) -> bool`, `_write_complete_marker(...)`, `_remove_output_marker(...)` (already added in Task 6 partly; complete here)
- Consumes: `derive_required_tasks`, `reconcile_tasks`, `SessionManager` methods, `write_page_files` from `table_extractor.html_assembler`, `extract_crop_as_html`, `PipelineCallError`, `_extract_task`.

- [ ] **Step 1: Write the failing tests**

Create `table_extractor/tests/test_extraction_job.py`:

```python
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import patch
from PIL import Image

from table_extractor.html_extractor import (
    ExtractionJob,
    _start_extraction_job,
    _get_active_job,
    _cleanup_completed_jobs,
    _output_complete,
    derive_required_tasks,
    reconcile_tasks,
)


class _FakeSM:
    def __init__(self, base):
        self.base = base
        self._store = {}

    def get_session_dir(self, sid):
        return os.path.join(self.base, sid)

    def get_page_dir(self, sid):
        return os.path.join(self.base, sid, "pages")

    def get_extraction_fragments_dir(self, sid):
        d = os.path.join(self.base, sid, "extraction_fragments")
        os.makedirs(d, exist_ok=True)
        return d

    def load_meta(self, sid):
        return self._store[sid]

    def save_meta_atomic(self, sid, meta):
        self._store[sid] = meta

    def metadata_lock(self, sid):
        import threading
        return threading.Lock()


def _make_session(base, sid, pages, analyzed=True):
    session_dir = os.path.join(base, sid)
    os.makedirs(os.path.join(session_dir, "pages"), exist_ok=True)
    meta = {"files": ["t.pdf"], "pages": pages, "extraction_tasks": []}
    if analyzed:
        for p in pages:
            p.setdefault("analysis_status", "done")
    sm = _FakeSM(base)
    sm._store[sid] = meta
    return sm, meta


def test_job_extracts_all_and_writes_complete(tmp_path):
    sid = "job1"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    # The job writes .complete under the global output root; point it at the test dir.
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>frag</p>"):
        job = _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                    str(tmp_path / sid / "pages"),
                                    str(tmp_path / "out"), "m")
        import time
        job.done_event.wait(timeout=10)
    assert _output_complete(sid) is True
    out_dir = os.path.join(str(tmp_path / "out"), sid)
    assert os.path.exists(os.path.join(out_dir, "page-0.html"))
    assert os.path.exists(os.path.join(out_dir, "index.html"))


def test_job_resumes_skipping_extracted(tmp_path):
    sid = "job2"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    # Pre-write a fragment + mark task extracted
    frag = os.path.join(sm.get_extraction_fragments_dir(sid), "page-0.html")
    open(frag, "w").write("<p>already</p>")
    desired = derive_required_tasks(meta)
    reconcile_tasks(meta, desired, sm.get_extraction_fragments_dir(sid))
    meta["extraction_tasks"][0]["extraction_status"] = "extracted"
    meta["extraction_tasks"][0]["fragment_path"] = "extraction_fragments/page-0.html"
    sm.save_meta_atomic(sid, meta)

    calls = {"n": 0}

    def fake(img, model):
        calls["n"] += 1
        return "<p>new</p>"

    with patch("table_extractor.html_extractor.extract_crop_as_html", fake):
        job = _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                    str(tmp_path / sid / "pages"),
                                    str(tmp_path / "out"), "m")
        import time
        job.done_event.wait(timeout=10)
    assert calls["n"] == 0  # skipped because already extracted
    out_dir = os.path.join(str(tmp_path / "out"), sid)
    with open(os.path.join(out_dir, "page-0.html")) as f:
        assert "already" in f.read()


def test_duplicate_job_rejected(tmp_path):
    sid = "job3"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                              str(tmp_path / sid / "pages"),
                              str(tmp_path / "out"), "m")
        try:
            _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                  str(tmp_path / sid / "pages"),
                                  str(tmp_path / "out"), "m")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "already running" in str(e)
    import time
    j = _get_active_job(sid)
    j.done_event.wait(timeout=10)


def test_cleanup_completed_jobs_removes_registry_entry(tmp_path):
    sid = "job4"
    page = os.path.join(str(tmp_path), sid, "pages", "page_000.png")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    Image.new("RGB", (40, 40)).save(page)
    sm, meta = _make_session(str(tmp_path), sid, [
        {"path": "page_000.png", "classification": "Simple", "crops": []},
    ])
    from table_extractor.html_extractor import set_output_root
    set_output_root(str(tmp_path / "out"))
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        job = _start_extraction_job(sid, sm, str(tmp_path / "crops"),
                                    str(tmp_path / sid / "pages"),
                                    str(tmp_path / "out"), "m")
        import time
        job.done_event.wait(timeout=10)
        _cleanup_completed_jobs()
        assert _get_active_job(sid) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_extraction_job.py -v`
Expected: ERROR (ExtractionJob / _start_extraction_job not defined).

- [ ] **Step 3: Implement**

Append to `table_extractor/html_extractor.py`. First add imports at top: `import concurrent.futures`, `import weakref`, `import tempfile`, `import time`, `from table_extractor.html_assembler import write_page_files`, `from table_extractor.retry import PipelineCallError`.

```python
import concurrent.futures
import tempfile
import time
import weakref


class ExtractionInProgressError(RuntimeError):
    pass


def _write_complete_marker(session_output_dir: str) -> None:
    """Write .complete marker atomically via temp file + os.replace."""
    fd, tmp_path = tempfile.mkstemp(dir=session_output_dir, suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time()}, f)
        os.replace(tmp_path, os.path.join(session_output_dir, ".complete"))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _output_complete(session_id: str) -> bool:
    session_output_dir = os.path.join(_OUTPUT_ROOT, session_id)
    return os.path.exists(os.path.join(session_output_dir, ".complete"))


# Module-level output root, set by app.py at startup via set_output_root().
_OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crop_app", "static", "extracted")


def set_output_root(output_root: str) -> None:
    """Allow app.py to set the extracted output root."""
    global _OUTPUT_ROOT
    _OUTPUT_ROOT = output_root


class ExtractionJob:
    """Background extraction job for a session."""

    def __init__(self, session_id, sm, crop_root, page_dir, output_dir, model,
                 max_workers=4, retry_nonretryable=False):
        self.session_id = session_id
        self.sm = sm
        self.crop_root = crop_root
        self.page_dir = page_dir
        self.output_dir = output_dir
        self.model = model
        self.max_workers = max_workers
        self.retry_nonretryable = retry_nonretryable
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        self.abort_flag = threading.Event()
        self.result = None
        self.error_message = None
        self.error_type = None

    def run(self):
        try:
            self._execute_extraction()
            if self.result is None:
                self.result = "error"
                self.error_message = "Job returned without setting outcome"
                self.error_type = "retryable"
        except Exception as e:
            self.result = "error"
            self.error_message = str(e)
            self.error_type = "retryable"
        finally:
            _clear_extraction_in_progress(self.session_id)
            self.done_event.set()
            self.executor.shutdown(wait=False)

    def _run_assembly(self, meta):
        output_dir = self.output_dir
        fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)
        session_output_dir = os.path.join(output_dir, self.session_id)
        os.makedirs(session_output_dir, exist_ok=True)
        _remove_output_marker(self.session_id, output_dir)

        pages_data = []
        for page_idx, page_info in enumerate(meta.get("pages", [])):
            page_tasks = [
                t for t in meta.get("extraction_tasks", [])
                if t["page_idx"] == page_idx and t["extraction_status"] == "extracted"
            ]
            fragments = []
            for task in page_tasks:
                frag_path = os.path.join(fragments_dir, os.path.basename(task["fragment_path"]))
                with open(frag_path, "r", encoding="utf-8") as f:
                    fragments.append(f.read())
            pages_data.append({"html": "\n".join(fragments)})

        session_files = meta.get("files", [])
        title = session_files[0] if session_files else f"Session {self.session_id[:8]}"
        write_page_files(self.session_id, pages_data, title, output_root=output_dir)
        _write_complete_marker(session_output_dir)

    def _execute_extraction(self):
        with self.sm.metadata_lock(self.session_id):
            meta = self.sm.load_meta(self.session_id)
            desired = derive_required_tasks(meta)
            reconcile_tasks(meta, desired, self.sm.get_extraction_fragments_dir(self.session_id))
            self.sm.save_meta_atomic(self.session_id, meta)

        meta = self.sm.load_meta(self.session_id)
        tasks = meta.get("extraction_tasks", [])

        def _eligible(task):
            if task["extraction_status"] == "extracted":
                return False
            if not self.retry_nonretryable and task["extraction_status"] == "failed" \
               and task.get("extraction_error_type") in ("auth", "credits"):
                return False
            return True

        tasks_to_run = [t for t in tasks if _eligible(t)]

        if not tasks_to_run:
            failed = [t for t in tasks if t["extraction_status"] == "failed"]
            if failed:
                terminal = next((t for t in failed if t["extraction_error_type"] in ("auth", "credits")), failed[0])
                self.result = "error"
                self.error_message = terminal.get("extraction_error") or "Tasks failed"
                self.error_type = terminal.get("extraction_error_type") or "retryable"
                return
            self._run_assembly(meta)
            self.result = "done"
            return

        max_workers = min(self.max_workers, len(tasks_to_run))
        if max_workers <= 0:
            self.result = "error"
            self.error_message = "No eligible tasks"
            self.error_type = "retryable"
            return

        semaphore = threading.Semaphore(max_workers)
        futures = []
        for task in tasks_to_run:
            if self.cancel_event.is_set() or self.abort_flag.is_set():
                break
            semaphore.acquire()
            if self.cancel_event.is_set() or self.abort_flag.is_set():
                semaphore.release()
                break
            future = self.executor.submit(self._extract_task, task, semaphore)
            futures.append(future)

        for future in futures:
            try:
                future.result()
            except PipelineCallError:
                pass
            except Exception:
                pass

        if self.cancel_event.is_set() and not self.abort_flag.is_set():
            self.result = "cancelled"
            return

        meta = self.sm.load_meta(self.session_id)
        failed = [t for t in meta["extraction_tasks"] if t["extraction_status"] == "failed"]
        if failed:
            auth_fail = [t for t in failed if t["extraction_error_type"] in ("auth", "credits")]
            if auth_fail:
                self.result = "error"
                self.error_message = auth_fail[0]["extraction_error"]
                self.error_type = auth_fail[0]["extraction_error_type"]
            else:
                self.result = "error"
                self.error_message = f"{len(failed)} task(s) failed"
                self.error_type = failed[0]["extraction_error_type"] or "retryable"
            return

        self._run_assembly(meta)
        self.result = "done"

    def _extract_task(self, task, semaphore):
        try:
            meta = self.sm.load_meta(self.session_id)
            page_info = meta["pages"][task["page_idx"]]
            if task["kind"] == "page":
                page_path = os.path.join(self.page_dir, page_info["path"])
                with Image.open(page_path) as img:
                    crop_img = img.copy()
            else:
                crop_path = os.path.join(self.crop_root, self.session_id, task["crop_filename"])
                with Image.open(crop_path) as img:
                    crop_img = img.copy()

            html_fragment = extract_crop_as_html(crop_img, self.model)
            fragments_dir = self.sm.get_extraction_fragments_dir(self.session_id)
            fragment_path = os.path.join(fragments_dir, f"{task['task_id']}.html")
            _write_file_atomic(fragment_path, html_fragment)

            with self.sm.metadata_lock(self.session_id):
                meta = self.sm.load_meta(self.session_id)
                for t in meta.get("extraction_tasks", []):
                    if t["task_id"] == task["task_id"]:
                        t["extraction_status"] = "extracted"
                        t["fragment_path"] = f"extraction_fragments/{task['task_id']}.html"
                        t["extraction_error"] = None
                        t["extraction_error_type"] = None
                self.sm.save_meta_atomic(self.session_id, meta)

        except PipelineCallError as e:
            with self.sm.metadata_lock(self.session_id):
                meta = self.sm.load_meta(self.session_id)
                for t in meta.get("extraction_tasks", []):
                    if t["task_id"] == task["task_id"]:
                        t["extraction_status"] = "failed"
                        t["extraction_error"] = e.message
                        t["extraction_error_type"] = e.error_type
                self.sm.save_meta_atomic(self.session_id, meta)
            if e.error_type in ("auth", "credits"):
                self.abort_flag.set()

        except Exception as e:
            with self.sm.metadata_lock(self.session_id):
                meta = self.sm.load_meta(self.session_id)
                for t in meta.get("extraction_tasks", []):
                    if t["task_id"] == task["task_id"]:
                        t["extraction_status"] = "failed"
                        t["extraction_error"] = str(e)
                        t["extraction_error_type"] = "retryable"
                self.sm.save_meta_atomic(self.session_id, meta)

        finally:
            semaphore.release()


def _write_file_atomic(path: str, content: str) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".html.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Job registry ---
_active_jobs = {}
_jobs_lock = threading.Lock()


def _get_active_job(session_id):
    with _jobs_lock:
        return _active_jobs.get(session_id)


def _start_extraction_job(session_id, sm, crop_root, page_dir, output_dir,
                          model, max_workers=4, retry_nonretryable=False):
    """Create and start a new extraction job. Atomically registers in-progress state."""
    with _jobs_lock:
        existing = _active_jobs.get(session_id)
        if existing and not existing.done_event.is_set():
            raise ExtractionInProgressError("Job already running for session")
        job = ExtractionJob(
            session_id=session_id, sm=sm, crop_root=crop_root, page_dir=page_dir,
            output_dir=output_dir, model=model, max_workers=max_workers,
            retry_nonretryable=retry_nonretryable,
        )
        _active_jobs[session_id] = job
        _set_extraction_in_progress(session_id)
        thread = threading.Thread(target=job.run, name=f"extract-{session_id[:8]}", daemon=True)
        thread.start()
        return job


def _cleanup_completed_jobs():
    """Drop finished jobs from the registry so SSE observers read disk state."""
    with _jobs_lock:
        for sid in list(_active_jobs.keys()):
            job = _active_jobs[sid]
            if job.done_event.is_set():
                del _active_jobs[sid]
```

Now **remove `run_extraction`** entirely from `html_extractor.py` (it is replaced by `ExtractionJob`). Search for `def run_extraction` and delete through its end. Also remove the now-unused `_get_classification`/`_get_sorted_crops` only if nothing else uses them — they are used by `run_extraction`, so safe to remove. Leave `clean_up_html_fragment`, `load_prompt`, `load_full_prompt`, `extract_crop_as_html` intact.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_extraction_job.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_extractor.py table_extractor/tests/test_extraction_job.py
git commit -m "feat: ExtractionJob background thread, bounded submission, assembly, .complete marker, job registry"
```

---

## Task 9: App integration (`crop_app/app.py`)

**Files:**
- Modify: `crop_app/app.py`
- Test: `crop_app/tests/test_extract_routes.py` (extend), `crop_app/tests/test_analysis.py` (extend)

**Interfaces:**
- Consumes: `SessionManager.save_meta_atomic`, `SessionManager.metadata_lock`, `SessionManager.get_extraction_fragments_dir`, `from table_extractor.html_extractor import ExtractionJob, _start_extraction_job, _get_active_job, _output_complete, set_output_root, derive_required_tasks, on_crop_mutation, _is_extraction_in_progress, reconcile_tasks`, `table_extractor.html_extractor.ExtractionInProgressError`.
- Produces: new `POST /extract-html/<session_id>` start route; rewritten `GET /extract-progress/<session_id>` SSE (observational); `.complete` checks in `/extracted/...`; `next_crop_id` handling in `POST /commit`; `on_crop_mutation` in `/commit`, `/trim`, `/delete-crop`; analyze route persists per-page immediately with new `analysis_status` field; `POST /analyze/<session_id>` that processes only pending/error pages.

- [ ] **Step 1: Write the failing tests**

Extend `crop_app/tests/test_extract_routes.py` (or create it if missing) with:

```python
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager


def test_post_extract_html_requires_analysis_done(client, tmp_path):
    sm = SessionManager(str(tmp_path / "up"), str(tmp_path / "cr"))
    sid = sm.create_session()
    sm.save_meta_atomic(sid, {"files": ["t.pdf"], "pages": [
        {"path": "p0.png", "analysis_status": "pending", "classification": None, "crops": []}
    ], "extraction_tasks": []})
    # patch app's sm
    app = create_app()
    app.session_manager = sm
    app.config["EXTRACTED_DIR"] = str(tmp_path / "out")
    c = app.test_client()
    r = c.post(f"/extract-html/{sid}")
    assert r.status_code == 400
```

(Write additional route tests in later steps; start with this to pin the prerequisite.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_extract_routes.py -v`
Expected: FAIL (route does not exist / wrong behavior).

- [ ] **Step 3: Implement app.py changes**

**REQUIRED FIRST STEP — remove the now-dead import.** Task 8 deletes `run_extraction` from `table_extractor/html_extractor.py`. The existing module-level import at `crop_app/app.py:35`:

```python
from table_extractor.html_extractor import run_extraction
```

will raise `ImportError` at app startup once Task 8 lands. **Delete that exact line.** Do NOT replace it with anything at module scope — the new symbols are imported inside `create_app` (below).

Then, at top of `create_app`, after creating `sm`, add:

```python
from table_extractor.html_extractor import (
    ExtractionJob,
    _start_extraction_job,
    _get_active_job,
    _output_complete,
    set_output_root,
    derive_required_tasks,
    on_crop_mutation,
    reconcile_tasks,
    ExtractionInProgressError,
)
set_output_root(app.config["EXTRACTED_DIR"])
```

**Replace** the `analyze_session` route so it persists per-page immediately with `analysis_status` and processes only pending/error pages:

```python
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
            status = page_info.get("analysis_status")
            if status == "done":
                continue
            page_path = os.path.join(page_dir, page_info["path"])
            if not os.path.exists(page_path):
                page_info["analysis_status"] = "error"
                page_info["analysis_error"] = "Page file missing"
                updated = True
                continue
            result = analyze_page(page_path)
            if result.get("classification") is None:
                page_info["analysis_status"] = "error"
                page_info["analysis_error"] = result.get("error")
            else:
                page_info["analysis_status"] = "done"
                page_info["classification"] = result["classification"]
                page_info["analysis_error"] = None
                if result["classification"] == "Complex" and page_info.get("pdf_path") and page_info.get("pdf_page") is not None:
                    try:
                        pdf_full_path = os.path.join(session_dir, page_info["pdf_path"])
                        upgrade_page_to_hires(pdf_full_path, page_path, page_info["pdf_page"])
                        page_info["upgraded"] = True
                    except Exception as e:
                        page_info["upgrade_error"] = str(e)
            updated = True
            with _sm.metadata_lock(session_id):
                _sm.save_meta_atomic(session_id, meta)
        return jsonify(meta)
```

**Replace** the `commit_crops` route to manage `next_crop_id` and run `on_crop_mutation`:

```python
    @app.route("/commit/<session_id>", methods=["POST"])
    def commit_crops(session_id):
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
```

**Replace** the `trim_crop` route:

```python
    @app.route("/trim/<session_id>/<crop_filename>", methods=["POST"])
    def trim_crop(session_id, crop_filename):
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
            cm = CropManager(app.config["CROP_DIR"])
            session_crop_dir = os.path.join(cm.crop_root, session_id)
            crop_path = os.path.join(session_crop_dir, crop_filename)
            if not os.path.exists(crop_path):
                return jsonify({"error": "Crop not found"}), 404
            cm.trim_crop(crop_path, data["bbox"])
            on_crop_mutation(meta, _sm, session_id, app.config["EXTRACTED_DIR"])
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"path": crop_path, "filename": crop_filename})
```

**Replace** the `delete_crop` route:

```python
    @app.route("/delete-crop/<session_id>", methods=["POST"])
    def delete_crop(session_id):
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
            page_info["crops"] = [c for c in page_info.get("crops", []) if c.get("filename") != filename]
            removed = before - len(page_info["crops"])
            on_crop_mutation(meta, _sm, session_id, app.config["EXTRACTED_DIR"])
            _sm.save_meta_atomic(session_id, meta)
        return jsonify({"ok": True, "removed": removed})
```

**Replace** the `extract_html_page` GET route (render page) — remove crop prerequisite, keep draft check, add `.complete` redirect:

```python
    @app.route("/extract-html/<session_id>", methods=["GET"])
    def extract_html_page(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return render_template("error.html", message="Session not found"), 400
        meta = _sm.load_meta(session_id)
        for page in meta.get("pages", []):
            if page.get("draft") and len(page["draft"]) > 0:
                return render_template("error.html", message="You have uncommitted changes. Please commit them before extracting HTML."), 400
        analyzed = all(p.get("analysis_status") == "done" for p in meta.get("pages", []))
        if not analyzed:
            return render_template("error.html", message="Please analyze all pages before extracting HTML."), 400
        if _output_complete(session_id):
            return redirect(f"/extracted/{session_id}/extraction.html")
        return render_template("extract_progress.html", session_id=session_id)
```

**Add** the new `POST /extract-html/<session_id>` start route (place it right after the GET route above):

```python
    @app.route("/extract-html/<session_id>", methods=["POST"])
    def start_extraction(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return jsonify({"error": "Session not found"}), 404
        retry_nonretryable = request.args.get("retry_nonretryable", "false") == "true"
        meta = _sm.load_meta(session_id)
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
        try:
            _start_extraction_job(
                session_id, _sm, app.config["CROP_DIR"], _sm.get_page_dir(session_id),
                app.config["EXTRACTED_DIR"], os.environ["DATA_EXTRACTION_MODEL_ID"],
                retry_nonretryable=retry_nonretryable,
            )
        except ExtractionInProgressError:
            return jsonify({"status": "error", "message": "Extraction already running"}), 409
        return jsonify({"status": "started"})
```

**Replace** the `extract_progress_sse` GET route with the observational version:

```python
    @app.route("/extract-progress/<session_id>", methods=["GET"])
    def extract_progress_sse(session_id):
        _sm = app.session_manager
        if not _sm.session_exists(session_id):
            return "Session not found", 404

        def generate():
            yield _sse_event({"status": "starting"})
            while True:
                meta = _sm.load_meta(session_id)
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
                    if total == 0:
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
                    return
                yield _sse_event({
                    "status": "progress", "progress": completed, "total": total,
                    "log": f"Extracted {completed}/{total} regions...",
                })
                job.done_event.wait(timeout=0.5)

        return Response(generate(), mimetype="text/event-stream")
```

Add the helper at module scope (outside `create_app`):

```python
def _sse_event(payload: dict) -> str:
    import json
    return f"data: {json.dumps(payload)}\n\n"
```

**Replace the `list_sessions` route** so each session dict carries `analysis_status` and `extraction_status` (consumed by `sessions.html` in Task 10). Replace the body of the `for sid in _sm.list_sessions():` loop (currently builds a dict with `id`, `name`, `files`, `page_count`, `crop_count`, `uploaded_at`) with:

```python
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
            sessions.append({
                "id": sid, "name": name, "files": files,
                "page_count": page_count, "crop_count": crop_count,
                "uploaded_at": os.path.getmtime(session_dir),
                "analysis_status": analysis_status,
                "extraction_status": extraction_status,
            })
```

**Add `.complete` checks to output-serving routes.** In `serve_extracted_html`, after the `session_dir` exists check, add:

```python
        complete_marker = os.path.join(session_dir, ".complete")
        if not os.path.exists(complete_marker):
            return "Extraction not complete. Please retry.", 404
```

In `serve_extracted_page` (GET branch), after the `session_dir` isdir check, add the same `.complete` check:

```python
        complete_marker = os.path.join(session_dir, ".complete")
        if not os.path.exists(complete_marker):
            return "Extraction not complete. Please retry.", 404
```

**Do NOT change any other route.** Specifically, leave these routes exactly as they are (they use `sm.save_meta`, which is correct because they are not concurrent with the extraction job and do not need the lock):
- `upload` (uses `sm.save_meta`)
- `save_draft` (uses `sm.save_meta`)
- `clear_draft` (uses `sm.save_meta`)
- `delete_session` (uses `shutil.rmtree`)
- `list_sessions` (read-only)
- `get_session`, `serve_page`, `serve_crop`, `save_page`, `serve_extracted_html`, `serve_extracted_page` (only the `.complete` checks above are added to the last two).
No other `sm.save_meta(...)` call site exists in `app.py`. Do not introduce one.

- [ ] **Step 4: Run the route tests**

`test_extract_routes.py` currently mocks the deleted `run_extraction` and MUST be fully replaced. Rewrite the entire file `crop_app/tests/test_extract_routes.py` with the exact content in **Step 5** before running tests. Then run:

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_extract_routes.py crop_app/tests/test_extraction_integration.py crop_app/tests/test_analysis.py crop_app/tests/test_crop_routes.py crop_app/tests/test_app.py -v`
Expected: all green. Two known adjustments if a test fails:
- `test_analysis.py`: the route now also sets `analysis_status`. If an assertion reads the old whole-session JSON, change it to assert `meta["pages"][i]["analysis_status"] == "done"`. **Do not delete any test.**
- `test_app.py`, `test_crop_routes.py`, `test_session_manager.py`: unchanged by this task; they must still pass as-is.

- [ ] **Step 5: Replace `crop_app/tests/test_extract_routes.py` (it mocks the deleted `run_extraction`)**

Overwrite the ENTIRE file `crop_app/tests/test_extract_routes.py` with this exact content:

```python
import os
import sys
import pytest
from PIL import Image
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager
from table_extractor.html_extractor import (
    set_output_root,
    _get_active_job,
)


@pytest.fixture
def client_ready_session(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")
    app.config["EXTRACTED_DIR"] = str(tmp_path / "extracted")
    set_output_root(app.config["EXTRACTED_DIR"])
    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm
    sid = sm.create_session()
    page_dir = sm.get_page_dir(sid)
    Image.new("RGB", (200, 300), "blue").save(os.path.join(page_dir, "page_000.png"))
    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    Image.new("RGB", (50, 50), "red").save(os.path.join(crop_dir, "crop_000.png"))
    sm.save_meta(sid, {
        "files": ["test.pdf"],
        "pages": [{
            "path": "page_000.png",
            "analysis_status": "done",
            "classification": "Complex",
            "crops": [{"path": "crop_000.png", "filename": "crop_000.png", "bbox": [0.1, 0.1, 0.5, 0.5]}],
        }],
    })
    with app.test_client() as client:
        yield client, sid, sm


def test_extract_html_progress_page_renders(client_ready_session):
    client, sid, sm = client_ready_session
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 200
    assert b"Initializing extraction" in resp.data


def test_extract_html_block_when_draft_present(client_ready_session):
    client, sid, sm = client_ready_session
    meta = sm.load_meta(sid)
    meta["pages"][0]["draft"] = [{"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}]
    sm.save_meta_atomic(sid, meta)
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert b"uncommitted changes" in resp.data


def test_extract_html_block_when_analysis_incomplete(client_ready_session):
    client, sid, sm = client_ready_session
    meta = sm.load_meta(sid)
    meta["pages"][0]["analysis_status"] = "pending"
    meta["pages"][0]["classification"] = None
    sm.save_meta_atomic(sid, meta)
    resp = client.get(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert b"analyze all pages" in resp.data


def test_post_extract_html_starts_job(client_ready_session):
    client, sid, sm = client_ready_session
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"
    job = _get_active_job(sid)
    if job:
        job.done_event.wait(timeout=10)


def test_extract_progress_sse_streams_terminal_from_disk(client_ready_session):
    client, sid, sm = client_ready_session
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        job = _get_active_job(sid)
        job.done_event.wait(timeout=10)
    resp = client.get(f"/extract-progress/{sid}")
    assert resp.status_code == 200
    assert "done" in resp.data.decode()


def test_extracted_html_serving_requires_complete_marker(client_ready_session):
    client, sid, sm = client_ready_session
    resp = client.get(f"/extracted/{sid}/extraction.html")
    assert resp.status_code == 404
```

- [ ] **Step 6: Commit**

```bash
git add crop_app/app.py crop_app/tests/test_extract_routes.py crop_app/tests/test_extraction_integration.py crop_app/tests/test_analysis.py crop_app/tests/test_crop_routes.py crop_app/tests/test_app.py
git commit -m "feat: app.py integrates atomic writes, per-page analysis status, crop mutation invalidation, POST start route, observational SSE, .complete checks"
```

- [ ] **Step 7: Add integration tests for required coverage**

Create `crop_app/tests/test_extraction_integration.py` with this exact content (covers: no automatic auth retry, mutation rejection during active job, terminal SSE after registry cleanup):

```python
import os
import sys
import time
import pytest
from PIL import Image
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import create_app
from session_manager import SessionManager
from table_extractor.html_extractor import (
    _get_active_job,
    _cleanup_completed_jobs,
    set_output_root,
    _clear_extraction_in_progress,
)


@pytest.fixture
def app_client(tmp_path):
    app = create_app()
    app.config["TESTING"] = True
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    app.config["CROP_DIR"] = str(tmp_path / "crops")
    app.config["EXTRACTED_DIR"] = str(tmp_path / "extracted")
    set_output_root(app.config["EXTRACTED_DIR"])
    sm = SessionManager(app.config["UPLOAD_DIR"], app.config["CROP_DIR"])
    app.session_manager = sm
    sid = sm.create_session()
    page_dir = sm.get_page_dir(sid)
    Image.new("RGB", (60, 60), "blue").save(os.path.join(page_dir, "page_000.png"))
    yield app.test_client(), sid, sm
    _clear_extraction_in_progress(sid)


def _mark_analyzed(sm, sid):
    meta = sm.load_meta(sid)
    for p in meta["pages"]:
        p["analysis_status"] = "done"
        p["classification"] = "Simple"
    sm.save_meta_atomic(sid, meta)


def test_no_automatic_auth_retry(app_client):
    client, sid, sm = app_client
    meta = sm.load_meta(sid)
    meta["pages"][0]["analysis_status"] = "done"
    meta["pages"][0]["classification"] = "Simple"
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "failed", "extraction_error": "bad key",
        "extraction_error_type": "auth", "fragment_path": None,
    }]
    sm.save_meta_atomic(sid, meta)

    resp = client.post(f"/extract-html/{sid}")
    assert resp.status_code == 400
    assert resp.get_json()["error_type"] == "auth"
    assert _get_active_job(sid) is None

    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp2 = client.post(f"/extract-html/{sid}?retry_nonretryable=true")
        assert resp2.status_code == 200
        assert resp2.get_json()["status"] == "started"
        job = _get_active_job(sid)
        assert job is not None
        job.done_event.wait(timeout=10)


def test_mutation_rejected_during_active_job(app_client):
    client, sid, sm = app_client
    _mark_analyzed(sm, sid)
    crop_dir = os.path.join(app.config["CROP_DIR"], sid)
    os.makedirs(crop_dir, exist_ok=True)
    Image.new("RGB", (20, 20)).save(os.path.join(crop_dir, "crop_000.png"))
    meta = sm.load_meta(sid)
    meta["pages"][0]["crops"] = [{"filename": "crop_000.png", "bbox": [0, 0.1, 1, 0.3]}]
    sm.save_meta_atomic(sid, meta)

    def slow(img, model):
        time.sleep(1.0)
        return "<p>x</p>"

    with patch("table_extractor.html_extractor.extract_crop_as_html", slow):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        resp2 = client.post(f"/trim/{sid}/crop_000.png", json={"bbox": [0, 0.1, 1, 0.3]})
        assert resp2.status_code == 409
    job = _get_active_job(sid)
    if job:
        job.done_event.wait(timeout=10)


def test_terminal_sse_after_cleanup(app_client):
    client, sid, sm = app_client
    _mark_analyzed(sm, sid)
    with patch("table_extractor.html_extractor.extract_crop_as_html", return_value="<p>x</p>"):
        resp = client.post(f"/extract-html/{sid}")
        assert resp.status_code == 200
        job = _get_active_job(sid)
        job.done_event.wait(timeout=10)
    _cleanup_completed_jobs()
    assert _get_active_job(sid) is None
    resp = client.get(f"/extract-progress/{sid}")
    assert resp.status_code == 200
    assert "done" in resp.data.decode()
```

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_extraction_integration.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add crop_app/tests/test_extraction_integration.py
git commit -m "test: add integration tests for auth-retry guard, mutation rejection, terminal SSE after cleanup"
```

- [ ] **Step 9: Add task-shape transition tests**

Create `table_extractor/tests/test_task_shape.py` with this exact content (covers first-crop and last-crop task-shape transitions via `on_crop_mutation`):

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from table_extractor.html_extractor import (
    on_crop_mutation,
    _clear_extraction_in_progress,
)


class _FakeSM:
    def __init__(self, base):
        self.base = base

    def get_extraction_fragments_dir(self, sid):
        d = os.path.join(self.base, sid, "extraction_fragments")
        os.makedirs(d, exist_ok=True)
        return d


def test_first_crop_task_shape_transition(tmp_path):
    sid = "s1"
    _clear_extraction_in_progress(sid)
    sm = _FakeSM(str(tmp_path))
    meta = {
        "files": ["t.pdf"],
        "pages": [{"analysis_status": "done", "classification": "Complex", "crops": []}],
        "extraction_tasks": [],
    }
    fdir = sm.get_extraction_fragments_dir(sid)
    with open(os.path.join(fdir, "page-0.html"), "w") as f:
        f.write("<p>whole</p>")
    meta["extraction_tasks"] = [{
        "task_id": "page-0", "page_idx": 0, "kind": "page",
        "extraction_status": "extracted", "extraction_error": None,
        "extraction_error_type": None, "fragment_path": "extraction_fragments/page-0.html",
    }]
    meta["pages"][0]["crops"] = [{"filename": "crop_003.png", "bbox": [0, 0.1, 1, 0.3]}]
    on_crop_mutation(meta, sm, sid, str(tmp_path / "out"))
    ids = [t["task_id"] for t in meta["extraction_tasks"]]
    assert "page-0" not in ids
    assert "crop_003" in ids
    crop_task = [t for t in meta["extraction_tasks"] if t["task_id"] == "crop_003"][0]
    assert crop_task["extraction_status"] == "pending"
    assert not os.path.exists(os.path.join(fdir, "page-0.html"))


def test_last_crop_task_shape_transition(tmp_path):
    sid = "s2"
    _clear_extraction_in_progress(sid)
    sm = _FakeSM(str(tmp_path))
    meta = {
        "files": ["t.pdf"],
        "pages": [{"analysis_status": "done", "classification": "Complex",
                   "crops": [{"filename": "crop_003.png", "bbox": [0, 0.1, 1, 0.3]}]}],
        "extraction_tasks": [],
    }
    fdir = sm.get_extraction_fragments_dir(sid)
    with open(os.path.join(fdir, "crop_003.html"), "w") as f:
        f.write("<p>crop</p>")
    meta["extraction_tasks"] = [{
        "task_id": "crop_003", "page_idx": 0, "kind": "crop",
        "crop_filename": "crop_003.png", "extraction_status": "extracted",
        "extraction_error": None, "extraction_error_type": None,
        "fragment_path": "extraction_fragments/crop_003.html",
    }]
    meta["pages"][0]["crops"] = []
    on_crop_mutation(meta, sm, sid, str(tmp_path / "out"))
    ids = [t["task_id"] for t in meta["extraction_tasks"]]
    assert "crop_003" not in ids
    assert "page-0" in ids
    page_task = [t for t in meta["extraction_tasks"] if t["task_id"] == "page-0"][0]
    assert page_task["extraction_status"] == "pending"
    assert not os.path.exists(os.path.join(fdir, "crop_003.html"))
```

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest table_extractor/tests/test_task_shape.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add table_extractor/tests/test_task_shape.py
git commit -m "test: add first-crop/last-crop task-shape transition tests"
```

---

## Task 10: Resume / status UI (templates + JS)

**Files:**
- Modify: `crop_app/templates/sessions.html` (add status spans in the card)
- Modify: `crop_app/templates/annotate.html` (add analysis badge container)
- Modify: `crop_app/templates/extract_progress.html` (add retry UI)
- Modify: `crop_app/static/js/annotate.js` (render analysis badge from `pageData`)
- Modify: `crop_app/static/js/extract_progress.js` (handle new SSE events + retry wiring)
- Test: `crop_app/tests/test_templates.py` (NEW, render-only smoke tests)

**Interfaces:**
- Backend `list_sessions` (Task 9, Step 3) attaches `analysis_status` and `extraction_status` strings to each session dict. `annotate` route passes `page_data` (the single page dict) which now carries `analysis_status` / `analysis_error`. `extract_progress.html` is rendered with `session_id`.

- [ ] **Step 1: Write render smoke tests**

Create `crop_app/tests/test_templates.py` (use `from app import create_app` — the package import is NOT available, matching every other test in `crop_app/tests`):

```python
import os, sys
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_templates.py -v`
Expected: FAIL (templates lack the status spans / retry control).

- [ ] **Step 3: Implement**

**3a. Backend `list_sessions` (already specified in Task 9 Step 3).** Confirm `list_sessions` attaches `analysis_status` and `extraction_status` to each session dict exactly as written there. No change needed here if Task 9 is done.

**3b. `sessions.html` — add status spans inside the card.** Replace the existing `.session-details` block (lines 204-210) with:

```html
            <div class="session-details">
              <span class="session-detail">Pages: {{ s.page_count }}</span>
              {% if s.crop_count > 0 %}
                <span class="session-detail">Crops: {{ s.crop_count }}</span>
              {% endif %}
              <span class="session-detail">Analysis: {{ s.analysis_status }}</span>
              <span class="session-detail">Extraction: {{ s.extraction_status }}</span>
              <span class="session-detail">{{ s.uploaded_at | datetime }}</span>
            </div>
```

**3c. `annotate.html` — add a badge container at the top of the crop panel.** In the `.crop-panel` div, immediately after `<h3>Crops</h3>`, insert:

```html
      <div id="analysis-status-badge"></div>
```

**3d. `annotate.js` — populate the badge from `pageData`.** Insert this block immediately after the line `const { sessionId, pageData, allPages } = window.APP_DATA;`:

```js
  // Show analysis status / error badge for the current page
  (function showAnalysisBadge() {
    const panel = document.getElementById("analysis-status-badge");
    if (!panel || !pageData) return;
    if (pageData.analysis_status === "error") {
      panel.style.color = "#e94560";
      panel.style.fontWeight = "600";
      panel.style.marginBottom = "12px";
      panel.textContent = "Analysis failed: " + (pageData.analysis_error || "unknown error");
    } else if (pageData.analysis_status === "pending") {
      panel.style.color = "var(--text-secondary)";
      panel.style.marginBottom = "12px";
      panel.textContent = "Page not yet analyzed.";
    } else if (pageData.analysis_status === "done") {
      panel.style.color = "#16a34a";
      panel.style.fontWeight = "600";
      panel.style.marginBottom = "12px";
      panel.textContent = "Analysis: " + (pageData.classification || "—");
    }
  })();
```

**3e. `extract_progress.html` — add retry UI.** Replace the existing `#error-area` div (the `<div id="error-area" hidden> ... </div>` block) with:

```html
      <div id="retry-area" hidden>
        <div class="error-block" id="retry-msg"></div>
        <div class="extract-actions">
          <button id="retry-btn" class="btn-open">Retry</button>
        </div>
      </div>

      <div id="error-area" hidden>
        <div class="error-block" id="error-msg">Extraction failed.</div>
        <div class="extract-actions">
          <a class="btn-secondary" href="/annotate/{{ session_id }}?page=0">&larr; Back to Annotations</a>
        </div>
      </div>
```

**3f. `extract_progress.js` — replace the entire `source.onmessage` handler** (the block starting at `source.onmessage = function (e) {` and ending at its closing `};`) with:

```js
  source.onmessage = function (e) {
    var data;
    try { data = JSON.parse(e.data); } catch (_) { return; }

    if (data.status === "starting") {
      setProgress(10);
      statusText.textContent = "Processing pages...";
      appendLog("Extraction server connected. Working...");

    } else if (data.status === "progress") {
      var total = data.total || data.totalPages || 1;
      var done = data.progress || data.page || 0;
      var pct = 10 + (done / Math.max(1, total)) * 80;
      setProgress(Math.min(pct, 90));
      statusText.textContent = "Extracted " + done + " of " + total + " regions...";
      if (data.log) appendLog(data.log);

    } else if (data.status === "done") {
      setProgress(100);
      pctText.textContent = "100%";
      statusText.textContent = "Complete";
      appendLog("HTML document generated successfully.");
      setTimeout(showResult, 700);
      source.close();

    } else if (data.status === "cancelled") {
      setProgress(100);
      pctText.textContent = "✗";
      statusText.textContent = "Cancelled";
      appendLog("Extraction cancelled.");
      source.close();

    } else if (data.status === "paused") {
      appendLog("Interrupted — click Resume to continue.");
      showRetry("Extraction interrupted. Resume from where it left off?", "/extract-html/" + sessionId);

    } else if (data.status === "idle") {
      appendLog("No extraction started yet.");
      showRetry("Start extraction?", "/extract-html/" + sessionId);

    } else if (data.status === "error") {
      setProgress(100);
      pctText.textContent = "✗";
      statusText.textContent = "Failed";
      appendLog("ERROR: " + (data.message || "unknown"));
      var isAuth = ["auth", "credits"].includes(data.error_type);
      if (isAuth) {
        showRetry(
          (data.message || "Authentication/credit failure.") + " Fix it, then click Retry.",
          "/extract-html/" + sessionId + "?retry_nonretryable=true"
        );
      } else {
        showError(data.message);
      }
      source.close();
    }
  };
```

**3g. `extract_progress.js` — add the `showRetry` helper.** Insert this immediately after the existing `showError` function definition (which ends with `}` before `setProgress(3);`):

```js
  function showRetry(message, url) {
    progressArea.hidden = true;
    var retryArea = document.getElementById("retry-area");
    var retryMsg = document.getElementById("retry-msg");
    var retryBtn = document.getElementById("retry-btn");
    retryMsg.textContent = message;
    retryBtn.disabled = false;
    retryBtn.textContent = "Retry";
    retryBtn.onclick = function () {
      retryBtn.disabled = true;
      retryBtn.textContent = "Retrying...";
      fetch(url, { method: "POST" })
        .then(function () { window.location.reload(); })
        .catch(function () {
          retryBtn.disabled = false;
          retryBtn.textContent = "Retry";
        });
    };
    retryArea.hidden = false;
  }
```

- [ ] **Step 4: Run template tests + full suite**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_templates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crop_app/templates/sessions.html crop_app/templates/annotate.html crop_app/templates/extract_progress.html crop_app/static/js/annotate.js crop_app/static/js/extract_progress.js crop_app/tests/test_templates.py
git commit -m "feat: resume/status UI in sessions, annotate, extract_progress templates and JS"
```

---

## Task 11: Legacy normalization + integration tests

**Files:**
- Modify: `table_extractor/html_extractor.py` (add `normalize_legacy_meta(meta, fragments_dir=None, crop_dir=None) -> dict`)
- Modify: `crop_app/app.py` (call `normalize_legacy_meta` at the start of `POST /extract-html` and the three crop-mutation routes, before `on_crop_mutation`)
- Test: `crop_app/tests/test_legacy_normalization.py` (NEW)

**Interfaces:**
- Produces: `normalize_legacy_meta(meta: dict, fragments_dir: str = None, crop_dir: str = None) -> dict` — returns a normalized copy with `analysis_status`, `extraction_tasks`, `next_crop_id` defaults filled per §14/§12.3. `next_crop_id` defaults to `max(existing numeric crop IDs in crop_dir) + 1` (never 0 when crops exist), per §1.1/§14.1.
- Consumes: `derive_required_tasks`, `SessionManager` (only for fragment existence when building tasks — pass `fragments_dir`).

- [ ] **Step 1: Write failing tests**

Create `crop_app/tests/test_legacy_normalization.py`:

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from table_extractor.html_extractor import normalize_legacy_meta


def test_normalize_legacy_classification_becomes_done():
    meta = {"pages": [{"path": "p.png", "classification": "Simple", "crops": []}]}
    out = normalize_legacy_meta(meta)
    assert out["pages"][0]["analysis_status"] == "done"


def test_normalize_missing_extraction_tasks_built():
    meta = {"pages": [{"path": "p.png", "classification": "Simple", "crops": []}]}
    out = normalize_legacy_meta(meta)
    assert "extraction_tasks" in out
    assert out["extraction_tasks"][0]["task_id"] == "page-0"


def test_normalize_next_crop_id_from_existing_crops(tmp_path):
    crop_dir = os.path.join(str(tmp_path), "crops", "sid")
    os.makedirs(crop_dir, exist_ok=True)
    # A legacy session that already has crop_003.png (and a stray crop_001.png)
    from PIL import Image
    Image.new("RGB", (10, 10)).save(os.path.join(crop_dir, "crop_003.png"))
    Image.new("RGB", (10, 10)).save(os.path.join(crop_dir, "crop_001.png"))
    meta = {"pages": [{"path": "p.png", "classification": "Complex", "crops": [
        {"filename": "crop_003.png", "bbox": [0, 0.1, 1, 0.3]},
    ]}]}
    out = normalize_legacy_meta(meta, crop_dir=crop_dir)
    # max numeric id is 3 -> next_crop_id must be 4 (no ID reuse)
    assert out.get("next_crop_id") == 4


def test_normalize_next_crop_id_zero_when_no_crops(tmp_path):
    crop_dir = os.path.join(str(tmp_path), "crops", "sid")
    os.makedirs(crop_dir, exist_ok=True)
    meta = {"pages": [{"path": "p.png", "classification": "Simple", "crops": []}]}
    out = normalize_legacy_meta(meta, crop_dir=crop_dir)
    assert out.get("next_crop_id") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_legacy_normalization.py -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement `normalize_legacy_meta`**

Add to `table_extractor/html_extractor.py`:

```python
def normalize_legacy_meta(meta: dict, fragments_dir: str = None, crop_dir: str = None) -> dict:
    """Return a normalized copy of meta with additive fields defaulted.

    Handles legacy sessions lacking analysis_status / extraction_tasks / next_crop_id.
    `next_crop_id` defaults to max(existing numeric crop IDs in crop_dir) + 1 (never 0
    when crops already exist), so legacy crop filenames are never reused.
    """
    meta = dict(meta)  # shallow copy; we will replace nested lists with new lists
    pages = []
    for page in meta.get("pages", []):
        page = dict(page)
        if "analysis_status" not in page:
            if page.get("classification") in ("Simple", "Complex"):
                page["analysis_status"] = "done"
            elif page.get("complex"):
                page["analysis_status"] = "done"
                page["classification"] = "Complex"
            else:
                page["analysis_status"] = "pending"
        pages.append(page)
    meta["pages"] = pages

    if "extraction_tasks" not in meta:
        desired = derive_required_tasks(meta)
        tasks = []
        for d in desired:
            frag_name = f"{d['task_id']}.html"
            has_frag = bool(fragments_dir and os.path.exists(os.path.join(fragments_dir, frag_name)))
            if has_frag:
                tasks.append({**d, "extraction_status": "extracted",
                              "fragment_path": f"extraction_fragments/{frag_name}",
                              "extraction_error": None, "extraction_error_type": None})
            else:
                tasks.append({**d, "extraction_status": "pending",
                              "fragment_path": None, "extraction_error": None,
                              "extraction_error_type": None})
        meta["extraction_tasks"] = tasks

    if "next_crop_id" not in meta:
        max_id = 0
        if crop_dir and os.path.isdir(crop_dir):
            for fname in os.listdir(crop_dir):
                if fname.startswith("crop_") and fname.endswith(".png"):
                    digits = fname[len("crop_"):-len(".png")]
                    if digits.isdigit():
                        max_id = max(max_id, int(digits))
        meta["next_crop_id"] = max_id + 1

    return meta
```

In `app.py`, at the start of `POST /extract-html`, `/commit`, `/trim`, `/delete-crop` (inside the lock, after `meta = _sm.load_meta(...)`), normalize. Pass `crop_dir` = `os.path.join(app.config["CROP_DIR"], session_id)` so legacy `next_crop_id` is derived from existing crop files:

```python
            fragments_dir = _sm.get_extraction_fragments_dir(session_id)
            crop_dir = os.path.join(app.config["CROP_DIR"], session_id)
            meta = normalize_legacy_meta(meta, fragments_dir, crop_dir)
```

Then proceed with `on_crop_mutation(meta, ...)` and `save_meta_atomic`.

- [ ] **Step 4: Run the new tests + full suite**

Run: `cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest crop_app/tests/test_legacy_normalization.py -v`
Expected: PASS.

Run the **entire** test suite as a final gate:
`cd "/Users/seemantshankar/Documents/Projects/Brochure Extraction" && python -m pytest -q`
Expected: all green (fix any regressions before committing).

- [ ] **Step 5: Commit**

```bash
git add table_extractor/html_extractor.py crop_app/app.py crop_app/tests/test_legacy_normalization.py
git commit -m "feat: legacy meta normalization on first access; integration tests"
```

---

## Self-Review Checklist (per writing-plans skill)

1. **Spec coverage** — every spec section maps to a task:
   - §1 Task identity/schema → Task 5 (crop ids), Task 6 (`derive_required_tasks`).
   - §2 Reconciliation → Task 6 (`reconcile_tasks`, `on_crop_mutation`), Task 8 (`_run_assembly`).
   - §3 Background job + SSE → Task 8 (job), Task 9 (POST start + observational SSE).
   - §4 Lock model → Task 2.
   - §5 `.complete` → Task 8 (`_write_complete_marker`), Task 9 (output checks).
   - §6 Retry/errors → Task 1.
   - §7 Bounded submission → Task 8 (`_execute_extraction`).
   - §8 Blank detection → Task 7.
   - §9 Analysis idempotency → Task 4.
   - §10 All-Simple eligibility → Task 9 (prerequisite removed).
   - §11 Resume UX → Task 10.
   - §12 Reconciliation rules → Task 6 + Task 8.
   - §13 SSE reconnection → Task 9.
   - §14 Backward compat → Task 11.
   - §15 Resume-after-fix → Task 9 (`retry_nonretryable`).
   - §16 File change summary → all files addressed.
   - §17 Tests → covered by each task's test file.
2. **Placeholder scan** — no "TBD"/"implement later"; every code step shows the code.
3. **Type consistency** — `ExtractionJob.run` sets only `self.result`; `reconcile_tasks`/`on_crop_mutation` operate on `meta["extraction_tasks"]`; `_sse_event` helper used consistently; `set_output_root` used by app.py so `_output_complete` resolves correctly; `save_meta_atomic` always called under `metadata_lock`.
