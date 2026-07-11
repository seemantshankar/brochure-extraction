# Task 2 Reviewer Findings Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the issues identified in the Task 2 reviewer findings regarding metadata lock patterns, legacy `save_meta` refactoring, and closing the file descriptor on error in `save_meta_atomic`.

**Architecture:** We will update `test_save_meta_atomic_creates_no_tmp_leftover` and `test_concurrent_writes_under_lock_are_consistent` setup phase to acquire the metadata lock. We will refactor `save_meta` to use the atomic saving method under a metadata lock. We will add a try/except block to `save_meta_atomic` to close the file descriptor on error.

**Tech Stack:** Python 3, Pytest

## Global Constraints
- Python 3.9
- Pytest for testing

---

### Task 1: Refactor `save_meta_atomic` and `save_meta` in `session_manager.py`

**Files:**
- Modify: `crop_app/session_manager.py`

**Interfaces:**
- Consumes: `SessionManager` class
- Produces: Updated `save_meta` and `save_meta_atomic` methods

- [ ] **Step 1: Update `save_meta_atomic` to close file descriptor on error**
  Modify `save_meta_atomic` in `crop_app/session_manager.py` to:
  ```python
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
  ```

- [ ] **Step 2: Refactor `save_meta` to acquire lock and use `save_meta_atomic`**
  Modify `save_meta` in `crop_app/session_manager.py` to:
  ```python
      def save_meta(self, session_id: str, data: dict) -> None:
          with self.metadata_lock(session_id):
              self.save_meta_atomic(session_id, data)
  ```

- [ ] **Step 3: Run the existing tests to check baseline compatibility**
  Run: `.venv/bin/pytest crop_app/tests/test_session_manager.py`
  Expected: At least the basic session manager tests pass, although locking checks might fail/succeed depending on test implementation.

- [ ] **Step 4: Commit refactored code**
  Run: `git add crop_app/session_manager.py`
  Run: `git commit -m "refactor: update save_meta and save_meta_atomic with proper lock and fd cleanup"`


### Task 2: Update lock patterns in test suite

**Files:**
- Modify: `crop_app/tests/test_session_manager.py`

**Interfaces:**
- Consumes: `SessionManager` class
- Produces: Updated test cases

- [ ] **Step 1: Update `test_save_meta_atomic_creates_no_tmp_leftover` to acquire lock**
  In `crop_app/tests/test_session_manager.py`, wrap the call to `save_meta_atomic` with `metadata_lock`:
  ```python
  def test_save_meta_atomic_creates_no_tmp_leftover(manager):
      sid = manager.create_session()
      data = {"pages": []}
      with manager.metadata_lock(sid):
          manager.save_meta_atomic(sid, data)
      session_dir = os.path.join(manager.upload_dir, sid)
      leftovers = [f for f in os.listdir(session_dir) if f.endswith(".json.tmp")]
      assert leftovers == []
      assert manager.load_meta(sid) == data
  ```

- [ ] **Step 2: Update `test_concurrent_writes_under_lock_are_consistent` setup to acquire lock**
  In `crop_app/tests/test_session_manager.py`, wrap the initial setup call to `save_meta_atomic` with `metadata_lock`:
  ```python
  def test_concurrent_writes_under_lock_are_consistent(manager):
      sid = manager.create_session()
      with manager.metadata_lock(sid):
          manager.save_meta_atomic(sid, {"counter": 0, "pages": []})
  ```

- [ ] **Step 3: Run verification tests**
  Run: `.venv/bin/pytest crop_app/tests/test_session_manager.py`
  Expected: All 11 tests pass successfully.

- [ ] **Step 4: Commit test updates**
  Run: `git add crop_app/tests/test_session_manager.py`
  Run: `git commit -m "test: update test lock patterns to acquire metadata_lock before calling save_meta_atomic"`
