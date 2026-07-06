from PIL import Image
from table_extractor.schemas import Region, RegionType
from table_extractor.render import draw_overlay

def test_draw_overlay():
    img = Image.new("RGB", (1000, 1000), color="white")
    r0 = Region(
        id="r0", label="Test ruled table", region_type=RegionType.RULED_TABLE,
        bbox=[100, 100, 900, 900], may_contain_subregions=False, depth=0
    )
    res = draw_overlay(img, [r0])
    assert res.size == (1000, 1000)
    # Check that the image was drawn on (not pure white)
    pixels = list(res.getdata())
    assert any(p != (255, 255, 255) for p in pixels)
