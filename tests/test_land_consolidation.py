from dataclasses import replace

import numpy as np
import pytest
from shapely.geometry import GeometryCollection, box, LineString

from wp_core.land_consolidation import (Policy, block_id, compare_season,
    connected_complete_link, shared_boundary, shared_pixels, union_geometry)


def profile(shift=0):
    days = np.arange(60, 335, 10)
    growth = np.maximum(0, np.sin((days - 70 - shift) / 240 * np.pi))
    values = np.column_stack([.2 + .6 * growth, .1 + .5 * growth,
                              -.1 + .4 * growth, .2 - .4 * growth])
    return days, values


def test_shared_edge_not_corner_or_gap():
    a = box(0, 0, 100, 100)
    p = replace(Policy(), topology_tolerance_m=.001)
    assert shared_boundary(a, box(100, 0, 200, 100), GeometryCollection(), p)[1] == "adjacent"
    assert shared_boundary(a, box(100, 100, 200, 200), GeometryCollection(), p)[1] == "no_meaningful_boundary"
    assert shared_boundary(a, box(100.01, 0, 200, 100), GeometryCollection(), p)[1] == "no_meaningful_boundary"


def test_small_sliver_tolerance_does_not_repair_or_accept_real_overlap():
    a = box(0, 0, 100, 100)
    b = box(99.9, 0, 200, 100)
    before = b.wkb
    assert shared_boundary(a, b, GeometryCollection(), Policy())[1] == "adjacent"
    assert b.wkb == before
    assert shared_boundary(a, box(99, 0, 200, 100), GeometryCollection(), Policy())[1] == "overlapping_geometry"


def test_barriers_and_overlaps_not_bridged():
    import shapely
    a, b = box(0, 0, 100, 100), box(100, 0, 200, 100)
    road = LineString([(100, -1), (100, 101)]).buffer(3)
    assert shared_boundary(a, b, road, Policy())[1] == "mapped_barrier"
    assert shared_boundary(a, b, shapely.STRtree([road, box(2000, 2000, 2100, 2100)]), Policy()) == shared_boundary(a, b, road, Policy())
    assert shared_boundary(a, box(90, 0, 200, 100), road, Policy())[1] == "overlapping_geometry"


def test_similarity_uses_real_dates_and_multiple_indices():
    days, a = profile()
    assert compare_season(days, a, a.copy(), 2021, Policy())["similar"]
    b = a.copy(); b[:, 2] += .2
    assert not compare_season(days, a, b, 2021, Policy())["similar"]
    _, shifted = profile(60)
    assert not compare_season(days, a, shifted, 2021, Policy())["similar"]


def test_missing_or_flat_profile_is_not_agreement():
    days, a = profile()
    b = a.copy(); b[5:14] = np.nan
    assert not compare_season(days, a, b, 2021, Policy())["comparable"]
    flat = np.full(a.shape, .2)
    assert compare_season(days, flat, flat, 2021, Policy())["reason"] == "weak_seasonal_contrast"
    assert not compare_season(days, a, a * np.nan, 2021, Policy())["similar"]


def test_shared_pixels_count_both_parcel_fractions():
    a = (np.array([1, 2]), np.array([50., 50.]))
    b = (np.array([2, 3]), np.array([90., 10.]))
    assert shared_pixels(a, b) == .9
    assert shared_pixels(a, (np.array([4]), np.array([100.]))) == 0.


def test_chain_cannot_merge_dissimilar_ends():
    ids = ["a", "b", "c"]
    masks = {(0, 1): 31, (1, 2): 31, (0, 2): 0}
    groups = connected_complete_link(ids, [(0, 1, 100), (1, 2, 90)], lambda a, b: masks[tuple(sorted((a, b)))], Policy())
    assert sorted(len(g) for g, _ in groups) == [1, 2]


def test_all_pairs_must_agree_in_same_three_years():
    masks = {(0, 1): 0b00111, (1, 2): 0b11100, (0, 2): 0b10101}
    groups = connected_complete_link(["a", "b", "c"], [(0, 1, 100), (1, 2, 90)], lambda a, b: masks[tuple(sorted((a, b)))], Policy())
    assert sorted(len(g) for g, _ in groups) == [1, 2]


def test_order_replay_and_unconnected_parcels():
    edges = [(0, 1, 100), (1, 2, 90)]
    result = connected_complete_link(["a", "b", "c", "d"], edges, lambda a, b: 31, Policy())
    assert result == connected_complete_link(["a", "b", "c", "d"], edges[::-1], lambda a, b: 31, Policy())
    assert sorted(len(g) for g, _ in result) == [1, 3]
    assert block_id(["b", "a"]) == block_id(["a", "b"])


def test_input_geometry_not_modified():
    polygons = [box(0, 0, 100, 100), box(100, 0, 200, 100)]
    before = [p.wkb for p in polygons]
    merged = union_geometry(polygons)
    assert merged.area == 20000
    assert before == [p.wkb for p in polygons]


def test_source_has_no_grid_download_or_public_write():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    module = (root / "wp_core/land_consolidation.py").read_text()
    runner = (root / "run_land_consolidation.py").read_text()
    for forbidden in ("requests.", "httpx.", "rasterio.", "grid_250", "setFeatureState", "openai."):
        assert forbidden not in module + runner
    assert 'mode=ro' in runner
    assert 'data/analysis/land_consolidation/' in runner


def test_only_ready_active_population_and_conflicts_remain_review(tmp_path, monkeypatch):
    import json
    import sqlite3
    import geopandas as gpd
    import pandas as pd
    import run_land_consolidation as runner
    release = tmp_path / "release"
    release.mkdir()
    (release / "release.json").write_text(json.dumps({"index": "activity.sqlite3"}))
    records = [
        ("a", "active", "ready", 0, 0),
        ("b", "partial", "ready", 0, 0),
        ("c", "active", "review", 0, 0),
        ("d", "active", "ready", 1, 0),
        ("e", "active", "ready", 0, 1),
        ("f", "active", "ready", 0, 0),
        ("g", "active", "ready", 0, 0),
    ]
    with sqlite3.connect(release / "activity.sqlite3") as con:
        con.execute("CREATE TABLE parcel_analytics(cadastre_code,activity_class,activity_state,household,road_excluded)")
        con.executemany("INSERT INTO parcel_analytics VALUES(?,?,?,?,?)", records)
    basis = gpd.GeoDataFrame({"internal_parcel_id": list("abcdefg"), "cadastre_code": list("abcdefg"),
        "area_official_m2": [10000.] * 7, "household_agriculture": [False, False, False, True, False, True, False],
        "road_excluded": [False, False, False, False, True, False, False], "stage": ["stage_1"] * 7},
        geometry=[box(440000 + i * 200, 4440000, 440100 + i * 200, 4440100) for i in range(7)], crs=32638).to_crs(4326)
    spatial = pd.DataFrame({"internal_parcel_id": list("abcdefg"), "cadastre_code": list("abcdefg"),
        "area_official_m2": [10000.] * 7, "activity_stage": ["stage_1"] * 7,
        "minimum_width_m": [100.] * 6 + [5.], "pure_pixels_10m": [50] * 7, "pure_pixels_20m": [10] * 7})
    empty = gpd.GeoDataFrame({"buffer_width_m": []}, geometry=[], crs=32638)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.gpd, "read_parquet", lambda path: basis.copy())
    monkeypatch.setattr(runner.gpd, "read_file", lambda path: empty.copy())
    monkeypatch.setattr(runner.pd, "read_parquet", lambda path: spatial.copy())
    config = {"activity_release": "release", "eo": "eo", "observations": "obs", "basis": "basis.parquet",
              "roads": "roads.geojson", "canal": "canal.geojson", "activity_class": "active", "activity_state": "ready"}
    _, register, edges, _ = runner.load_population(config, Policy())
    states = register.set_index("internal_parcel_id").screening_state.to_dict()
    assert states == {"a": "pending", "f": "household_road_mask_conflict_review", "g": "spatial_support_review"}
    assert not edges
