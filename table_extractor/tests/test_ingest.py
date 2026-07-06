from PIL import Image
from table_extractor.ingest import load_and_prep, crop_with_padding
import os

def test_load_and_prep_single(tmp_path):
    img_path = os.path.join(tmp_path, "single.png")
    img = Image.new("RGB", (100, 100), color="red")
    img.save(img_path)
    pages = load_and_prep(img_path)
    assert len(pages) == 1
    assert pages[0].size == (100, 100)

def test_load_and_prep_double_spread(tmp_path):
    img_path = os.path.join(tmp_path, "spread.png")
    img = Image.new("RGB", (200, 100), color="blue")  # w/h = 2.0 (> 1.8)
    img.save(img_path)
    pages = load_and_prep(img_path)
    assert len(pages) == 2
    assert pages[0].size == (100, 100)
    assert pages[1].size == (100, 100)

def test_crop_with_padding():
    img = Image.new("RGB", (1000, 1000), color="white")
    cropped = crop_with_padding(img, [100.0, 100.0, 200.0, 200.0], pad_pct=0.05)
    # Box is [100, 100, 200, 200], width/height = 100
    # Padding = 100 * 0.05 = 5. Target crop = [95, 95, 205, 205]
    assert cropped.size == (110, 110)
