"""Tree network v2 (plan v6 checklist C1-C6, A1): hierarchy and ids, full coverage, believable tree, terrain per block,
pump stations, one sizing rule, 12 m3/s main canal. Synthetic world for the rules; the real build for the outputs."""
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canal_ops_network_v2 as n  # noqa: E402

OUT = n.OUT
CADASTRE = re.compile(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b")
PARENT_KIND = {"main_gate": {"main_canal"}, "main_pump": {"main_canal"}, "secondary_canal": {"main_gate", "main_pump"},
               "secondary_gate": {"secondary_canal"}, "secondary_pump": {"secondary_canal"}, "direct_outlet": {"secondary_canal"},
               "tail_escape": {"secondary_canal"}, "tertiary_canal": {"secondary_gate", "secondary_pump"},
               "tertiary_gate": {"tertiary_canal"}, "main_canal": {"main_canal"}}


# ---------------------------------------------------------------- synthetic world
def lines():
    """Stage I runs west from x=20 km to x=10 km; stage II continues west to x=0 but is stored reversed."""
    raw = {"stage_1": LineString([(20000, 5000), (10000, 5000)]), "stage_2": LineString([(0, 5000), (10000, 5000)])}
    return n.n1.orient_lines(raw, n.canal_cfg())


def terrain():
    """Falls 1 m/km westward and 10 m/km southward; a 50 m hill at x 14-16 km south of the canal."""
    xs = np.arange(-5000, 25000, 10.0) + 5
    ys = np.arange(12000, -6000, -10.0) - 5
    z = 900 + xs[None, :] / 1000.0 + (ys[:, None] - 5000) / 100.0
    hill = (xs[None, :] > 14000) & (xs[None, :] < 16000) & (ys[:, None] < 4700) & (ys[:, None] > 2000)
    return n.n1.Terrain(np.where(hill, z + 50.0, z), (10.0, 0.0, -5000.0, 0.0, -10.0, 12000.0))


CLASSES = ["FIELD_CROP", "NO_CROP", "ORCHARD", "VINEYARD", "GREENHOUSE", "NON_FARM", "NOT_CLASSIFIABLE"]


def parcels(seed=5, k=900):
    rng = np.random.default_rng(seed)
    x = rng.uniform(500, 19500, k)
    y = 5000 + np.where(rng.random(k) < 0.85, -1, 1) * rng.uniform(60, 6000, k)
    cls = rng.choice(CLASSES, k, p=[.45, .2, .1, .1, .03, .08, .04])
    p = pd.DataFrame({"item_id": [f"s{i:04d}" for i in range(k)], "cls": cls, "area_ha": np.round(rng.lognormal(-0.3, 0.8, k), 3),
                      "community": np.where(x > 12000, "East", np.where(x > 6000, "Mid", "West")), "x": x, "y": y,
                      "loc_source": "land types v1.5", "community_basis": "land types v1.5"},
                     index=pd.Index([f"p{i:04d}" for i in range(k)], name="internal_parcel_id"))
    p["farm"] = p.cls.isin(n.CFG["land_types"]["farm_classes"])
    p["ltype"] = p.cls.map(n.CFG["land_types"]["ltype"])
    p.loc["p0000", ["area_ha", "x", "y", "cls", "farm", "ltype"]] = [95.0, 8000.0, 3000.0, "FIELD_CROP", True, "field"]  # one big farm
    p["lon"], p["lat"] = p.x, p.y
    return p


def synthetic(p=None):
    ln = lines()
    net = n.build_network(parcels() if p is None else p, ln, terrain())
    reached = net["parcels"][net["parcels"].status == "reached"]
    rng = np.random.default_rng(1)
    series = rng.uniform(0, 60, (len(reached), 96)) * reached.area_ha.to_numpy()[:, None] * (reached.ltype != "greenhouse").to_numpy()[:, None]
    nd = n.size_network(net, series, np.full(96, 10.0))
    nd = n.section_of(n.add_pumps(n.node_aggregates(nd, net["parcels"])))
    net["nodes"], net["lines"] = nd, ln
    return net


@pytest.fixture(scope="module")
def net():
    return synthetic()


# ---------------------------------------------------------------- C1 hierarchy and ids
def test_hierarchy_gates_at_every_level(net):
    nd = net["nodes"].set_index("node")
    for node, r in nd.iterrows():
        if r.parent is None:
            assert node == "I"
            continue
        assert nd.at[r.parent, "kind"] in PARENT_KIND[r.kind], (node, r.kind, nd.at[r.parent, "kind"])
    kinds = set(nd.kind)
    assert {"main_gate", "secondary_canal", "secondary_gate", "tertiary_canal", "tertiary_gate", "direct_outlet", "tail_escape"} <= kinds


def test_ids_follow_the_plan_scheme_and_are_unique(net):
    nd = net["nodes"]
    assert not nd.node.duplicated().any()
    for node, kind in zip(nd.node, nd.kind):
        assert re.match(n.ID_RE[kind], node), (node, kind)
    t = nd[nd.kind == "tertiary_gate"].node.iloc[0]
    assert re.match(r"^(I|II)-[GP]\d{2}-S1-[GP]\d+-T1-G\d+$", t)       # e.g. I-G04-S1-G2-T1-G1


def test_ids_stable_across_reruns_and_input_order():
    a = synthetic()["nodes"]
    p = parcels().sample(frac=1.0, random_state=7)
    b = synthetic(p)["nodes"]
    cols = ["node", "kind", "parent", "ha", "capacity_lps", "terrain_state"]
    pd.testing.assert_frame_equal(a[cols].reset_index(drop=True), b[cols].reset_index(drop=True))


def test_id_change_table():
    old = pd.DataFrame({"internal_parcel_id": ["a", "b", "c"], "main": ["I-G01", "I-G01", "I-G02"], "unit": ["U1", "U1", "U2"], "outlet": ["O1", "O2", "O3"]})
    new = old.assign(main=["I-G01", "I-G03", "I-G03"])
    t = n.id_changes(old, new, kinds=("main",)).set_index("old_id")
    assert t.at["I-G01", "status"] == "unchanged" and t.at["I-G02", "new_id"] == "I-G03"


# ---------------------------------------------------------------- C2 coverage
def test_every_farm_parcel_reached_and_hectares_reconcile(net):
    p = net["parcels"]
    farm = p[p.farm]
    assert farm.outlet.notna().all() and (farm.status == "reached").all()
    assert p.loc[~p.farm, "outlet"].isna().all() and p.loc[~p.farm, "status"].str.startswith("not served").all()
    nd = net["nodes"]
    assert nd.loc[nd.kind.isin(["main_gate", "main_pump"]), "ha"].sum() == pytest.approx(farm.area_ha.sum(), abs=0.01)
    outlets = nd[nd.kind.isin(["tertiary_gate", "direct_outlet"])]
    assert outlets.ha.sum() == pytest.approx(farm.area_ha.sum(), abs=0.01)
    assert (outlets.n_parcels > 0).all()


def test_reached_is_not_irrigated(net):
    p = net["parcels"]
    gh = p[p.cls == "GREENHOUSE"]
    assert len(gh) and (gh.status == "reached").all() and (gh.canal_demand != "canal demand").all()
    nd = net["nodes"]
    assert (nd.ha_irrigable <= nd.ha + 1e-9).all()


# ---------------------------------------------------------------- C3 believable tree
def test_one_parent_no_loops_single_root(net):
    assert n.tree_errors(net["nodes"]) == []


def test_no_crossings_and_main_canal_not_crossed(net):
    bad, main = n.crossings(net["edges"], net["lines"])
    assert bad == [] and main == []


def test_tail_escape_on_every_secondary_and_direct_outlets(net):
    nd = net["nodes"]
    assert set(nd.loc[nd.kind == "tail_escape", "parent"]) == set(nd.loc[nd.kind == "secondary_canal", "node"])
    big = net["parcels"].loc["p0000"]
    assert big.outlet.split("-")[-1].startswith("D")                # a 95 ha farm: direct outlet on the secondary


def test_community_names_in_labels(net):
    nd = net["nodes"]
    lab = nd[nd.kind != "main_canal"]
    assert lab.label.str.contains(" · ").all()
    assert set(lab.community.dropna()) <= {"East", "Mid", "West"}


def test_emst_is_minimal_and_respects_barriers():
    rng = np.random.default_rng(2)
    xy = rng.uniform(0, 1000, (40, 2))
    parent, forced = n.emst(xy)
    assert forced == 0 and (parent[1:] >= 0).all() and parent[0] == -1
    length = sum(np.hypot(*(xy[v] - xy[parent[v]])) for v in range(1, 40))
    d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    seen, best, total = {0}, d[0].copy(), 0.0                        # brute-force Prim for comparison
    while len(seen) < 40:
        j = min((v for v in range(40) if v not in seen), key=lambda v: best[v])
        total += best[j]; seen.add(j); best = np.minimum(best, d[j])
    assert length == pytest.approx(total)
    wall = np.asarray([LineString([(500, -10), (500, 1010)])], dtype=object)
    xy2 = np.array([[100.0, 500], [400, 500], [600, 500], [900, 500], [450, 1500], [550, 1500]])
    parent, forced = n.emst(xy2, n.forbidden(xy2, wall))
    edges = [LineString([xy2[v], xy2[parent[v]]]) for v in range(1, len(xy2))]
    assert forced == 0 and not any(e.crosses(wall[0]) for e in edges)


def test_cut_tree_every_node_in_exactly_one_unit():
    rng = np.random.default_rng(4)
    xy = np.vstack([[0, 0], rng.uniform(0, 3000, (120, 2))])
    ha = np.concatenate([[0], rng.uniform(1, 9, 120)])
    ha[17] = 80.0                                                   # bigger than a tertiary: stays on the secondary
    parent, _ = n.emst(xy)
    units, on_sec = n.cut_tree(parent, ha, xy)
    seen = [m for u in units for m in (u["members"] if u["kind"] == "tertiary" else [u["node"]])]
    assert sorted(seen) == list(range(1, 121))
    t = n.CFG["tree"]
    assert all(u["ha"] <= t["tertiary_max_ha"] + 1e-9 for u in units if u["kind"] == "tertiary")
    assert all(u["at"] in on_sec for u in units if u["kind"] == "tertiary")
    assert 17 in on_sec and any(u["kind"] == "direct" and u["node"] == 17 for u in units)
    assert all(parent[v] in on_sec for v in on_sec if v != 0)       # the secondary is one connected trunk


def test_blocks_merge_only_with_adjacent_windows():
    w = n.CFG["secondary_blocks"]["chainage_window_m"]
    p = pd.DataFrame({"stage": "stage_1", "side": "left", "area_ha": [200, 30, 10, 200],
                      "chainage_m": [0.5 * w, 1.5 * w, 6.5 * w, 7.5 * w]}, index=list("abcd"))
    b = n.secondary_blocks(p)
    assert b["a"] == b["b"]                                          # small neighbour joins
    assert b["c"] == b["d"] and b["c"] != b["a"]                     # far small group never jumps 5 windows


# ---------------------------------------------------------------- C4 terrain
def test_terrain_decided_per_block_not_per_parcel(net):
    p = net["parcels"][net["parcels"].status == "reached"]
    assert (p.groupby("unit").terrain_state.nunique() == 1).all()
    assert set(p.terrain_state) <= set(n.STATES)
    assert {"gravity", "pumping"} <= set(p.terrain_state)           # south falls away; north bank and the hill are higher


def test_threshold_fixed_in_config_and_state_rule():
    tr = n.CFG["terrain"]
    assert tr["uncertain_band_m"] == 3.0 and "before the first build" in tr["uncertain_band_basis"]
    assert "before any v2 result" in n.CFG["frozen"]
    assert n.terrain_state(3.5, 3.0) == "pumping" and n.terrain_state(-3.5, 3.0) == "gravity"
    assert n.terrain_state(2.0, 3.0) == "terrain uncertain" and n.terrain_state(np.nan, 3.0) == "terrain not covered"


def test_canal_level_only_falls_downstream():
    lv = n.canal_levels(lines(), terrain())
    for _, g in lv.groupby("stage"):
        d = g.sort_values("chainage_m").level.dropna().diff().dropna()
        assert (d <= 1e-9).all()
    end1 = lv[lv.stage == "stage_1"].sort_values("chainage_m").level.dropna().iloc[-1]
    assert lv[lv.stage == "stage_2"].level.dropna().max() <= end1 + 1e-9


# ---------------------------------------------------------------- C5 pumps
def test_pump_stations_whole_units_night_windows_no_heads(net):
    nd = net["nodes"]
    pumps = nd[nd.pump.astype(bool)]
    assert len(pumps)
    assert all(float(u).is_integer() and u >= 1 for u in pumps.pump_units)
    assert (pumps.station_lps >= pumps.capacity_lps - 1e-9).all()
    assert (pumps.run_windows == "23:00-07:00").all() and (pumps.pump_basis == "terrain screening, synthetic").all()
    assert not [c for c in n.NODE_COLS if re.search(r"head|lift|energy|water_level|level_m|elev", c, re.I)]


def test_pump_units_rule():
    assert n.pump_units(20) == (1, 25)
    assert n.pump_units(180) == (4, 50)
    assert n.pump_units(410) == (3, 200)


def test_real_pump_list_overrides_the_terrain_rule(net):
    nd = net["nodes"]
    gate = nd[nd.kind == "secondary_gate"].node.iloc[0]
    pump = nd[nd.kind == "main_pump"].node.iloc[0] if (nd.kind == "main_pump").any() else None
    over = [{"node": gate, "kind": "pump", "units": 2, "unit_lps": 25}] + ([{"node": pump, "kind": "gate"}] if pump else [])
    o = n.add_pumps(nd, override=over).set_index("node")
    assert o.at[gate, "kind"] == "secondary_pump" and o.at[gate, "pump_units"] == 2 and o.at[gate, "pump_basis"] == "real list (owner override)"
    if pump:
        assert o.at[pump, "kind"] == "main_gate" and not o.at[pump, "pump"]


# ---------------------------------------------------------------- C6 sizing, A1 main canal
def test_one_sizing_rule_at_every_level(net):
    s = n.CFG["sizing"]
    nd = net["nodes"]
    c = nd[nd.capacity_lps.notna() & (nd.node != "I")]
    for r in c.itertuples():
        step = s["rounding_lps"][r.flow_class]
        rule = np.ceil(max(r.p90_lps, r.floor_lps) * s["headroom"] / step - 1e-6) * step
        lo = s["flow_class_min_open_lps"].get(r.flow_class, 0.0)
        expect = max(rule, lo) if r.kind != "main_canal" else min(rule, n.CFG["main_canal"]["capacity_lps"])
        assert r.capacity_lps == pytest.approx(expect, abs=step), r.node


def test_no_child_larger_than_parent_and_parents_rotate(net):
    nd = net["nodes"].set_index("node")
    cap = nd.capacity_lps.dropna()
    for node in cap.index:
        par = nd.at[node, "parent"]
        if isinstance(par, str) and par in cap.index:
            size = nd.at[node, "station_lps"] if pd.notna(nd.at[node, "station_lps"]) else cap[node]
            assert size <= cap[par] + 1e-9, (node, par)
    kids = cap.groupby(nd.parent.reindex(cap.index)).sum()
    assert any(kids[k] > cap[k] for k in kids.index if k in cap.index)   # rotating children, not the sum


def test_tertiary_streams_and_minimum_flows(net):
    nd = net["nodes"]
    lo, hi = n.CFG["sizing"]["tertiary_stream_lps"]
    assert nd.loc[nd.kind == "tertiary_canal", "capacity_lps"].between(lo, hi).all()
    assert (nd.loc[nd.flow_class == "secondary", "capacity_lps"] >= 100).all()
    m = nd[nd.min_open_lps.notna()]
    assert (m.min_open_lps >= 0.3 * m.capacity_lps - 1e-9).all()


def test_main_canal_capacity_12_m3s(net):
    nd = net["nodes"].set_index("node")
    assert n.CFG["main_canal"]["capacity_lps"] == 12000
    assert nd.at["I", "capacity_lps"] == 12000 and nd.at["II", "capacity_lps"] <= 12000
    assert (nd.capacity_lps.dropna() <= 12000).all()


# ---------------------------------------------------------------- the real build
def real():
    if not (OUT / "summary.json").exists():
        pytest.skip("network v2 not built")
    return json.loads((OUT / "summary.json").read_text(encoding="utf-8"))


def test_real_build_all_checks_pass():
    s = real()
    assert s["all_checks_pass"], {k: v for k, v in s["checks"].items() if not v}
    assert s["farm"]["reached_share"] == 1.0
    assert abs(s["universe"]["ha"] - 10339.3) <= 0.01
    rec = s["reconcile_by_class"]
    assert abs(sum(v["reached_ha"] + v["not_served_ha"] for v in rec.values()) - 10339.3) <= 0.01
    assert rec["GREENHOUSE"]["reached_ha"] == rec["GREENHOUSE"]["ha"]
    assert s["locations"]["filled_from_cadastral_geometry"] == 2945
    assert s["tree"]["crossings"] == 0 and s["tree"]["main_canal_crossings"] == 0
    assert 6000 <= s["terrain"]["stage_1_terrain_not_covered_to_m"] <= 8000


def test_real_outputs_hold_no_codes_ids_or_heads():
    real()
    nodes = pd.read_parquet(OUT / "nodes.parquet")
    text = (OUT / "network.geojson").read_text(encoding="utf-8") + nodes.to_csv()
    assert not CADASTRE.search(text) and "parcel_" not in text
    assert not re.search(r"command\s+area", text, re.I)
    assert not [c for c in nodes.columns if re.search(r"head|lift|energy|water_level|level_m|elev", c, re.I)]
    m = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    assert "parcel_node.parquet" in m["internal_only"]


def test_real_tree_recheck_from_files():
    from pyproj import Transformer
    real()
    nodes = pd.read_parquet(OUT / "nodes.parquet")
    edges = pd.read_parquet(OUT / "edges.parquet")
    assert n.tree_errors(nodes) == []
    t = Transformer.from_crs("EPSG:4326", n.CFG["crs"], always_xy=True)
    x0, y0 = t.transform(edges.lon0.to_numpy(), edges.lat0.to_numpy())
    x1, y1 = t.transform(edges.lon1.to_numpy(), edges.lat1.to_numpy())
    bad, main = n.crossings(pd.DataFrame({"x0": x0, "y0": y0, "x1": x1, "y1": y1}), n.load_canal(), tol=1.0)
    assert bad == [] and main == []
    p = pd.read_parquet(OUT / "parcel_node.parquet")
    assert set(p.loc[p.farm, "outlet"]) <= set(nodes.node)


def test_real_build_is_reproducible():
    real()
    nodes = pd.read_parquet(OUT / "nodes.parquet")
    again = n.build_all()["nodes"]
    cols = ["node", "kind", "parent", "label", "capacity_lps", "terrain_state", "pump_units"]
    pd.testing.assert_frame_equal(nodes[cols].reset_index(drop=True), again[cols].reset_index(drop=True), check_dtype=False)


def test_touching_the_canal_at_one_end_does_not_hide_a_crossing():
    """Regression: an edge starting on the canal that also crosses it further on is a crossing."""
    canal = {"stage_1": LineString([(0, 0), (1000, 0), (1000, 500), (0, 500)])}
    e = pd.DataFrame({"x0": [500.0, 200.0], "y0": [0.0, 0.0], "x1": [1200.0, 200.0], "y1": [300.0, 300.0]})
    bad, main = n.crossings(e, canal)
    assert main == [0]                                              # starts on the canal, crosses it at x=1000
    xy = np.array([[500.0, 0.0], [1200.0, 300.0]])
    assert n.forbidden(xy, np.asarray(list(canal.values()), dtype=object))[0, 1]
    assert n.crosses_lines(xy[0], xy[1], list(canal.values())) and not n.crosses_lines((200, 0), (200, 300), list(canal.values()))
