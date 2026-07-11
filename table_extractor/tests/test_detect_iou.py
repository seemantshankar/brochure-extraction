from table_extractor.schemas import Region, RegionType
from table_extractor.detect import check_overlaps

def test_iou_overlapping_warning():
    r1 = Region(id="r1", label="L1", region_type=RegionType.RULED_TABLE, bbox=[10, 10, 100, 100], may_contain_subregions=False)
    r2 = Region(id="r2", label="L2", region_type=RegionType.RULED_TABLE, bbox=[20, 20, 110, 110], may_contain_subregions=False) # Significant overlap
    
    check_overlaps([r1, r2])
    assert r1.overlap_warning is True
    assert r2.overlap_warning is True
