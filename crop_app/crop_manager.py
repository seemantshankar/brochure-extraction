# crop_app/crop_manager.py
import os
from PIL import Image


class CropManager:
    def __init__(self, crop_root: str):
        self.crop_root = crop_root
        os.makedirs(crop_root, exist_ok=True)

    def extract_crop(self, page_path: str, normalized_bbox: list) -> Image.Image:
        """Extract a crop from a page image using normalized coordinates [x0, y0, x1, y1] (0-1).

        Returns a PIL Image.
        """
        img = Image.open(page_path)
        w, h = img.size
        x0 = int(max(0, min(normalized_bbox[0], normalized_bbox[2])) * w)
        y0 = int(max(0, min(normalized_bbox[1], normalized_bbox[3])) * h)
        x1 = int(min(w, max(normalized_bbox[2], normalized_bbox[0])) * w)
        y1 = int(min(h, max(normalized_bbox[3], normalized_bbox[1])) * h)

        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y1 = y0 + 1

        return img.crop((x0, y0, x1, y1))

    def save_crop(self, session_id: str, page_path: str, normalized_bbox: list, filename: str) -> str:
        """Extract a crop and save to crops/<session_id>/<filename>.

        Returns the absolute path to the saved file.
        """
        crop_img = self.extract_crop(page_path, normalized_bbox)
        crop_dir = os.path.join(self.crop_root, session_id)
        os.makedirs(crop_dir, exist_ok=True)
        filepath = os.path.join(crop_dir, filename)
        crop_img.save(filepath, "PNG")
        return filepath

    def trim_crop(self, existing_crop_path: str, normalized_bbox_in_crop: list) -> str:
        """Re-crop an existing crop image using normalized coordinates within that crop.

        Saves the result back to the same file path. Returns the path.
        """
        img = Image.open(existing_crop_path)
        w, h = img.size
        x0 = int(max(0, min(normalized_bbox_in_crop[0], normalized_bbox_in_crop[2])) * w)
        y0 = int(max(0, min(normalized_bbox_in_crop[1], normalized_bbox_in_crop[3])) * h)
        x1 = int(min(w, max(normalized_bbox_in_crop[2], normalized_bbox_in_crop[0])) * w)
        y1 = int(min(h, max(normalized_bbox_in_crop[3], normalized_bbox_in_crop[1])) * h)

        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y1 = y0 + 1

        trimmed = img.crop((x0, y0, x1, y1))
        trimmed.save(existing_crop_path, "PNG")
        return existing_crop_path
