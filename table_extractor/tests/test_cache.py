from table_extractor.cache import cached_call, _cache_key
import concurrent.futures
import json
import os
import shutil
from pathlib import Path

def test_caching_and_force(tmp_path):
    test_dir = os.path.join(tmp_path, ".stage_cache")
    # Override the cache directory path in the module for test isolation
    import table_extractor.cache as cache
    orig_dir = cache.CACHE_DIR
    cache.CACHE_DIR = test_dir

    try:
        called = 0
        def execute_api():
            nonlocal called
            called += 1
            return {"regions": [{"id": "r1"}]}

        img_bytes = b"dummy_image_data"
        stage = "detect"
        model = "claude-3-5"

        # 1. First run (miss)
        res1 = cached_call(img_bytes, stage, model, execute_api)
        assert called == 1
        assert res1 == {"regions": [{"id": "r1"}]}

        # 2. Second run (hit)
        res2 = cached_call(img_bytes, stage, model, execute_api)
        assert called == 1
        assert res2 == {"regions": [{"id": "r1"}]}

        # 3. Forced run (miss/update)
        res3 = cached_call(img_bytes, stage, model, execute_api, force=True)
        assert called == 2
        assert res3 == {"regions": [{"id": "r1"}]}

    finally:
        cache.CACHE_DIR = orig_dir
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)


def test_concurrent_cached_call_writes_valid_json(tmp_path):
    test_dir = os.path.join(tmp_path, ".stage_cache")
    import table_extractor.cache as cache
    orig_dir = cache.CACHE_DIR
    cache.CACHE_DIR = test_dir

    try:
        image_bytes = b"concurrent_test_image"

        def worker(index):
            return cached_call(
                image_bytes=image_bytes,
                stage="concurrent_test",
                model="test/model",
                fn=lambda: [f"result_{index}", {"tokens": 1}],
                force=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, i) for i in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(results) == 20

        cache_file = Path(test_dir) / f"{_cache_key(image_bytes, 'concurrent_test', 'test/model')}.json"
        with open(cache_file, "r", encoding="utf-8") as f:
            cached_result = json.load(f)

        assert cached_result in results
    finally:
        cache.CACHE_DIR = orig_dir
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)
