import logging
from PIL import Image
from table_extractor.schemas import Region

logger = logging.getLogger(__name__)

_ocr_engine = None
_ocr_initialized = False

def get_ocr_engine():
    """Lazily load and initialize the RapidOCR engine."""
    global _ocr_engine, _ocr_initialized
    if _ocr_initialized:
        return _ocr_engine
    
    _ocr_initialized = True
    try:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            from rapidocr import RapidOCR
        
        _ocr_engine = RapidOCR()
        logger.info("RapidOCR initialized successfully.")
    except Exception as e:
        logger.warning(f"Failed to initialize RapidOCR (graceful fallback to no-op): {e}")
        _ocr_engine = None
    
    return _ocr_engine

def get_normalized_ocr_boxes(img: Image.Image) -> list[list[float]]:
    """Runs RapidOCR on the image and returns text boxes normalized to 0-1000 scale."""
    engine = get_ocr_engine()
    if engine is None:
        return []
    
    w, h = img.size
    if w <= 0 or h <= 0:
        return []
        
    try:
        import numpy as np
        # Convert PIL Image to RGB numpy array for RapidOCR
        img_np = np.array(img.convert("RGB"))
        res = engine(img_np)
        
        # Handle different return signatures (result, elapse) vs just result
        if isinstance(res, tuple) and len(res) == 2:
            result, _ = res
        else:
            result = res
        
        if not result:
            return []
        
        normalized_boxes = []
        for line in result:
            # line is typically [box, text, confidence] or (text, confidence, box) or similar
            box = None
            for item in line:
                if isinstance(item, (list, tuple)) and len(item) == 4:
                    if all(isinstance(pt, (list, tuple)) and len(pt) == 2 for pt in item):
                        box = item
                        break
            if box is None:
                # Fallback to index-based lookup if structure check failed
                if len(line) >= 3:
                    if isinstance(line[0], (list, tuple)) and len(line[0]) == 4:
                        box = line[0]
                    elif isinstance(line[2], (list, tuple)) and len(line[2]) == 4:
                        box = line[2]
            
            if box is not None:
                # box is [[x0, y0], [x1, y1], [x2, y2], [x3, y3]]
                tx0 = min(pt[0] for pt in box)
                ty0 = min(pt[1] for pt in box)
                tx1 = max(pt[0] for pt in box)
                ty1 = max(pt[1] for pt in box)
                
                # Normalize to 0-1000 scale
                tx0_norm = (tx0 / w) * 1000.0
                ty0_norm = (ty0 / h) * 1000.0
                tx1_norm = (tx1 / w) * 1000.0
                ty1_norm = (ty1 / h) * 1000.0
                
                normalized_boxes.append([tx0_norm, ty0_norm, tx1_norm, ty1_norm])
                
        return normalized_boxes
    except Exception as e:
        logger.warning(f"Error running RapidOCR engine or normalizing boxes: {e}")
        return []

def snap_bbox(
    bbox: list[float],
    normalized_ocr_boxes: list[list[float]],
    tolerance_x: float = 5.0,
    tolerance_y: float = 15.0
) -> list[float]:
    """Snaps a single bounding box to contain any overlapping text bounding boxes."""
    if not normalized_ocr_boxes:
        return bbox
        
    rx0, ry0, rx1, ry1 = bbox
    rx0_orig, ry0_orig, rx1_orig, ry1_orig = bbox
    
    # Tolerance-expanded bounding box
    cx0 = rx0 - tolerance_x
    cy0 = ry0 - tolerance_y
    cx1 = rx1 + tolerance_x
    cy1 = ry1 + tolerance_y
    
    for tx0, ty0, tx1, ty1 in normalized_ocr_boxes:
        # Check if the text box overlaps with the expanded region
        overlap_x = max(cx0, tx0) < min(cx1, tx1)
        overlap_y = max(cy0, ty0) < min(cy1, ty1)
        if overlap_x and overlap_y:
            # Calculate overlap with the ORIGINAL unexpanded bounding box to verify it's part of this region
            inter_x0 = max(rx0_orig, tx0)
            inter_y0 = max(ry0_orig, ty0)
            inter_x1 = min(rx1_orig, tx1)
            inter_y1 = min(ry1_orig, ty1)
            
            inter_w = max(0.0, inter_x1 - inter_x0)
            inter_h = max(0.0, inter_y1 - inter_y0)
            inter_area = inter_w * inter_h
            
            text_w = tx1 - tx0
            text_h = ty1 - ty0
            text_area = text_w * text_h
            
            # Calculate overlap ratios relative to the minimum of the text box and ORIGINAL region dimensions
            region_w = rx1_orig - rx0_orig
            region_h = ry1_orig - ry0_orig
            w_ratio = inter_w / min(text_w, region_w) if text_w > 0 and region_w > 0 else 0.0
            h_ratio = inter_h / min(text_h, region_h) if text_h > 0 and region_h > 0 else 0.0
            
            # Calculate how much we need to expand in each direction relative to the ORIGINAL bounding box
            # to prevent a chain-reaction of snaps from exceeding the limit.
            exp_x0 = rx0_orig - tx0
            exp_x1 = tx1 - rx1_orig
            exp_y0 = ry0_orig - ty0
            exp_y1 = ty1 - ry1_orig

            # Expansion limit to prevent dragging borders too far
            limit_x = max(3.5 * tolerance_x, 50.0)
            limit_y = max(3.5 * tolerance_y, 50.0)
            
            # Prevent spanning headers/banners from dragging borders by blocking snap if text is wide and causes large expansion
            is_not_spanning_x = (text_w <= 120.0) or (exp_x0 <= 1.5 * tolerance_x and exp_x1 <= 1.5 * tolerance_x)
            
            # Snap in each direction if the expansion is small, OR if there is a high overlap in that dimension
            should_snap_x0 = is_not_spanning_x and ((exp_x0 <= limit_x) or (w_ratio >= 0.3))
            should_snap_x1 = is_not_spanning_x and ((exp_x1 <= limit_x) or (w_ratio >= 0.3))
            should_snap_y0 = (exp_y0 <= limit_y) or (h_ratio >= 0.3)
            should_snap_y1 = (exp_y1 <= limit_y) or (h_ratio >= 0.3)
            
            if should_snap_x0:
                rx0 = min(rx0, tx0)
            if should_snap_y0:
                ry0 = min(ry0, ty0)
            if should_snap_x1:
                rx1 = max(rx1, tx1)
            if should_snap_y1:
                ry1 = max(ry1, ty1)
            
    # Clamp coordinates to [0, 1000] range
    rx0 = max(0.0, min(1000.0, rx0))
    ry0 = max(0.0, min(1000.0, ry0))
    rx1 = max(0.0, min(1000.0, rx1))
    ry1 = max(0.0, min(1000.0, ry1))
    
    return [rx0, ry0, rx1, ry1]

def snap_regions(
    img: Image.Image,
    regions: list[Region],
    tolerance_x: float = 5.0,
    tolerance_y: float = 15.0
) -> None:
    """Recursively snaps bounding boxes of all regions in the list using OCR detection."""
    # Only run OCR if there are regions to process and OCR engine can be initialized
    if not regions:
        return
        
    normalized_ocr_boxes = get_normalized_ocr_boxes(img)
    if not normalized_ocr_boxes:
        return
        
    def _snap_recursive(r_list: list[Region]):
        for r in r_list:
            r.bbox = snap_bbox(r.bbox, normalized_ocr_boxes, tolerance_x=tolerance_x, tolerance_y=tolerance_y)
            if r.children:
                _snap_recursive(r.children)
                
    _snap_recursive(regions)
