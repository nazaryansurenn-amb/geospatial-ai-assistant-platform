"""Terrain-routed network v3: the routing rules on small grids, and the built network (when present)."""
from __future__ import annotations

import json
import math
import re
import sys
from array import array
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canal_ops_network_v3 as n3  # noqa: E402

OUT = ROOT / "data/analysis/canal_ops_sim_v1/network_v3"
CFG = n3.CFG


def grid(ground, closed=()):
    """A small grid with a closed frame: (cls, canal, ground, W)."""
    g = np.asarray(ground, dtype=float)
    H, W = g.shape
    cls = np.full((H, W), n3.BORDER, dtype="uint8")
    for r, c in closed:
        cls[r, c] = n3.CLOSED
    cls[0, :] = cls[-1, :] = cls[:, 0] = cls[:, -1] = n3.CLOSED
    return bytearray(cls.ravel().tobytes()), bytearray(H * W), array("d", g.ravel().tolist()), W


# ---------------------------------------------------------------- routing rules
def test_pava_down_is_the_least_squares_non_increasing_fit():
    assert np.allclose(n3.pava_down([3, 4, 2]), [3.5, 3.5, 2])
    y = np.array([10, 9, 9.5, 8, 8.2, 7, 7.5, 6])
    f = n3.pava_down(y)
    assert (np.diff(f) <= 1e-12).all()
    assert abs(f.mean() - y.mean()) < 1e-9


def test_water_level_never_rises_and_stays_within_one_metre_of_the_ground():
    rng = np.random.default_rng(1)
    g = 100 - 0.002 * np.arange(30)[None, :] * 10 + rng.normal(0, 0.3, (12, 30))
    cls, canal, gl, W = grid(g)
    start = 6 * W + 1
    wl, lab, pred = n3.search(cls, canal, gl, W, [(start, float(g.ravel()[start]) + 0.5, 0, -1)], 0.5)
    emb = CFG["water_level"]["max_above_ground_m"]
    for i in np.flatnonzero(np.isfinite(wl)):
        p = pred[i]
        if p >= 0:
            assert wl[i] <= wl[p] + 1e-9
        assert wl[i] <= g.ravel()[i] + emb + 1e-9


def test_closed_farm_cells_are_never_entered():
    g = np.full((9, 9), 100.0)
    closed = [(r, 4) for r in range(1, 8) if r != 7]           # a wall with one gap at row 7
    cls, canal, gl, W = grid(g, closed)
    wl, lab, pred = n3.search(cls, canal, gl, W, [(4 * W + 1, 100.5, 0, -1)], 0.5)
    for r, c in closed:
        assert not np.isfinite(wl[r * W + c])
    target = 4 * W + 7                                         # reached only through the gap
    assert np.isfinite(wl[target])
    cur, rows = target, []
    while cur >= 0:
        rows.append(divmod(cur, W))
        cur = pred[cur]
    assert any(r == 7 for r, c in rows if c in (3, 4, 5))


def test_a_hill_deeper_than_the_cut_limit_blocks_gravity():
    g = np.full((7, 15), 100.0)
    g[:, 7] = 103.0                                            # a 3 m rise across the whole grid
    cls, canal, gl, W = grid(g)
    wl, *_ = n3.search(cls, canal, gl, W, [(3 * W + 1, 100.5, 0, -1)], 0.5)
    assert not np.isfinite(wl[3 * W + 10])                     # beyond the hill: no gravity route
    g[:, 7] = 101.0                                            # 0.5 m above the water stays within the cut limit
    cls, canal, gl, W = grid(g)
    wl, *_ = n3.search(cls, canal, gl, W, [(3 * W + 1, 100.5, 0, -1)], 0.5)
    assert np.isfinite(wl[3 * W + 10])


def test_drop_structures_follow_falling_ground():
    g = 100 - 0.5 * np.arange(20)[None, :].repeat(5, 0)        # falls 0.5 m every 10 m
    cls, canal, gl, W = grid(g)
    wl, *_ = n3.search(cls, canal, gl, W, [(2 * W + 1, 100.0, 0, -1)], 0.5)
    end = 2 * W + 18
    assert wl[end] <= g[2, 18] + CFG["water_level"]["max_above_ground_m"] + 1e-9


def test_search_is_deterministic():
    rng = np.random.default_rng(7)
    g = 100 + rng.normal(0, 0.4, (15, 15))
    a = n3.search(*grid(g)[:3], 15, [(7 * 15 + 1, 101.0, 0, -1)], 0.5)
    b = n3.search(*grid(g)[:3], 15, [(7 * 15 + 1, 101.0, 0, -1)], 0.5)
    assert np.array_equal(np.nan_to_num(a[0], nan=-1), np.nan_to_num(b[0], nan=-1)) and np.array_equal(a[2], b[2])


def test_secondary_carries_more_than_a_tertiary_and_units_cover_every_outlet():
    W = 100
    pred = np.full(W * 40, -1, dtype="int32")
    trunk = [20 * W + c for c in range(1, 60)]
    for a, b in zip(trunk, trunk[1:]):
        pred[b] = a
    outlets = {}
    for k, c in enumerate(range(5, 60, 6)):                   # branches north every 60 m, 15 ha each
        prev = 20 * W + c
        for r in range(19, 14, -1):
            pred[r * W + c] = prev
            prev = r * W + c
        outlets[prev] = 15.0
    t = n3.Tree(outlets, pred, lambda c: c == trunk[0])
    sec, units, acc = n3.split_tiers(t, W, CFG)
    assert t.root == trunk[0]
    assert all(acc[c] > CFG["tree"]["tertiary_max_ha"] for c in sec if c != t.root)
    covered = sorted(o for u in units for o in u["outlets"])
    assert covered == sorted(outlets)
    assert all(sum(t.ha.get(o, 0) for o in u["outlets"]) <= CFG["tree"]["tertiary_max_ha"] for u in units)


def test_levels_on_tree_never_break_the_cut_limit_when_routed_with_the_reserve():
    rng = np.random.default_rng(3)
    g = 100 - 0.001 * np.arange(60)[None, :] * 10 + rng.normal(0, 0.5, (20, 60))
    cls, canal, gl, W = grid(g)
    start = 10 * W + 1
    lvl = float(g.ravel()[start]) + 0.5
    wl, lab, pred = n3.search(cls, canal, gl, W, [(start, lvl, 0, -1)], CFG["water_level"]["routing_slope_m_per_km"])
    reach = [i for i in np.flatnonzero(np.isfinite(wl)) if divmod(i, W)[1] in (20, 40, 58)]
    outs = {i: 5.0 for i in reach[::3]}
    t = n3.Tree(outs, pred, lambda c: c == start)
    sec, units, _ = n3.split_tiers(t, W, CFG)
    lev, drops, cuts = n3.levels_on_tree(t, sec, units, lvl, g.ravel(), W, CFG)
    assert cuts == 0
    assert all(lev[v] <= lev[p] + 1e-9 for v, p in t.parent.items() if p is not None)


def test_pump_clusters_join_small_groups_to_a_near_large_one():
    xy = np.array([[0, 0], [100, 0], [200, 0], [900, 0], [5000, 0]], dtype=float)
    ha = np.array([10, 10, 10, 5, 5], dtype=float)
    lab = n3.clusters_of(xy, ha, 300, 20, 1000)
    assert lab[0] == lab[1] == lab[2] == lab[3]               # 900 m away, under 20 ha: joins
    assert lab[4] != lab[0]                                   # 5 km away: its own small station


# ---------------------------------------------------------------- the built network
built = pytest.mark.skipif(not (OUT / "summary.json").exists(), reason="network v3 not built")


@pytest.fixture(scope="module")
def summary():
    return json.loads((OUT / "summary.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nodes():
    return pd.read_parquet(OUT / "nodes.parquet")


@built
def test_every_build_check_passes(summary):
    failed = [k for k, v in summary["checks"].items() if not v]
    assert not failed, failed


@built
def test_no_canal_runs_through_farm_land(summary):
    assert summary["routing"]["edge_points_inside_farm_interiors"] == 0
    assert summary["routing"]["cells_where_level_rises"] == 0
    assert summary["routing"]["cells_cut_deeper_than_limit"] == 0


@built
def test_pumps_only_as_stations_at_the_canal(nodes):
    pumps = nodes[nodes.pump.astype(bool)]
    assert set(pumps.kind) <= {"main_pump", "secondary_pump"}
    par = dict(zip(nodes.node, nodes.parent))
    for n in pumps.node:                                       # no pump station below another pump station
        cur = par.get(n)
        while isinstance(cur, str):
            assert cur not in set(pumps.node), (n, cur)
            cur = par.get(cur)


@built
def test_hectares_reconcile_and_every_farm_parcel_is_reached(summary):
    assert summary["universe"]["ha"] == pytest.approx(10339.3, abs=0.01)
    assert summary["farm"]["reached_share"] == 1.0


@built
def test_outputs_carry_no_head_figures_or_codes():
    cols = pd.read_parquet(OUT / "nodes.parquet").columns
    assert not [c for c in cols if re.search(r"head|lift|energy|level_m|elev|ground|shortfall", c, re.I)]
    text = (OUT / "network.geojson").read_text(encoding="utf-8")
    assert not re.search(r"\b\d{2}-\d{3}-\d{4}-\d{4}\b", text)
    assert not re.search(r"parcel_[0-9a-f]{16,}", text)
    assert not re.search(r"command\s+area", text, re.I)


@built
def test_read_guard_clean_and_no_archive():
    man = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    assert man["read_guard_violations"] == []
    assert not [f for f in man["files_read"] if re.search(r"sql_?archive|\.(bak|mdf)$", f, re.I)]
    assert man["config_sha256"] and "terrain_dem" in man["inputs"]


@built
def test_network_v2_is_left_unchanged():
    v2 = ROOT / "data/analysis/canal_ops_sim_v1/network_v2"
    man = json.loads((v2 / "manifest.json").read_text(encoding="utf-8"))
    import hashlib
    for name, h in man["outputs"].items():
        assert hashlib.sha256((v2 / name).read_bytes()).hexdigest() == h, name
