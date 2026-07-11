from unittest.mock import MagicMock
from PIL import Image
from table_extractor.schemas import Region, RegionType
from table_extractor.snap import snap_bbox, snap_regions, get_normalized_ocr_boxes

def test_snap_bbox_no_ocr_boxes():
    bbox = [100.0, 100.0, 200.0, 200.0]
    snapped = snap_bbox(bbox, [])
    assert snapped == bbox

def test_snap_bbox_no_overlap():
    bbox = [100.0, 100.0, 200.0, 200.0]
    # Text box far away (over 15 units tolerance)
    ocr_boxes = [[300.0, 300.0, 400.0, 400.0]]
    snapped = snap_bbox(bbox, ocr_boxes, tolerance_x=15.0, tolerance_y=15.0)
    assert snapped == bbox

def test_snap_bbox_with_overlap():
    bbox = [100.0, 100.0, 200.0, 200.0]
    # Text box overlaps directly
    ocr_boxes = [[150.0, 150.0, 250.0, 250.0]]
    snapped = snap_bbox(bbox, ocr_boxes, tolerance_x=15.0, tolerance_y=15.0)
    # x1 and y1 should expand to 250
    assert snapped == [100.0, 100.0, 250.0, 250.0]

def test_snap_bbox_within_tolerance():
    bbox = [100.0, 100.0, 200.0, 200.0]
    # Text box is within 15 units of tolerance (e.g. at 205, which is close to 200)
    ocr_boxes = [[205.0, 205.0, 250.0, 250.0]]
    snapped = snap_bbox(bbox, ocr_boxes, tolerance_x=15.0, tolerance_y=15.0)
    # x1 and y1 should expand to 250
    assert snapped == [100.0, 100.0, 250.0, 250.0]

def test_snap_bbox_separate_tolerances():
    bbox = [100.0, 100.0, 200.0, 200.0]
    # Text box is at x=210, y=210.
    # tolerance_x=5.0: x=210 is outside tolerance_x (200 + 5 = 205), so no snap horizontally.
    # tolerance_y=15.0: y=210 is within tolerance_y (200 + 15 = 215), but since there is no horizontal overlap (cx0=95, cx1=205 vs tx0=210), there is no overall overlap.
    # Let's verify that a text box with x=202 (within tolerance_x) and y=210 (within tolerance_y) snaps both.
    ocr_boxes_1 = [[202.0, 210.0, 250.0, 250.0]]
    snapped_1 = snap_bbox(bbox, ocr_boxes_1, tolerance_x=5.0, tolerance_y=15.0)
    assert snapped_1 == [100.0, 100.0, 250.0, 250.0]

    # Let's verify that a text box with x=208 (outside tolerance_x) and y=210 (within tolerance_y) does not snap.
    ocr_boxes_2 = [[208.0, 210.0, 250.0, 250.0]]
    snapped_2 = snap_bbox(bbox, ocr_boxes_2, tolerance_x=5.0, tolerance_y=15.0)
    assert snapped_2 == bbox

def test_snap_bbox_clamping():
    # If text box goes out of bounds, snap coordinates should clamp to [0.0, 1000.0]
    bbox = [10.0, 10.0, 200.0, 200.0]
    ocr_boxes = [[-50.0, -50.0, 50.0, 50.0]]
    snapped = snap_bbox(bbox, ocr_boxes, tolerance_x=15.0, tolerance_y=15.0)
    assert snapped[0] == 0.0
    assert snapped[1] == 0.0
    assert snapped[2] == 200.0
    assert snapped[3] == 200.0

def test_get_normalized_ocr_boxes(mocker):
    # Mock get_ocr_engine to return a mock engine
    mock_engine = MagicMock()
    # Mock result format: [[box, text, score]]
    # box structure: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    mock_result = [
        [[[10.0, 20.0], [110.0, 20.0], [110.0, 40.0], [10.0, 40.0]], "Hello", 0.95]
    ]
    # RapidOCR returns (result, elapse)
    mock_engine.return_value = (mock_result, 0.123)
    
    mocker.patch("table_extractor.snap.get_ocr_engine", return_value=mock_engine)
    
    img = Image.new("RGB", (1000, 2000))
    boxes = get_normalized_ocr_boxes(img)
    
    # 1000x2000 image size:
    # x: 10/1000 * 1000 = 10, 110/1000 * 1000 = 110
    # y: 20/2000 * 1000 = 10, 40/2000 * 1000 = 20
    assert len(boxes) == 1
    assert boxes[0] == [10.0, 10.0, 110.0, 20.0]

def test_snap_regions(mocker):
    # Mock get_normalized_ocr_boxes to return pre-computed boxes
    mocker.patch("table_extractor.snap.get_normalized_ocr_boxes", return_value=[
        [150.0, 150.0, 250.0, 250.0]
    ])
    
    child = Region(
        id="child",
        label="Child Region",
        region_type=RegionType.RULED_TABLE,
        bbox=[120.0, 120.0, 180.0, 180.0],
        may_contain_subregions=False
    )
    parent = Region(
        id="parent",
        label="Parent Region",
        region_type=RegionType.RULED_TABLE,
        bbox=[100.0, 100.0, 200.0, 200.0],
        may_contain_subregions=True,
        children=[child]
    )
    
    img = Image.new("RGB", (500, 500))
    snap_regions(img, [parent], tolerance_x=15.0, tolerance_y=15.0)
    
    # Parent bbox should snap to [100.0, 100.0, 250.0, 250.0]
    assert parent.bbox == [100.0, 100.0, 250.0, 250.0]
    # Child bbox should snap to [120.0, 120.0, 250.0, 250.0] since it overlaps with the OCR box
    assert child.bbox == [120.0, 120.0, 250.0, 250.0]
