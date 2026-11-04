"""Water sharing down the tree network v2 (plan v6 checklist D3, 2c hydraulics): nested capacities, losses by level,
balance closure at every link, minimum flows and rotation, no child above its parent, the same priorities at every
level, never zero twice, the 12 m3/s head cap with spill, section turns, speed."""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_tree_allocation as A  # noqa: E402

REAL = (A.NV2 / "nodes.parquet").exists()


# ---------------------------------------------------------------- a small tree with numbers worked by hand
def small_tree(cap_g1=1e9, cap_out=1e9, min_open=float("nan")):
    """head I -> offtake I-G01 at 2 km -> secondary I-G01-S1 -> outlets D1 (1 km) and D2 (3 km); escape E."""
    n = pd.DataFrame([
        dict(node="I", kind="main_canal", parent=None, stage="I", section=None, chainage_m=0.0, upstream_m=0.0, path_m=0.0,
             capacity_lps=12000.0, min_open_lps=np.nan),
        dict(node="I-G01", kind="main_gate", parent="I", stage="I", section="upper stage I", chainage_m=2000.0, upstream_m=np.nan,
             path_m=0.0, capacity_lps=cap_g1, min_open_lps=min_open),
        dict(node="I-G01-S1", kind="secondary_canal", parent="I-G01", stage="I", section="upper stage I", chainage_m=np.nan,
             upstream_m=np.nan, path_m=0.0, capacity_lps=cap_g1, min_open_lps=min_open),
        dict(node="I-G01-S1-D1", kind="direct_outlet", parent="I-G01-S1", stage="I", section="upper stage I", chainage_m=np.nan,
             upstream_m=np.nan, path_m=1000.0, capacity_lps=cap_out, min_open_lps=np.nan),
        dict(node="I-G01-S1-D2", kind="direct_outlet", parent="I-G01-S1", stage="I", section="upper stage I", chainage_m=np.nan,
             upstream_m=np.nan, path_m=3000.0, capacity_lps=cap_out, min_open_lps=np.nan),
        dict(node="I-G01-S1-E", kind="tail_escape", parent="I-G01-S1", stage="I", section="upper stage I", chainage_m=np.nan,
             upstream_m=np.nan, path_m=np.nan, capacity_lps=np.nan, min_open_lps=np.nan),
    ])
    return A.build_tree(n)


def rows(*spec):
    """(outlet, group, net_m3, ha, ltype)"""
    return pd.DataFrame([dict(outlet=o, group=g, net_m3=v, ha=h, ltype=lt, stage_weight=1.0, ripening=False)
                         for o, g, v, h, lt in spec])


def test_losses_by_level_give_the_head_cost():
    t = small_tree()
    main, sec = 1 - A.LOSS_PCT_PER_KM["main"] / 100, 1 - A.LOSS_PCT_PER_KM["secondary"] / 100
    assert t.cost["I-G01"] == pytest.approx(1 / main ** 2)
    assert t.cost["I-G01-S1-D2"] == pytest.approx(1 / (main ** 2 * sec ** 3))
    assert t.path["I-G01-S1-D1"] == ["I", "I-G01", "I-G01-S1", "I-G01-S1-D1"]


def test_enough_water_meets_every_need_and_balances():
    t = small_tree()
    r = rows(("I-G01-S1-D1", "other_green_field", 600, 1, "field"), ("I-G01-S1-D2", "perennial", 300, 1, "orchard"))
    p = A.rule_A(r, 1e9, 10, t)
    assert p.rows.alloc.tolist() == pytest.approx((p.rows.need).tolist())
    nt = p.node_table()
    head_need = float((p.rows.need * p.rows.outlet.map(t.cost)).sum())
    assert nt.at["I", "in_m3"] == pytest.approx(head_need) and p.used == pytest.approx(head_need)
    assert A.balance(p, nt)["closes"] and A.balance(p, nt)["max_link_residual_pct"] < 1e-6
    assert nt.at["I-G01-S1", "seepage_m3"] == pytest.approx(
        sum(nt.at[o, "in_m3"] * (1 / t.nodes.at[o, "keep"] - 1) for o in ("I-G01-S1-D1", "I-G01-S1-D2")))


def test_a_node_capacity_binds_and_the_parent_is_not_cut_in_proportion():
    t = small_tree(cap_g1=1.0)                                  # 1 l/s for 10 days = 864 m3 entering the offtake
    r = rows(("I-G01-S1-D1", "other_green_field", 600, 1, "field"), ("I-G01-S1-D2", "perennial", 600, 1, "orchard"))
    p = A.rule_A(r, 1e9, 10, t)
    nt = p.node_table()
    assert nt.at["I-G01", "in_m3"] == pytest.approx(864, rel=1e-6)
    per = p.rows.set_index("group")
    # rule A: the perennial floor level first, the sensitive levels next, then the rest: the perennial row is ahead
    assert per.at["perennial", "alloc"] / per.at["perennial", "need"] > per.at["other_green_field", "alloc"] / per.at["other_green_field", "need"]
    assert not A.invariants(p, nt)


def test_minimum_flow_turns_small_volumes_into_rotation():
    t = small_tree(min_open=100.0)
    r = rows(("I-G01-S1-D1", "other_green_field", 600, 1, "field"))
    nt = A.rule_A(r, 1e9, 10, t).node_table()
    g = nt.loc["I-G01"]
    assert g["mode"] == "rotation" and g.open_lps == 100.0           # average flow far below 100 l/s
    assert g.hours == pytest.approx(g.in_m3 / (100 * 3.6)) and g.hours < 240
    assert nt.at["I-G01-S1-E", "mode"] == "closed"


def test_head_cap_of_12_m3s_and_spill():
    acc, spill = A.head_supply(15.0 * 86400 * 10, 10)
    assert acc == pytest.approx(12.0 * 86400 * 10) and spill == pytest.approx(3.0 * 86400 * 10)
    acc, spill = A.head_supply(1000.0, 10)
    assert acc == 1000.0 and spill == 0.0


def test_never_zero_twice_at_the_lowest_unit():
    t = small_tree()
    r = rows(("I-G01-S1-D1", "other_green_field", 600, 1, "field"), ("I-G01-S1-D2", "other_green_field", 600, 1, "field"))
    tiny = 200.0                                                 # far below the need of either outlet
    plain = A.rule_A(r, tiny, 10, t).rows.set_index("outlet")
    again = A.rule_A(r, tiny, 10, t, zero_last={("I-G01-S1-D2", "other_green_field")}).rows.set_index("outlet")
    d2 = again.loc["I-G01-S1-D2"]
    # the outlet left dry last dekad is served first, up to its floor share or all the water there is
    assert d2.alloc >= min(A.ca.CFG["floors"]["zero_streak_floor_share"] * d2.need,
                           tiny / t.cost["I-G01-S1-D2"]) - 1e-6
    assert d2.alloc > plain.at["I-G01-S1-D2", "alloc"]


# ---------------------------------------------------------------- the real tree
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if not REAL:
        pytest.skip("network v2 not built")
    from wp_core import canal_ops_tools_v6 as V
    return V.v6_world(tmp_path_factory.mktemp("lt") / "ledger.sqlite", "2025-07-D3")


@pytest.fixture(scope="module")
def plans(world):
    per = "2025-07-D2"
    out = {}
    for scen, f in (("normal", 1.0), ("short40", 0.6), ("tiny", 0.1)):
        sup, spill = A.head_supply(world.head_capacity_m3[per] * f, world.days[per])
        for name, fn in (("A", A.rule_A), ("B", A.rule_B)):
            p = fn(world.demand[per], sup, world.days[per], world.maps.tree)
            out[(scen, name)] = (p, p.node_table())
    return out


def test_every_plan_balances_within_0_1_pct_at_every_link(plans):
    for key, (p, nt) in plans.items():
        b = A.balance(p, nt)
        assert b["closes"] and b["max_link_residual_pct"] <= 0.1, key
        assert b["head_in_m3"] == pytest.approx(b["delivered_m3"] + b["seepage_m3"], abs=2), key


def test_invariants_hold_capacity_parent_and_minimum_flows(plans):
    for key, (p, nt) in plans.items():
        assert A.invariants(p, nt) == [], key
        cap = nt.capacity_lps * 86.4 * p.days
        assert ((nt.in_m3 <= cap + 1) | cap.isna()).all(), key
        par = nt.parent.map(nt.in_m3)
        assert ((nt.in_m3 <= par + 1) | par.isna()).all(), key                 # no child above its parent
        op = nt[nt["mode"] != "closed"]
        m = op.min_open_lps.notna()
        assert (op[m].open_lps >= np.minimum(op[m].min_open_lps, op[m].capacity_lps) - 1e-6).all(), key
        assert (op.hours <= 24 * p.days + 1e-6).all(), key


def test_same_priorities_at_every_node_under_rule_A(plans):
    p, _ = plans[("short40", "A")]
    s, d = p.structure, p.rows
    x, need = d.alloc.to_numpy(), d.need.to_numpy()
    for grp_hi, grp_lo in (("perennial", "other_green_field"), ("sensitive_field", "other_green_field")):
        hi, lo = (d.group == grp_hi).to_numpy(), (d.group == grp_lo).to_numpy()
        a_hi = np.bincount(s.pn, (x * hi)[s.pr], minlength=len(s.ids))
        n_hi = np.bincount(s.pn, (need * hi)[s.pr], minlength=len(s.ids))
        a_lo = np.bincount(s.pn, (x * lo)[s.pr], minlength=len(s.ids))
        n_lo = np.bincount(s.pn, (need * lo)[s.pr], minlength=len(s.ids))
        both = (n_hi > 0) & (n_lo > 0)
        r_hi, r_lo = a_hi[both] / n_hi[both], a_lo[both] / n_lo[both]
        assert (r_hi >= r_lo - 1e-6).mean() > 0.99, (grp_hi, grp_lo)          # every node with both groups


def test_shortage_lowers_the_share_met_and_section_turns_start(plans):
    met = {k: A.metrics(*plans[(k, "A")])["need_met_pct"] for k in ("normal", "short40", "tiny")}
    assert met["normal"] > met["short40"] > met["tiny"]
    p, nt = plans[("tiny", "A")]
    turns = A.section_turns(p, nt)
    assert turns and set(turns) <= {"upper stage I", "lower stage I", "stage II"}
    assert sum(turns.values()) <= p.days + 1e-6                     # the turns fit the dekad


def test_turn_chart_uses_each_gate_opening(plans):
    p, nt = plans[("short40", "A")]
    tc = A.turns(p, nt)
    assert len(tc) and (tc.open_lps > 0).all() and tc.branch.str.endswith("-S1").all()
    assert set(tc["mode"]) <= {"rotation", "continuous"}


def test_one_simulation_takes_about_a_second_or_less(world):
    per = "2025-07-D2"
    sup, spill = A.head_supply(world.head_capacity_m3[per] * 0.6, world.days[per])
    t0 = time.perf_counter()
    p = A.simulate_plan(world.demand[per], sup, world.days[per], world.maps.tree, {"other_green_field": 0.6},
                        {"I-G07": 0.5}, 0.3)
    p.node_table()
    assert time.perf_counter() - t0 < 1.5
