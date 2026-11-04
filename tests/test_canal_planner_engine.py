"""Water delivery planner, step 5 (owner approval 2026-11-02): the operational plan for a confirmed supply. The water
account closes, the order of service holds, streams and change times are kept, and the same input gives the same plan.
Canal operating logic v2 (expert design reference 2026-11-02, owner-approved): one test or more per rule A-G."""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_planner_engine as E  # noqa: E402
from wp_core import canal_planner_state as ST  # noqa: E402
from wp_core import canal_planner_supply as SU  # noqa: E402

pytestmark = pytest.mark.skipif(not ST.SR.DAILY[2025].exists(), reason="satellite series missing")
D = date(2025, 7, 1)


@pytest.fixture(scope="module")
def snap():
    return ST.snapshot(D, "normal", 9)


def plan(text, snap, **kw):
    return E.plan(SU.parse_supply(text, SU.planning_moment(D)), snap=snap, **kw)


def supply(text):
    return SU.parse_supply(text, SU.planning_moment(D))


@pytest.fixture(scope="module")
def p5(snap):
    return plan("5 m3/s for 48 hours", snap)


@pytest.fixture(scope="module")
def p2(snap):
    return plan("2 m3/s for 48 hours", snap)


@pytest.fixture(scope="module")
def p9(snap):
    return plan("9 m3/s for 48 hours", snap)


def test_water_account_closes_and_is_complete(p5):
    a = p5.account
    assert a["closes"] and a["released_head_m3"] > 0
    assert all(v >= 0 for k, v in a.items() if k != "closes")
    assert a["delivered_field_inlets_m3"] == pytest.approx(p5.outlets.delivered_m3.sum(), abs=1)
    assert (p5.outlets.delivered_m3 <= p5.outlets.need_m3 + 1e-6).all()           # never more than the fields need


def test_turns_streams_and_times(p5):
    o = p5.outlets[p5.outlets.delivered_m3 > 0]
    p = o.attrs.get("parcels", p5.outlets.attrs["parcels"])
    due = p[p.due]
    assert (due.turn_mm.between(E.TURN_MIN, E.TURN_MAX)).all()                     # 500-1,000 m3/ha
    assert ((o.lps % 20) == 0).all() and (o.lps >= 20).all()                         # whole hand streams
    start = SU.datetime.fromisoformat(p5.supply["start"])
    assert start.hour in (7, 19)
    assert (o.open >= start).all() and (o.close > o.open).all()
    assert (p5.offtakes.peak_lps % 20 == 0).all()
    q, _ = E.head_flow(p5.supply)
    assert np.abs(np.diff(np.concatenate([[0], q, [0]]))).max() <= E.RAMP * E.Q_H / 2 + 1e-9   # ramps
    assert q.max() <= SU.CAP + 1e-9


def test_order_of_service_when_short(p2, p5):
    o = p2.outlets
    served = o.delivered_m3 > 0
    by = o.groupby("klass").delivered_m3.apply(lambda s: (s > 0).mean())
    assert by.get(3, 0) >= by.get(4, 0)                                              # protected before the rest
    assert p2.summary["outlets_not_reached"] > 0 and p2.summary["not_reached_ha"] > 0
    assert p5.summary["ha_with_a_turn"] > p2.summary["ha_with_a_turn"]              # more water, more land
    assert served.any() and o.status[~served].str.startswith("not reached").all()


def test_same_input_same_plan(snap, p5):
    again = plan("5 m3/s for 48 hours", snap)
    assert again.plan_id == p5.plan_id and again.account == p5.account
    other = plan("5 m3/s for 48 hours", snap, losses="high")
    assert other.plan_id != p5.plan_id
    assert other.account["delivered_field_inlets_m3"] < p5.account["delivered_field_inlets_m3"]   # higher losses


def test_history_classes_go_first(snap):
    # operating logic v2 (E) changed the order inside a class: hard floors first (never zero twice), then by the
    # score S = 40 D + 25 C + 20 T + 15 R, ties by name; it was most urgent first, then tail first
    o = E.outlets(snap, D, 48)
    some = list(o.index[o.klass == 4][:3])
    h = E.outlets(snap, D, 48, history={"zero_last": some})
    assert list(h.index[:3]) == sorted(some, key=lambda k: (-h.at[k, "score"], k))
    assert (h.loc[some, "klass"] == 1).all() and (h.loc[some, "tier"] == 1).all()
    assert (h.loc[some, "D"] == 1).all()                                            # zero last time: D = 1


def test_longer_windows_are_refused_here(snap):
    s = SU.parse_supply("5 m3/s for 2 weeks", SU.planning_moment(D))
    with pytest.raises(SU.SupplyRefused):
        E.plan(s, snap=snap)


# ---------------------------------------------------------------- realism fixes 1-4 (expert review, owner 2026-11-02)
def test_fix1_offtakes_need_enough_canal_flow_to_draw(snap, p2):
    # operating logic v2 (B) made check gates the default; the 50 % rule of fix 1 now applies only with
    # check_gates=False, so "low" is that explicit setting (it was the default)
    low = plan("2 m3/s for 48 hours", snap, check_gates=False)
    gates = plan("2 m3/s for 48 hours", snap, check_gates=True)
    assert low.summary["offtakes_canal_too_low"]                                      # 2 m3/s: some cannot draw
    assert set(gates.summary["offtakes_canal_too_low"]) < set(low.summary["offtakes_canal_too_low"])
    assert gates.account["delivered_field_inlets_m3"] > low.account["delivered_field_inlets_m3"]
    for k in low.summary["offtakes_canal_too_low"]:
        assert k not in low.offtakes.index                                            # never drew
    assert (low.outlets.status[low.outlets.low_canal] == "not reached: canal level").all()
    assert p2.summary["check_gates_assumed"] and not low.summary["check_gates_assumed"]   # the new default


def test_fix2_arrival_band_and_turn_end_drain(p5):
    a = p5.account
    assert a["closes"] and a["turn_end_drain_m3"] > 0
    o = p5.outlets[p5.outlets.delivered_m3 > 0]
    band = E.REAL["arrival_band_share"]
    spread = (o.open - o.water_from).dt.total_seconds() / 3600
    assert np.allclose(spread, 2 * band * o.arrival_h, atol=1e-6)                     # water first, full flow later


def test_fix3_gate_changes_follow_travel_time_and_not_at_night(p5):
    # operating logic v2 (G1): the travel time uses the flow-dependent velocity, kept per offtake as arr_h (it was
    # chainage / 0.7 m/s)
    for k, r in p5.offtakes.iterrows():
        arr = round(E.reach_design()[1][k] / p5.summary["main_velocity_m_s"] / 3600 / E.Q_H) * E.Q_H
        assert r.arr_h == pytest.approx(arr, abs=E.Q_H)
        for c in r.changes:
            head = SU.datetime.fromisoformat(c["head"])
            here = SU.datetime.fromisoformat(c["here"])
            assert (here - head).total_seconds() / 3600 == pytest.approx(r.arr_h)
            assert here.minute % 15 == 0
    # at night a gatekeeper never opens or raises a gate (pump stations excepted); the flow may still fall when the
    # canal carries less, with the gate untouched
    raised = [(k, c) for k, r in p5.offtakes.iterrows() if not r.pump
              for a, c in zip([{"lps": 0}] + r.changes[:-1], r.changes)
              if c["lps"] > a["lps"] and E._night(SU.datetime.fromisoformat(c["here"]))]
    assert not raised


def test_fix3_offtake_open_close_match_its_gate_changes(p5):
    # review 2026-11-02: open / close used the unrounded travel time, so a sheet could show 04:56 next to "change here
    # at 05:00"; both now come from the same quarter-hour arrival
    for k, r in p5.offtakes.iterrows():
        assert r.open == SU.datetime.fromisoformat(r.changes[0]["here"])
        assert r.close == SU.datetime.fromisoformat(r.changes[-1]["here"]) and r.changes[-1]["lps"] == 0
        assert r.open.minute % 15 == 0 and r.close.minute % 15 == 0


def test_fix4_stress_test_hurts_the_tail_more_with_more_taking(p5):
    before = p5.account.copy()
    r = E.stress_tests(p5)
    assert p5.account == before                                                       # the plan itself is unchanged
    by = {(x["head_extra"], x["late_minutes"]): x for x in r}
    assert by[(0.25, 30)]["tail_shortfall_m3"] > by[(0.10, 15)]["tail_shortfall_m3"] > 0
    assert all(0 <= x["tail_shortfall_pct"] <= 100 for x in r)


# ------------------------------------------------- canal operating logic v2 (expert design reference 2026-11-02)
def test_A_night_water_is_routed_at_19(p5):
    o = p5.outlets
    assert p5.summary["night_m3"] > 0 and p5.summary["outlets_with_night_turn"] > 0
    start = SU.datetime.fromisoformat(p5.supply["start"])
    # a turn that starts in a night block is a night-accepting outlet (tail, turn over 8 h or the fixed share)
    started_night = [k for k, q in o.first_q.dropna().items()
                     if (start + timedelta(hours=(q // E.BLOCK) * 12)).hour == 19]
    assert started_night and o.loc[started_night, "night_ok"].all()
    assert (o.loc[o["tail"] | o.large, "night_ok"]).all()                          # tail and large turns always
    share = np.mean([E._accepts_night(k) for k in o.index])
    assert 0.55 <= share <= 0.75                                                    # 60-70 % by a stable hash
    assert [E._accepts_night(k) for k in o.index[:50]] == [E._accepts_night(k) for k in o.index[:50]]


def test_A_escape_is_reported_per_block_and_flagged(p5, p9):
    for p in (p5, p9):
        b = p.summary["escape_by_block"]
        assert b and sum(x["head_m3"] for x in b) == pytest.approx(p.account["released_head_m3"], abs=len(b))
        assert sum(x["escape_m3"] for x in b) == pytest.approx(p.account["escape_m3"], abs=len(b))
    assert p5.summary["escape_pct"] <= 5.0                                          # 5 m3/s: routed, not spilled
    assert p9.summary["routing_flag"] == E.ROUTE_FLAG                               # 9 m3/s for 48 h: too much
    assert "not_taken_m3" not in p9.account                                         # the line "not taken" is gone


def test_B_reach_blocks_below_half_design(p2, p9):
    assert p2.summary["reach_blocks_on"] and not p9.summary["reach_blocks_on"]
    blocks = p2.summary["reach_blocks"]
    order = E.RB["order"]
    secs = [b["sections"] for b in blocks]
    assert secs[0] == ["stage II"]                                                  # tail reach first
    firsts = [min(order.index(s) for s in x) for x in secs]
    assert firsts == sorted(firsts)                                                 # tail -> middle -> head
    for b in blocks:                                                                # whole 12-h blocks, changes at 07/19
        assert SU.datetime.fromisoformat(b["start"]).hour in (7, 19)
    o = p2.outlets[p2.outlets.delivered_m3 > 0]
    start = SU.datetime.fromisoformat(p2.supply["start"])
    for k, r in o.iterrows():                                                       # water only in its reach's block
        b = int(r.first_q // E.BLOCK)
        assert r.section in secs[min(b, len(secs) - 1)]
    assert not p2.summary["offtakes_canal_too_low"]                                 # nothing stranded by a low canal


def test_C_close_reopen_drain_and_refill(p5, p9):
    for p in (p5, p9):
        off = p.offtakes
        assert p.summary["close_events"] == off.closes.sum() >= len(off)            # every opened secondary closes
        assert p.summary["reopen_events"] == off.reopens.sum()
        assert p.account["turn_end_drain_m3"] > 0
        assert p.summary["close_reopen_loss_m3"] == pytest.approx(
            p.account["turn_end_drain_m3"] + p.account["refill_m3"], abs=2)
    assert p9.summary["reopen_events"] > 0 and p9.account["refill_m3"] > 0          # reopens cost a refill
    gaps = E._short_gaps({"X": np.array([1, 1, 0, 0, 1, 0] + [0] * 30 + [1.0])})
    assert gaps == {"X": {2, 3}}                                                    # under 6 h: held, longer: closed
    assert E.CR["holding_lps"] == 20 and E.CR["drain_share"] == 0.5


def test_D_ramp_down_in_daylight_and_tail_takes_the_last_water(p5):
    h = E.head_plan(p5.supply)
    q, start = h["q"], h["start"]
    falls = [i for i in range(1, len(q)) if q[i] < q[i - 1] - 1e-9]
    assert falls and all(not E._night(start + timedelta(hours=i * E.Q_H)) for i in falls)   # never at night
    assert h["ramp_down_after_window_h"] > 0                                        # 07:00 end: down from 05:00
    assert p5.summary["head"]["deviations"]                                         # and reported
    assert p5.summary["end_canal_water_to_tail_m3"] >= 0


def test_E_combined_order_of_service(snap, p2):
    o = E.outlets(snap, D, 48)
    keys = list(zip(o.tier, -o.score, o.index))
    assert keys == sorted(keys)                                                     # tier, score, then name
    w = E.OD["weights"]
    r = o.iloc[0]
    assert r.score == pytest.approx(w["D"] * r.D + w["C"] * r.C + w["T"] * r["T"] + w["R"] * r.R, abs=1e-5)
    assert (o.R[o.section == "stage II"] == 1).all()                                # the section turn (stage II first)
    # every outlet without its full turn is carry-over with a stated reason
    nr = p2.outlets[p2.outlets.status != "served"]
    assert nr.carry_over.all() and (nr.reason.str.len() > 0).all()
    assert p2.summary["concentrate_full_turns"]                                     # 2 m3/s: under 60 % of due demand
    assert sum(p2.summary["carry_over_reasons"].values()) == len(nr)


def test_F_released_equals_confirmed_and_cap_extension():
    for text in ("2 m3/s for 48 hours", "5 m3/s for 36 hours", "9 m3/s for 48 hours"):
        h = E.head_plan(supply(text))
        assert h["within_tolerance"]
        assert abs(h["released_m3"] - h["confirmed_m3"]) <= 0.01 * h["confirmed_m3"]
        assert h["q"][0] > 0                                                         # ramps up from the window start
    inside = E.head_plan(supply("5 m3/s for 36 hours"))                             # ends 19:00: ramps inside
    assert inside["ramp_down_after_window_h"] == 0 and inside["extension_h"] == 0
    assert inside["plateau_m3s"] > 5                                                # the plateau is raised
    capped = E.head_plan(supply("11.9 m3/s for 48 hours"))
    assert capped["plateau_m3s"] <= SU.CAP + 1e-9 and capped["extension_h"] > 0    # cap binds: runs longer
    assert capped["within_tolerance"]


def test_G_velocity_streams_and_escape_seepage(p5, p9):
    v = E.OL["velocity"]
    assert E.speed(12, 12) == pytest.approx(v["design_m_s"])
    assert E.speed(0.4 * 12, 12) == pytest.approx(0.485, abs=0.005)                 # about 0.5 m/s at 40 %
    assert E.speed(0.5, 12) == v["min_m_s"]
    assert p9.summary["main_velocity_m_s"] > p5.summary["main_velocity_m_s"]
    o = p5.outlets
    assert set((o.stream_m3s * 1000).round()) <= {20.0, 40.0}                       # 20-50 l/s, whole streams
    ok = o.earthen & (o.cap_m3s >= 2 * E.HS)
    assert (o.stream_m3s[ok] >= 2 * E.HS - 1e-9).all()                              # 2 streams at earthen gates
    assert (o.loc[~o.earthen, "stream_m3s"] >= E.HS - 1e-9).all()
    # escaped water also seeps on the main canal: seepage exceeds what the offtakes' draws alone lose
    off = p9.offtakes
    assert p9.account["main_canal_seepage_m3"] > (off.head_m3 - off.at_offtake_m3).sum() + 1
    assert p9.summary["escape_capacity_m3s"] == E.ES["capacity_m3s"]


def test_G_escape_capacity_is_a_hard_limit(snap):
    big = plan("9 m3/s for 7 days", snap)
    assert big.summary["escape_peak_m3s"] > E.ES["capacity_m3s"]
    assert big.summary["routing_error"] == E.ROUTE_FLAG                             # a planning error: reduce the head


def test_G7_account_invariant_exact(p2, p5, p9):
    for p in (p2, p5, p9):
        a = p.account
        terms = sum(v for k, v in a.items() if k not in ("released_head_m3", "closes"))
        assert abs(terms - a["released_head_m3"]) <= 8                              # rounding only; floats asserted
        assert p.summary["head"]["released_m3"] == a["released_head_m3"]


def test_G6_operating_stress_tests(p5):
    before = p5.account.copy()
    late, night = E.operating_stress_tests(p5)
    assert p5.account == before                                                     # the plan itself is unchanged
    assert late["late_h"] == 2 and late["night_turns_hit"] > 0 and late["night_water_lost_m3"] > 0
    assert late["need_met_pct_after"] <= late["need_met_pct_plan"] + 2
    assert night["need_met_pct_if_declined"] <= night["need_met_pct_plan"]
    assert set(night["offtakes_night_only"]) <= set(p5.offtakes.index)
    assert "names no person" in late["label"] and "names no person" in night["label"]


def test_escape_capacity_2_with_warnings(p5, p9):
    """Owner 2026-11-03: escape capacity 2.0 m3/s; above 80 % a warning; above 12.5 % of released a waste warning."""
    assert E.ES["capacity_m3s"] == 2.0
    assert p5.summary["routing_error"] is None
    assert p5.summary["escape_near_limit"] == (p5.summary["escape_peak_m3s"] > 1.6)
    assert not p5.summary["escape_waste_warning"] and p9.summary["escape_waste_warning"]
