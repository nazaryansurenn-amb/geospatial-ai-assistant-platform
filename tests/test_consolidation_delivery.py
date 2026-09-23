import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from wp_core.consolidation_delivery import project, PROPERTIES


def sample():
    blocks = gpd.GeoDataFrame([{"block_id": "private_a", "stage": "stage_1", "parcel_count": 3, "area_ha": 3., "strip_priority": True,
        "support_year_mask": 31, "maximum_shared_10m_fraction": .8, "geometry": box(435000, 4445000, 435100, 4445300)}], crs=32638)
    members = pd.DataFrame([{"block_id": "private_a", "cadastre_code": f"04-087-0119-000{i}", "area_official_m2": 10000.} for i in range(1, 4)])
    return blocks, members


def test_allowlisted_projection_preserves_group_geometry_and_area():
    b, m = sample()
    before = b.geometry.to_wkb().tolist()
    collection, summary, lookup = project(b, m)
    f = collection["features"][0]
    assert set(f["properties"]) == PROPERTIES
    assert f["geometry"] == b.to_crs(4326).iloc[0].geometry.__geo_interface__
    assert summary["summaries"]["lower_hrazdan"]["all"] == {"groups": 1, "parcels": 3, "area_ha": 3.}
    assert summary["summaries"]["stage_2"]["all"]["groups"] == 0
    assert set(lookup) == set(m.cadastre_code)
    assert before == b.geometry.to_wkb().tolist()
    assert not any(v in json.dumps((collection, summary, lookup)) for v in ("private_a", "support_year_mask", "maximum_shared", "confidence"))


@pytest.mark.parametrize("kind", ["duplicate_code", "oversized", "bad_stage", "bad_count"])
def test_bad_candidate_is_not_published(kind):
    b, m = sample()
    if kind == "duplicate_code": m.loc[1, "cadastre_code"] = m.loc[0, "cadastre_code"]
    if kind == "oversized": m.loc[0, "area_official_m2"] = 10001
    if kind == "bad_stage": b.loc[0, "stage"] = "unknown"
    if kind == "bad_count": b.loc[0, "parcel_count"] = 4
    with pytest.raises(ValueError): project(b, m)


def test_tile_templates_remain_unescaped_and_hook_survives_panel_closure():
    root = Path(__file__).resolve().parents[1]
    source = root / "server_data/review/consolidation_review_20260906_v1/frontend_source/src"
    component = (source / "ConsolidationView.jsx").read_text(encoding="utf-8")
    app = (source / "App.jsx").read_text(encoding="utf-8")
    assert "`${window.location.origin}${delivery.url}`" in component
    assert "new URL(delivery.url" not in component
    assert "const consolidation = useConsolidation(" in app
    assert 'type: "vector"' in component
    assert "setFeatureState" not in component
