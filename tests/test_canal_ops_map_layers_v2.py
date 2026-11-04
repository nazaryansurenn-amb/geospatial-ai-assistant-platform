"""Map layers v2 over network v3: canal colours by level and supply, one pump symbol per station, downstream lines."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_map_layers_v2 as m2  # noqa: E402

OUT = ROOT / m2.CFG["output"]
built = pytest.mark.skipif(not (OUT / "summary.json").exists(), reason="map layers v2 not built")


def layer(name):
    raw = (OUT / name).read_bytes()
    return json.loads(gzip.decompress(raw) if name.endswith(".gz") else raw)


def test_canal_colours_are_the_approved_colour_blind_safe_set():
    s = m2.CFG["styles"]
    assert s["main_canal"]["color"] == "#0072B2" and s["main_canal"]["casing"] == "#ffffff"
    assert s["secondary_canal"]["color"] == "#56B4E9" and s["tertiary_canal"]["color"] == "#A6DCEF"
    assert s["pumped_secondary_canal"]["color"] == "#E69F00" and s["pumped_tertiary_canal"]["color"] == "#F5C26B"
    assert s["rising_main"]["color"] == "#D55E00" and s["rising_main"]["dashed"]
    assert s["observed_zone"]["color"] == "#ffffff" and s["observed_zone"]["dashed"]
    colours = [s[k]["color"] for k in ("main_canal", "secondary_canal", "tertiary_canal", "pumped_secondary_canal",
                                       "pumped_tertiary_canal", "rising_main", "observed_zone", "tail_escape")]
    assert len(set(colours)) == len(colours)
    assert m2.CFG["parcels"]["fill_opacity"] == 0.3


def test_draw_order_puts_structures_over_canals_over_areas():
    L = {l["id"]: l["order"] for l in m2.layers_manifest()["layers"]}
    assert L["parcels"] < L["pumping_areas"] < L["tertiary_canals"] < L["secondary_canals"] < L["main_canal"] < L["main_gates"]
    assert L["pump_stations"] == max(L.values())


@built
def test_every_map_check_passes():
    s = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    failed = [k for k, v in s["checks"].items() if not v]
    assert not failed, failed


@built
def test_pump_symbols_only_on_stations_and_gates_below_are_ordinary_gates():
    feats = layer("network.geojson.gz")["features"]
    pumps = [f["properties"] for f in feats if f["properties"].get("pump")]
    assert pumps and {p["kind"] for p in pumps} <= {"main_pump", "secondary_pump"}
    below = [f["properties"] for f in feats if f["properties"].get("pumped") and f["geometry"]["type"] == "Point"]
    assert below and all(not p["pump"] for p in below)


@built
def test_canals_are_lines_running_downstream_and_split_by_supply():
    feats = layer("network.geojson.gz")["features"]
    kinds = {(f["properties"]["kind"], f["properties"].get("pumped")) for f in feats if f["geometry"]["type"] == "MultiLineString"}
    assert ("secondary_canal", False) in kinds and ("tertiary_canal", False) in kinds
    assert any(k == "rising_main" for k, _ in kinds)
    assert all(len(line) >= 2 for f in feats if f["geometry"]["type"] == "MultiLineString" for line in f["geometry"]["coordinates"])


@built
def test_page_reads_the_map_of_the_network_in_use():
    import canal_ops_view as v
    assert v.MAP_DIR.name in ("map", "map_v2")
