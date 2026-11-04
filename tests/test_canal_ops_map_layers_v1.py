"""Map layers v1 (plan v6 checklist B1-B7): satellite basemap, 22,802 parcels coloured by v1.5 with a two-level legend,
44 communities and their outline, the observed zone, the real canal with its terrain gap, layer manifest for the page."""
import gzip
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import shapely
from shapely.geometry import Polygon, shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_map_layers_v1 as m  # noqa: E402

OUT = m.OUT
V15 = "server_data/review/alphaearth_photo_review_20261030_v1/releases/landtypes_v1_5/parcels.parquet"   # plan 2c path
ZONE = "Lower Hrazdan observed zone, stages I and II"


def layer(name):
    if not (OUT / name).exists():
        pytest.skip("map layers not built")
    return m.read_layer(OUT / name)


@pytest.fixture(scope="module")
def parcels():
    return layer("parcels.geojson.gz")


@pytest.fixture(scope="module")
def manifest():
    return layer("layers.json")


# ---------------------------------------------------------------- B1
def test_satellite_basemap_with_attribution(manifest):
    b = manifest["basemap"]
    assert b["type"] == "raster" and "World_Imagery" in b["tiles"][0] and "Esri" in b["attribution"]


# ---------------------------------------------------------------- B2
def test_parcels_layer_reconciles_to_22802(parcels):
    f = parcels["features"]
    assert len(f) == 22802 and len({x["properties"]["id"] for x in f}) == 22802
    assert sum(x["properties"]["ha"] for x in f) == pytest.approx(10339.3, abs=0.05)
    assert all(x["geometry"]["type"] in ("Polygon", "MultiPolygon") for x in f)


def test_missing_locations_filled_inside_their_parcel(parcels):
    net = pd.read_parquet(ROOT / "data/analysis/canal_ops_sim_v1/network_v2/parcel_node.parquet",
                          columns=["item_id", "loc_source", "lon", "lat"])
    filled = net[net.loc_source != "land types v1.5"]
    assert len(filled) == 2945 and filled[["lon", "lat"]].notna().all().all()
    geo = {x["properties"]["id"]: shape(x["geometry"]) for x in parcels["features"]}
    inside = [geo[i].buffer(1e-5).contains(shapely.Point(x, y)) for i, x, y in zip(filled.item_id, filled.lon, filled.lat)]
    assert all(inside)


# ---------------------------------------------------------------- B3
def test_colours_follow_v15_from_the_planned_path(parcels):
    for cfg in (m.CFG, json.loads((ROOT / "config/canal_ops_network_v2.json").read_text(encoding="utf-8"))):
        assert cfg["inputs"]["land_types"] == V15
    v15 = pd.read_parquet(ROOT / V15, columns=["item_id", "type6"]).set_index("item_id").type6.fillna("NCI")
    got = pd.Series({x["properties"]["id"]: x["properties"]["t"] for x in parcels["features"]})
    assert (got.sort_index() == v15.sort_index()).all()


def test_two_level_legend_covers_every_class(parcels, manifest):
    groups = {g["key"]: [c["key"] for c in g["classes"]] for g in manifest["legend"]["groups"]}
    assert groups["field_land"] == ["FIELD_CROP", "NO_CROP", "CROPLAND_UNCERTAIN"]
    assert groups["perennial"] == ["ORCHARD", "VINEYARD", "PERENNIAL"]
    keys = {c for v in groups.values() for c in v}
    assert {x["properties"]["t"] for x in parcels["features"]} <= keys
    for g in manifest["legend"]["groups"]:
        for c in g["classes"]:
            assert c["color"] is None or re.fullmatch(r"#[0-9a-f]{6}", c["color"])


# ---------------------------------------------------------------- B4
def test_44_communities_and_their_outline():
    com = layer("communities.geojson")["features"]
    src = json.loads((ROOT / "data/source/wua_areas.geojson").read_text(encoding="utf-8"))["features"]
    assert len(com) == 44 and {c["properties"]["name"] for c in com} == {s["properties"]["name_hy"] for s in src}
    assert sum(c["properties"]["zone_parcels"] for c in com) == 22802
    w = layer("wua_outline.geojson")["features"][0]
    p = w["properties"]
    assert p["label"] == "Outline of the 44 communities" and p["communities"] == 44
    holes = m.holes_of(m.to_m([shape(w["geometry"])]).iloc[0])
    assert all(h.area >= 10e4 for h in holes)                       # slivers and small holes closed
    big = p["holes"][0]
    assert 360 <= big["area_ha"] <= 375 and big["explanation"] and len(big["surrounded_by"]) >= 1
    assert big["observed_zone_parcels_inside"] == 0


def test_small_holes_filled_large_kept():
    outer = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
    small = [(100, 100), (150, 100), (150, 150), (100, 150)]         # 0.25 ha
    large = [(400, 400), (800, 400), (800, 800), (400, 800)]         # 16 ha
    g = m.fill_small_holes(Polygon(outer, [small, large]), 10e4)
    assert [round(h.area) for h in m.holes_of(g)] == [160000]


# ---------------------------------------------------------------- B5
def test_observed_zone_label_buffer_and_area():
    z = layer("observed_zone.geojson")["features"][0]["properties"]
    assert z["label"] == ZONE and z["buffer_m"] > 0 and z["area_ha"] > z["parcel_ha_official"]
    assert z["parcels"] == 22802


def test_no_command_area_wording_or_codes_anywhere():
    layer("summary.json")
    assert m.public_text_problems(OUT) == {}
    for d in (OUT, ROOT / "data/analysis/canal_ops_sim_v1/network_v2"):
        for f in d.iterdir():
            if f.suffix in (".json", ".geojson", ".gz"):
                raw = f.read_bytes()
                text = (gzip.decompress(raw) if f.suffix == ".gz" else raw).decode("utf-8")
                assert not re.search(r"command\s+area", text, re.I), f.name


def test_public_text_check_finds_codes_and_wording(tmp_path):
    (tmp_path / "a.geojson").write_text('{"x": "04-087-9119-0005"}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"x": "the command area"}', encoding="utf-8")
    (tmp_path / "c.geojson").write_text('{"x": "ok"}', encoding="utf-8")
    assert m.public_text_problems(tmp_path) == {"a.geojson": ["cadastral code"], "b.json": ["forbidden zone wording"]}


# ---------------------------------------------------------------- B6
def test_real_canal_two_stages_and_terrain_gap():
    f = layer("canal.geojson")["features"]
    stages = {x["properties"]["stage"] for x in f if x["properties"]["kind"] == "main_canal"}
    assert stages == {"I", "II"}
    gap = [x for x in f if x["properties"]["kind"] == "terrain_not_covered"]
    assert len(gap) == 1 and gap[0]["properties"]["stage"] == "I" and 6000 <= gap[0]["properties"]["to_m"] <= 8000
    one = next(x for x in f if x["properties"].get("stage") == "I" and x["properties"]["kind"] == "main_canal")
    assert "12 m3/s" in one["properties"]["capacity_note"]


# ---------------------------------------------------------------- B7
def test_layer_switcher_zoom_legend_popups(manifest):
    L = {x["id"]: x for x in manifest["layers"]}
    need = {"parcels", "communities", "wua_outline", "observed_zone", "main_canal", "secondary_canals", "tertiary_canals",
            "main_gates", "pump_stations", "pumping_areas", "laterals", "outlets", "tail_escapes"}
    assert need <= set(L)
    assert L["parcels"]["minzoom"] == 13 and L["laterals"]["minzoom"] == 13 and L["tertiary_canals"]["minzoom"] == 13
    assert all("visible" in x and x["title"] for x in L.values())
    for k in ("parcels", "communities", "main_gates", "pump_stations", "secondary_canals", "pumping_areas"):
        assert L[k]["popup"], k
    assert all((OUT / x["file"]).exists() for x in L.values())


# ---------------------------------------------------------------- network on the map
def test_network_layer_labels_pumps_and_areas():
    net = layer("network.geojson.gz")["features"]
    nodes = [x for x in net if x["geometry"]["type"] == "Point"]
    assert all(" · " in x["properties"]["label"] for x in nodes)
    pumps = [x["properties"] for x in nodes if x["properties"]["pump"]]
    assert pumps and all(p["pump_units"] >= 1 and p["run_windows"] == "23:00-07:00" for p in pumps)
    assert not any(re.search(r"head|lift|energy|elev", k) for x in net for k in x["properties"])
    areas = [x["properties"] for x in layer("pumping_areas.geojson")["features"] if x["properties"]["kind"] == "pumping_area"]
    s = json.loads((ROOT / "data/analysis/canal_ops_sim_v1/network_v2/summary.json").read_text(encoding="utf-8"))
    assert sum(a["ha"] for a in areas) == pytest.approx(s["terrain"]["reached_by_state"]["pumping"]["ha"], abs=0.05)
    assert all(a["basis"] == "terrain screening, synthetic" for a in areas)
    lat = layer("laterals.geojson.gz")["features"]
    assert len(lat) == s["farm"]["reached_parcels"]


def test_summary_checks_pass():
    s = layer("summary.json")
    assert s["all_checks_pass"], {k: v for k, v in s["checks"].items() if not v}
    assert s["communities"]["features"] == 44 and s["parcels"]["features"] == 22802
    assert s["read_guard"]["violations"] == []
