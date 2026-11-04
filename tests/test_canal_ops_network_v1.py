"""Synthetic canal network v1: chainage, assignment, gate clustering, tree, stable ids, no parcel ids in public outputs."""
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import canal_ops_network_v1 as n  # noqa: E402

OUT = n.OUT
PARCEL_ID = re.compile(r"parcel_[0-9a-f]{16,}")
CADASTRE = re.compile(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b")


def lines():
    """Stage I runs west from x=20 km to x=10 km; stage II continues west to x=0 but is stored reversed."""
    raw = {"stage_1": LineString([(20000, 5000), (10000, 5000)]), "stage_2": LineString([(0, 5000), (10000, 5000)])}
    return n.orient_lines(raw)


def terrain():
    """10 m grid falling 1 m per km westward and 10 m per km southward; raised land at x 14-16 km off the canal."""
    xs = np.arange(-5000, 25000, 10.0) + 5
    ys = np.arange(12000, -2000, -10.0) - 5
    z = 900 + xs[None, :] / 1000.0 + (ys[:, None] - 5000) / 100.0
    hill = (xs[None, :] > 14000) & (xs[None, :] < 16000) & (np.abs(ys[:, None] - 5000) > 100)
    z = np.where(hill, z + 50.0, z)
    return n.Terrain(z, (10.0, 0.0, -5000.0, 0.0, -10.0, 12000.0))


def parcels(seed=3, k=600):
    rng = np.random.default_rng(seed)
    x = rng.uniform(500, 19500, k)
    y = 5000 + rng.choice([-1, 1], k) * rng.uniform(50, 7000, k)
    comm = np.where(x > 12000, "East", np.where(x > 6000, "Mid", "West"))
    p = pd.DataFrame({"community": comm, "area_ha": np.round(rng.lognormal(-0.7, 0.9, k), 3),
                      "ltype": rng.choice(n.LTYPES, k), "x": x, "y": y},
                     index=pd.Index([f"p{i:04d}" for i in range(k)], name="internal_parcel_id"))
    p.loc["p0000", ["area_ha", "x", "y"]] = [42.0, 8000.0, 4000.0]   # one parcel over the gate maximum
    p.loc["p0001", ["x", "y"]] = np.nan                  # no location
    p.loc["p0002", "community"] = "(outside community outlines)"
    return p


@pytest.fixture(scope="module")
def net():
    return n.build_network(parcels(), lines(), terrain())


def test_chainage_runs_downstream_from_zero_per_stage():
    ln = lines()
    assert ln["stage_2"].coords[0] == (10000.0, 5000.0)    # reversed: starts where stage I ends
    xs = np.linspace(19900, 100, 60)
    pr = n.project(xs, np.full_like(xs, 5100.0), ln)
    for s, g in pr.groupby("stage"):
        assert g.chainage_m.is_monotonic_increasing and g.chainage_m.min() >= 0
        assert g.chainage_m.max() <= ln[s].length + 1e-6
    assert set(pr.side) == {"right"}                        # north of a westward flow is the right bank
    assert n.upstream_offsets(ln) == {"stage_1": 0.0, "stage_2": 10000.0}


def test_canal_profile_falls_downstream(net):
    chk = n.orientation_check(net["profile"], lines())
    assert all(v["falls_downstream"] for v in chk.values())


def test_each_parcel_assigned_once_and_hectares_reconcile(net):
    p, gt, br = net["parcels"], net["gates"], net["branches"]
    asg = p[p.status == "assigned"]
    assert not p.index.duplicated().any() and asg.gate.notna().all() and gt.gate.is_unique
    assert p[p.status == "unassigned"].gate.isna().all()
    assert abs(gt.ha.sum() - asg.area_ha.sum()) < 0.01 and abs(br.ha.sum() - asg.area_ha.sum()) < 0.01
    assert abs(asg.area_ha.sum() + p.area_ha[p.status == "unassigned"].sum() - p.area_ha.sum()) < 1e-6
    for lt in n.LTYPES:
        assert abs(gt[f"ha_{lt}"].sum() - asg.area_ha[asg.ltype == lt].sum()) < 0.01


def test_unassigned_never_forced(net):
    p = net["parcels"]
    r = p.unassigned_reason
    assert r["p0001"] == "no location" and r["p0002"] == "outside community outlines"
    far = p.distance_m > n.CFG["assignment"]["max_distance_m"]
    assert far.any() and (p.status[far] == "unassigned").all()
    assert (r[far] == "beyond max distance from canal").all()


def test_tree_is_valid_and_shallow(net):
    br, gt = net["branches"], net["gates"]
    assert n.tree_errors(br, gt, lines()) == []
    assert set(br.parent) <= set(lines()) and set(gt.parent) <= set(br.branch)
    assert set(br.level) == {1} and set(gt.level) == {2}
    bad = br.copy(); bad.loc[0, "parent"] = bad.branch.iloc[0]
    assert any("loop" in e for e in n.tree_errors(bad, gt, lines()))


def test_offtake_upstream_and_nearest_for_community(net):
    p, br = net["parcels"], net["branches"]
    assert n.upstream_rule_errors(p, br) == 0
    assert (br.chainage_m % n.CFG["offtakes"]["round_m"] == 0).all()
    for s, g in br.groupby("stage_key"):
        assert list(g.label) == [f"{g.stage.iloc[0]}-B{i}" for i in range(1, len(g) + 1)]
        assert g.chainage_m.is_monotonic_increasing


def test_small_groups_join_a_neighbour(net):
    br = net["branches"]
    mn = n.CFG["offtakes"]["min_group_ha"]
    for (s, c), g in br.groupby(["stage_key", "community"]):
        assert len(g) == 1 or (g.ha >= mn).all()


def test_pump_by_terrain_rule(net):
    br = net["branches"]
    rise = n.CFG["offtakes"]["pump_min_rise_m"]
    assert ((br.offtake_type == "pump") == (br.rise_m >= rise)).all()
    assert (br.type_basis == "type by terrain rule, synthetic").all()
    assert (br.offtake_type == "pump").any() and (br.offtake_type == "gate").any()


def test_gate_size_bounds_and_no_crossing(net):
    p, gt = net["parcels"], net["gates"]
    g = n.CFG["gates"]
    ok = gt[gt.size_flag == "ok"]
    assert ((ok.ha >= g["min_ha"] - 1e-9) & (ok.ha <= g["max_ha"] + 1e-9)).all()
    assert (gt.ha[gt.ha > g["max_ha"]] == gt.ha[gt.size_flag == "single_parcel_over_max"]).all()
    assert (gt.n_parcels[gt.size_flag == "single_parcel_over_max"] == 1).all()
    assert gt.loc[gt.size_flag == "single_parcel_over_max"].ha.max() == 42.0
    asg = p[p.status == "assigned"]
    runs = asg.groupby(["branch", "side"]).area_ha.agg(["sum", "max"])
    fillable = runs[(runs["sum"] >= g["min_ha"]) & (runs["max"] <= g["min_ha"])].index
    assert len(fillable) and not gt.set_index(["branch", "side"]).loc[fillable].size_flag.eq("under_min").any()
    per = asg.groupby("gate").agg(c=("community", "nunique"), s=("side", "nunique"), b=("branch", "nunique"))
    assert (per.max() == 1).all()


def test_partition_contiguous_and_bounded():
    rng = np.random.default_rng(5)
    for _ in range(30):
        ha = np.round(rng.lognormal(-0.5, 1.0, rng.integers(5, 200)), 3)
        cuts = n.partition(ha)
        assert cuts[0][0] == 0 and cuts[-1][1] == len(ha)
        assert all(a[1] == b[0] for a, b in zip(cuts, cuts[1:]))
        sums = [ha[s:e].sum() for s, e in cuts]
        assert all(v <= 30 + 1e-9 or e - s == 1 for v, (s, e) in zip(sums, cuts))
    for _ in range(30):                                    # small parcels can always fill 10-30 ha gates
        ha = np.round(rng.uniform(0.05, 2.0, rng.integers(10, 300)), 3)
        if ha.sum() >= 10:
            assert all(10 - 1e-9 <= ha[s:e].sum() <= 30 + 1e-9 for s, e in n.partition(ha))
    assert n.partition(np.array([5.0, 35.0, 5.0])) == [(0, 1), (1, 2), (2, 3)]


def test_ids_deterministic_across_reruns_and_input_order(net):
    again = n.build_network(parcels().sample(frac=1.0, random_state=9), lines(), terrain())
    for k, key in (("branches", "branch"), ("gates", "gate")):
        assert list(net[k][key]) == list(again[k][key])
    pd.testing.assert_series_equal(net["parcels"].gate, again["parcels"].gate)


def test_old_to_new_id_table(net):
    p0 = net["parcels"].reset_index()
    ch = n.id_changes(p0, p0)
    assert set(ch.status) == {"unchanged"}
    drop = parcels().drop(index=net["parcels"][net["parcels"].status == "assigned"].index[:3])
    p1 = n.build_network(drop, lines(), terrain())["parcels"].reset_index()
    ch = n.id_changes(p0, p1)
    g = ch[ch.kind == "gate"]
    assert (g.status == "changed").any() and set(g.status) <= {"unchanged", "changed", "new", "removed"}
    assert g[g.status == "changed"].new_id.isin(p1.gate).all()


def test_spurs_capped_and_public_outputs_have_no_parcel_ids(net):
    cap = n.CFG["spurs"]["length_cap_m"]
    gt = net["gates"]
    ln = np.hypot(gt.spur_x - gt.branch.map(net["branches"].set_index("branch").x),
                  gt.spur_y - gt.branch.map(net["branches"].set_index("branch").y))
    assert (ln <= cap + 1e-6).all()
    gj = n.network_geojson(net, lines())
    spurs = [f for f in gj["features"] if f["properties"]["kind"] == "branch_spur"]
    assert spurs and all(f["properties"]["dashed"] and f["properties"]["schematic"] for f in spurs)
    text = json.dumps(gj) + json.dumps(n.summarize(net, lines()), allow_nan=False)
    assert not re.search(r"\bp\d{4}\b", text) and not PARCEL_ID.search(text)
    assert "internal_parcel_id" not in set(gt.columns) | set(net["branches"].columns)


def test_summary_checks_pass(net):
    s = n.summarize(net, lines())
    failed = [k for k, v in s["checks"].items() if not v and k != "assigned_share_parcels_ge_target"]
    assert failed == []
    assert s["offtakes"]["pump"] + s["offtakes"]["gate"] == s["offtakes"]["total"]


@pytest.mark.skipif(not (OUT / "summary.json").exists(), reason="network not built")
def test_real_network_smoke():
    s = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    assert s["all_checks_pass"] and s["universe"]["parcels"] == 18130
    assert abs(s["universe"]["ha"] - 9143.35) < 0.1 and s["assigned"]["parcels_share"] >= 0.9
    br = pd.read_parquet(OUT / "branches.parquet")
    gt = pd.read_parquet(OUT / "gates.parquet")
    assert set(br.offtake_type) <= {"pump", "gate"} and gt.gate.is_unique and set(gt.branch) <= set(br.branch)
    assert abs(gt.ha.sum() - s["assigned"]["ha"]) < 0.01
    for f in ("network.geojson", "summary.json"):
        text = (OUT / f).read_text(encoding="utf-8")
        assert not PARCEL_ID.search(text) and not CADASTRE.search(text)
    for df in (br, gt):
        text = df.astype(str).to_csv()
        assert not PARCEL_ID.search(text) and not CADASTRE.search(text)
    m = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    assert {"branches.parquet", "gates.parquet", "network.geojson", "summary.json"} <= set(m["outputs"])
    assert not any("ARCHIVE" in k.upper() for k in m["inputs"])
