from table_extractor.schemas import Region, RegionType, ExtractedContent

def test_region_nesting():
    content = ExtractedContent(
        region_id="r1",
        region_type=RegionType.BULLET_PANEL,
        markdown="* Bullet 1",
        model_used="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001}
    )
    child = Region(
        id="r1-1",
        parent_id="r1",
        label="Child Panel",
        region_type=RegionType.BULLET_PANEL,
        bbox=[100, 100, 200, 200],
        may_contain_subregions=False,
        depth=1
    )
    parent = Region(
        id="r1",
        parent_id=None,
        label="Parent Panel",
        region_type=RegionType.BULLET_PANEL,
        bbox=[50, 50, 300, 300],
        may_contain_subregions=True,
        depth=0,
        children=[child],
        extracted=content
    )
    assert parent.children[0].id == "r1-1"
    assert parent.extracted.region_id == "r1"
