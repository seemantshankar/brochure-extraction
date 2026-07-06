# crop_app/tests/test_crop_manager.py
import os
import pytest
import sys
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from crop_manager import CropManager


@pytest.fixture
def manager(tmp_path):
    return CropManager(str(tmp_path / "crops"))


@pytest.fixture
def sample_page(tmp_path):
    img = Image.new("RGB", (200, 300), "white")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.rectangle([50, 50, 150, 200], fill="red")
    path = str(tmp_path / "test_page.png")
    img.save(path)
    return path


def test_extract_crop_returns_correct_size(manager, sample_page):
    img = manager.extract_crop(sample_page, [0.25, 1/6, 0.75, 2/3])
    assert img.size[0] == 100
    assert img.size[1] == 150


def test_save_crop_creates_file(manager, sample_page):
    crop_path = manager.save_crop("test-session", sample_page, [0.25, 0.1, 0.75, 0.7])
    assert os.path.exists(crop_path)
    assert crop_path.endswith(".png")


def test_save_crop_file_is_valid_image(manager, sample_page):
    crop_path = manager.save_crop("test-session", sample_page, [0.0, 0.0, 1.0, 1.0])
    img = Image.open(crop_path)
    assert img.size == (200, 300)


def test_trim_crop_crops_within_crop(manager, sample_page):
    full_crop_path = manager.save_crop("test-session", sample_page, [0.0, 0.0, 1.0, 1.0])
    trimmed_path = manager.trim_crop(full_crop_path, [0.25, 0.25, 0.75, 0.75])
    img = Image.open(trimmed_path)
    assert img.size == (100, 150)


def test_extract_crop_handles_edge_coords(manager, sample_page):
    img = manager.extract_crop(sample_page, [0.0, 0.0, 1.0, 1.0])
    assert img.size == (200, 300)

    img2 = manager.extract_crop(sample_page, [0.5, 0.5, 0.5, 0.5])
    assert img2.size[0] >= 1 and img2.size[1] >= 1


def test_save_crop_increments_filename(manager, sample_page):
    path1 = manager.save_crop("test-session", sample_page, [0.0, 0.0, 0.5, 0.5])
    path2 = manager.save_crop("test-session", sample_page, [0.5, 0.5, 1.0, 1.0])
    assert path1 != path2
    assert "crop_000" in path1
    assert "crop_001" in path2
