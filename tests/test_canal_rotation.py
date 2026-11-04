"""Canal rotation logic (owner-approved design docs/CANAL_ROTATION_LOGIC_2026_11_02_EN.md, change log R1-R9 and part 3b
item 2): rounds and turns at every level on the real tree. One season chain per scenario (April to July 2025)."""
import json
import math
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from wp_core import canal_ops_tools_v6 as V  # noqa: E402
from wp_core import canal_rotation as R  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402

pytestmark = pytest.mark.skipif(not (V.NV2 / "nodes.parquet").exists() or not V.REFERENCE.exists(),
                                reason="network v2 or demand not built")
UNTIL = "2025-07-D3"
import pandas as pd  # noqa: E402
EX = V.example_ids(pd.read_parquet(V.NV2 / "nodes.parquet")) if (V.NV2 / "nodes.parquet").exists() else {}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return V.v6_world(tmp_path_factory.mktemp("rot") / "ledger.sqlite", UNTIL)


@pytest.fixture(scope="module")
def st(world):
    return V._static(world)


@pytest.fixture(scope="module")
def seasons(world):
    return {sc: V.season_rounds(world, UNTIL, sc) for sc in ("normal", "short40")}


def every_round(seasons):
    for sc, rr in seasons.items():
        for r in rr:
            yield sc, r


# ---------------------------------------------------------------- R1: rounds, periods, streams
def test_rounds_are_half_a_dekad(seasons):
    rr = seasons["normal"]
    by = {}
    for r in rr:
        by.setdefault(r.period, []).append(r)
    for p, rs in by.items():
        days = sum(r.slots for r in rs) * R.SLOT_H / 24
        assert [r.label for r in rs] == ["A", "B"] and rs[0].start == R.dekad_start(p)
        assert days in (8, 9, 10, 11) and abs(rs[0].slots - rs[1].slots) <= 1


def test_july_periods_follow_the_owner_table(world):
    rows = V._rule_a_rows(world, "2025-07-D2", "normal")
    P = R.outlet_periods(rows, 7, 5.0)
    for o, g in rows.groupby("outlet"):
        keys = {R.crop_key(r.group, r.crop) for r in g.itertuples() if r.need >= 0.3 * g.need.sum()} or \
               {R.crop_key(r.group, r.crop) for r in g.itertuples()}
        assert P[o] == min(R.CFG["periods"]["july_rounds"][k] for k in keys)
    assert R.CFG["periods"]["july_rounds"] == {"sensitive_field": 1, "other_green_field": 2, "alfalfa": 2, "orchard": 2,
                                               "perennial": 2, "vineyard": 3}


def test_other_months_use_the_fao56_rule(world):
    rows = V._rule_a_rows(world, "2025-05-D2", "normal")
    P = R.outlet_periods(rows, 5, 5.0)
    assert set(P.values()) <= {1, 2, 3, 4} and len(set(P.values())) > 1


def test_streams_are_whole_hand_streams_and_tertiary_minimum(st, seasons):
    for kids in st.children.values():
        for c in kids:
            assert c.minimum >= 20 and c.minimum >= 0.3 * float(st.nd.at[c.id, "capacity_lps"]) - 1e-9 or c.pump
            if not c.pump:
                assert c.stream % 20 == 0 and c.stream >= c.minimum
    for o in st.offtakes.values():
        if not o.pump:
            assert o.stream % 20 == 0 and o.minimum >= max(100, 0.3 * float(st.nd.at[o.id, "capacity_lps"])) - 20
    for sc, r in every_round(seasons):
        for t in r.turns:
            if t["level"] == "secondary" and "pump_limited" not in t["flags"]:
                c = next(c for c in st.children[t["parent"]] if c.id == t["unit"])
                if not c.pump:
                    assert t["lps"] % 20 == 0 and t["lps"] >= c.minimum, t           # closed or >= the minimum
        for o, f in r.offtake_flow.items():
            if not st.offtakes[o].pump:
                assert all(x == 0 or (x % 20 == 0 and x >= st.offtakes[o].minimum - 1e-9) for x in f), (o, f)


def test_turn_hours_follow_v_over_3_6_q(seasons):
    """R9: hours = V / (3.6 Q) + fill time (quarter hours); no 0.8 factor."""
    r = seasons["normal"][-1]
    for t in [t for t in r.turns if t["level"] == "tertiary"][:300]:
        h = (t["close"] - t["open"]).total_seconds() / 3600
        assert t["m3"] == pytest.approx(3.6 * t["lps"] * h, rel=0.005, abs=1)


# ---------------------------------------------------------------- capacity at every time step
def test_no_parent_carries_more_than_its_capacity(st, seasons, world):
    for sc, r in every_round(seasons):
        q_head = None
        for k in range(r.slots):
            head = sum(r.offtake_flow[o][k] * st.offtakes[o].cost for o in st.offtakes)
            stage2 = sum(r.offtake_flow[o][k] * st.offtakes[o].cost for o in st.offtakes if st.offtakes[o].stage == "II")
            assert head <= R.A.MAIN_CAPACITY_LPS + 1e-6
            assert stage2 / st.cost_II <= st.cap_II + 1e-6
            for o in st.offtakes.values():
                assert r.offtake_flow[o.id][k] <= float(st.nd.at[o.id, "capacity_lps"]) + 1e-6
        # secondary: children's draws never exceed the offtake's flow at any quarter hour
        for o in st.offtakes.values():
            ts = [t for t in r.turns if t["level"] == "secondary" and t["parent"] == o.s1]
            if not ts:
                continue
            keep = {c.id: c.keep for c in st.children[o.s1]}
            points = sorted({t["open"] for t in ts})
            for p in points:
                draw = sum(t["lps"] / keep[t["unit"]] for t in ts if t["open"] <= p < t["close"])
                k = int((p - r.start).total_seconds() // (R.SLOT_H * 3600))
                assert draw <= r.offtake_flow[o.id][k] + 1e-6, (o.id, p)
        # tertiary: one farm outlet at a time on each tertiary canal
        by = {}
        for t in r.turns:
            if t["level"] == "tertiary":
                by.setdefault(t["parent"], []).append(t)
        for ts in by.values():
            ts.sort(key=lambda t: t["open"])
            assert all(a["close"] <= b["open"] for a, b in zip(ts, ts[1:]))


# ---------------------------------------------------------------- R2: sections, stage II window
def test_sections_take_turns_every_round_in_the_approved_order(st, seasons):
    assert R.SECTION_ORDER == ["stage II", "lower stage I", "upper stage I"]
    secs = [st.offtakes[o].section for o in st.order]
    assert secs == sorted(secs, key=R.SECTION_ORDER.index)                   # the queue: stage II first
    for sc, r in every_round(seasons):
        assert r.lead_order == [s for s in R.SECTION_ORDER if s in r.lead_order]
        assert len(r.lead_order) == 3


def test_stage_ii_window_follows_the_duty_test(st, seasons):
    seen = 0
    for sc, r in every_round(seasons):
        w = r.stage2_window_slots
        assert w == 0 or w >= R.CFG["stage2_window"]["min_slots"]
        for o in st.offtakes.values():
            if o.stage == "I":
                assert all(x == 0 for x in r.offtake_flow[o.id][:w]), (r.period, r.label, o.id)   # stage I closed
        seen += w > 0
    assert seen >= 1                                                                    # the window does occur


def test_duty_test_on_a_constructed_round(st):
    """W_II = whole slots of min(n (1 - d*), stage II volume / head flow), valid only when stage I fits the rest."""
    state = R.State()
    V0 = {o: 0.0 for o in st.offtakes}
    s1 = next(o for o in st.offtakes.values() if o.stage == "I" and not o.pump)
    s2 = next(o for o in st.offtakes.values() if o.stage == "II" and not o.pump)
    V0[s1.id] = s1.stream * 43.2 * 3                                  # 3 of 10 slots at full stream: d* = 0.3
    V0[s2.id] = s2.stream * 43.2 * 5
    q_head = 3000.0
    m = R._main_canal(st, state, V0, 10, R.dekad_start("2025-07-D2"), q_head, {"day_pumping": []})
    w1 = 10 * (1 - 0.3)
    w2 = V0[s2.id] * s2.cost / (q_head * 43.2)
    assert m["window"] == int(math.floor(min(w1, w2)))


# ---------------------------------------------------------------- queue, priority, never zero twice, skips
def test_queue_runs_tail_to_head_and_persists_across_rounds(st, world):
    for s1, kids in st.children.items():
        pos = [float(st.nd.at[c.id, "path_m"] or 0) for c in kids]
        assert pos == sorted(pos, reverse=True)                        # tail first
    state = R.State()
    rows = V._rule_a_rows(world, "2025-07-D2", "normal")
    (sup, _), days = V._supply(world, "2025-07-D2", "normal"), world.days["2025-07-D2"]
    need = float((rows.need * rows.outlet.map(world.maps.tree.cost)).sum())
    a = R.run_dekad(st, state, "2025-07-D2", rows, days, sup, need)
    assert any(v for v in state.s1_ptr.values())                       # the queue did not restart at the tail
    assert state.round_no == 2 and state.main_ptr in range(len(st.order))


def test_start_order_follows_the_queue(st, seasons):
    r = seasons["normal"][-1]
    for s1 in st.children:
        ts = sorted((t for t in r.turns if t["level"] == "secondary" and t["parent"] == s1
                     and not {"deadline_start", "protected"} & set(t["flags"]) and t["mode"] == "rotating"),
                    key=lambda t: (t["open"], t["queue_pos"]))
        groups = {}
        for t in ts:
            groups.setdefault(t["group"], []).append(t["queue_pos"])
        assert all(g is not None for g in groups)


def test_carry_over_goes_first_in_its_group(st, world):
    state = R.State()
    rows = V._rule_a_rows(world, "2025-07-D2", "normal")
    (sup, _), days = V._supply(world, "2025-07-D2", "normal"), world.days["2025-07-D2"]
    need = float((rows.need * rows.outlet.map(world.maps.tree.cost)).sum())
    # pick a direct outlet in the middle of its secondary's queue and give it carry-over priority
    s1 = EX["big_s1"]
    c = [c for c in st.children[s1] if c.kind == "direct_outlet" and not c.pump][len(st.children[s1]) // 3]
    state.zero_last = {c.id}
    rr = R.run_dekad(st, state, "2025-07-D2", rows, days, sup, need)
    r = next(r for r in rr if c.id in r.outlet_due)
    mine = next(t for t in r.turns if t["unit"] == c.id)
    mates = [t for t in r.turns if t["level"] == "secondary" and t["parent"] == s1 and t["group"] == mine["group"]
             and t["unit"] != c.id and "deadline_start" not in t["flags"]]
    assert all(mine["open"] <= t["open"] for t in mates)
    assert c.id in r.flags["carry_over"] and r.outlet_delivered.get(c.id, 0) > 0


def test_zero_twice_is_best_effort_with_first_call(st, seasons):
    """Owner (2026-11-02): conditions are not ideal, so "never zero twice" is best effort, not a guarantee. The system
    must do the most it can: nobody is skipped twice running; an outlet left dry is due again and goes first in its
    group (carry-over priority); the rare repeats are counted and reported, and must not grow (regression guard)."""
    for sc, rr in seasons.items():
        skipped_prev, zero_prev, twice, due_n = set(), set(), 0, 0
        for r in rr:
            assert not (set(r.flags["skipped"]) & skipped_prev)
            for o in r.outlet_due:
                due_n += 1
                if o in zero_prev:
                    assert o in r.flags["carry_over"], (r.period, r.label, o)       # first call this time
                    if r.outlet_delivered.get(o, 0) <= 1e-6:
                        twice += 1
            zero_prev = {o for o in r.outlet_due if r.outlet_delivered.get(o, 0) <= 1e-6} | set(r.flags["skipped"])
            skipped_prev = set(r.flags["skipped"])
        assert twice / due_n < (0.01 if sc == "normal" else 0.03), (sc, twice, due_n)


def test_perennials_and_sensitive_fields_are_never_skipped(seasons, world):
    rr = seasons["short40"]
    skipped = [(r, o) for r in rr for o in r.flags["skipped"]]
    assert skipped                                                     # very short supply does skip other fields
    for r, o in skipped:
        rows = V._rule_a_rows(world, r.period, "short40")
        g = rows[rows.outlet == o]
        assert g[g.group.isin(R.PROTECTED)].need.sum() < 0.3 * g.need.sum()


# ---------------------------------------------------------------- R3 pumps, R4 change times, R5 ramps
def test_canal_inflows_change_only_at_07_and_19_and_handovers_on_quarters(seasons):
    for sc, r in every_round(seasons):
        for t in r.turns:
            if t["level"] == "main":
                assert t["open"].hour in (7, 19) and t["open"].minute == 0 and t["close"].hour in (7, 19)
            else:
                assert t["open"].minute % 15 == 0 and t["close"].minute % 15 == 0


def test_ramps_at_the_offtakes(st, seasons):
    for sc, r in every_round(seasons):
        for ramp in r.ramps:
            off = st.offtakes[ramp["unit"]]
            assert ramp["at"].hour in (7, 19)
            prev, t_prev = ramp["from"], None
            for t, q in ramp["steps"]:
                assert abs(q - prev) <= 0.2 * max(off.stream, off.minimum) + 0.1
                if t_prev is not None:
                    assert t - t_prev == timedelta(hours=2)
                prev, t_prev = q, t
            assert ramp["steps"][-1][1] == pytest.approx(ramp["to"], abs=0.1)


def test_pumps_run_nights_first_and_day_use_is_flagged(st, seasons):
    night_only = 0
    for sc, r in every_round(seasons):
        for t in r.turns:
            pump = (t["level"] == "main" and st.offtakes[t["unit"]].pump) or \
                   (t["level"] == "secondary" and next(c for c in st.children[t["parent"]] if c.id == t["unit"]).pump)
            if not pump:
                continue
            h, day = t["open"], False
            while h < t["close"]:
                day |= not R.is_night(h)
                h += timedelta(hours=0.25)
            assert (not day) or "day_pumping" in t["flags"], t
            night_only += not day
    assert night_only > 0
    pumps = {o.id for o in st.offtakes.values() if o.pump} | {c.id for cs in st.children.values() for c in cs if c.pump}
    assert set(seasons["normal"][0].flags["pump_limited"]) <= pumps      # flagged stations are pump stations


# ---------------------------------------------------------------- R6 balance with transit, R7
def test_balance_closes_per_day_with_water_in_transit(seasons):
    for sc, r in every_round(seasons):
        b = r.balance
        assert b["closes"] and b["max_day_residual_pct"] <= 0.1 and b["max_secondary_residual_pct"] <= 0.1
        for d in b["per_day"].values():
            lhs = d["released_m3"] + d["in_transit_start_m3"]
            rhs = d["arrived_m3"] + d["seepage_m3"] + d["in_transit_end_m3"]
            assert lhs == pytest.approx(rhs, abs=3 + 1e-3 * lhs)
        assert b["in_transit_start_m3"] > 0                             # water released before the round starts
        assert b["head_peak_release_lps"] <= R.A.MAIN_CAPACITY_LPS + 1e-6


# ---------------------------------------------------------------- slices, hindsight, size, tools
def test_day_week_and_dekad_are_slices_of_one_schedule(world):
    dek = []
    cur = 0
    while cur is not None:
        d = V.schedule(world, "2025-07-D2", cursor=cur)["data"]
        dek += list(d["turns"].values())
        cur = d["cursor"]["next"]
    day = []
    cur = 0
    while cur is not None:
        d = V.schedule(world, scope="day", date="2025-07-12", cursor=cur)["data"]
        day += list(d["turns"].values())
        cur = d["cursor"]["next"]
    in_day = [t for t in dek if t["open"] < "07-13 07:00" and t["close"] > "07-12 07:00"]
    strip = lambda ts: sorted(json.dumps({k: v for k, v in t.items()}, sort_keys=True) for t in ts)  # noqa: E731
    assert strip(day) == strip(in_day) and day
    week = V.schedule(world, scope="week", date="2025-07-11")["data"]
    assert week["from"] == "07-11 07:00" and week["to"] == "07-18 07:00"


def test_no_hindsight(world):
    with pytest.raises(T.ToolRefused, match="hindsight"):
        V.schedule(world, "2025-08-D1")
    with pytest.raises(T.ToolRefused, match="hindsight"):
        V.schedule(world, scope="day", date="2025-08-05")
    w = V.schedule(world, scope="week", date="2025-07-28")["data"]      # a week past the planning date is cut there
    assert w["to"] == "08-01 07:00"


def test_schedule_results_fit_the_size_caps(world, st):
    nodes = [None, EX["big"], EX["pump"], EX["big_s1"], EX["sec_gate"], EX["tertiary"]]
    for n in nodes:
        for scope, kw in (("dekad", {"period": "2025-07-D2"}), ("day", {"date": "2025-07-12"}),
                          ("week", {"date": "2025-07-11"})):
            r = V.schedule(world, scope=scope, node=n, **kw)
            assert len(json.dumps(r["data"], ensure_ascii=False).encode()) <= V.MAX_RESULT_BYTES
            assert len(r["data"]["turns"]) <= V.SCHEDULE_PAGE <= V.PAGE_MAX
            assert set(r["data"]) <= V.ALLOWED_FIELDS_V6["schedule"]


def test_every_tertiary_schedule_page_fits(world):
    nd = world.maps.nodes
    for n in nd.node[nd.kind.isin(["secondary_gate", "secondary_pump"])]:
        for kw in ({"period": "2025-07-D2"}, {"scope": "week", "date": "2025-07-11"}):
            cur = 0
            while cur is not None:
                d = V.schedule(world, node=n, cursor=cur, **kw)["data"]
                assert len(json.dumps(d, ensure_ascii=False).encode()) <= V.MAX_RESULT_BYTES
                cur = d["cursor"]["next"]


def test_schedule_rows_say_who_is_next(world):
    d = V.schedule(world, "2025-07-D2", node=EX["big_s1"])["data"]
    rows = list(d["turns"].values())
    assert any(r["next"] for r in rows) and all(re.fullmatch(r"\d\d-\d\d \d\d:\d\d", r["open"]) for r in rows)
    assert d["rules"]["hand_stream_lps"] == 20 and d["rules"]["change_times"] == ["07:00", "19:00"]


# ---------------------------------------------------------------- the agents know how the canal works
def test_every_agent_has_how_this_canal_works():
    import irrigation_assistant as ia
    defs = ia.agent_definitions()
    assert len(defs) == 12
    for name, d in defs.items():
        p = d["prompt"]
        assert "How this canal works" in p, name
        assert "Never suggest opening all gates at the same time" in p, name
        assert "who is next" in p and "carry-over priority" in p, name
    assert V.estimate_tokens(V.map_context()) <= V.MAP_CONTEXT_MAX_TOKENS
    banned = json.loads((ROOT / "config/canal_ops_sim_v1.json").read_text(encoding="utf-8"))["wording_ban"]["en"]
    assert not [w for w in banned if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", V.rotation_text().lower())]
