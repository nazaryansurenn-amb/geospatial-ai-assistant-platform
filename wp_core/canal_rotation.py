"""Canal rotation: when, at what stream and in what order the water moves (owner-approved canal rotation logic,
docs/CANAL_ROTATION_LOGIC_2026_11_02_EN.md section 10; change log R1-R9; config/canal_ops_rotation_v1.json).

The tree allocation (wp_core/canal_tree_allocation.py) decides HOW MUCH each outlet gets per dekad. This module turns
that into turns:
  - a round is half a dekad; every outlet has a period of 1-4 rounds (July: owner table; other months: FAO-56 rule)
    and a phase from its rotation group, so groups alternate along the canal;
  - main canal: sections take turns every round (stage II -> lower stage I -> upper stage I), stage II gets its own
    window when the duty test allows it, offtakes run in whole 12-h slots (07:00 / 19:00) at whole hand streams
    (20 l/s), continuous when their duty is >= 0.85, pumps in night slots first (day use flagged), ramps of at most
    20 % of capacity per 2 h at the offtakes;
  - secondary canals: the offtake's flow moves between tertiary inlets and direct outlets in hand streams at quarter
    hours, tail first in a rolling queue that does not restart each round;
  - tertiary canals: the whole tertiary stream goes to one farm outlet at a time, tail first;
  - a gate is closed or at or above its minimum opening (tertiary: 20 l/s and 30 % of capacity); rotation runs at
    the working stream for fewer hours (hours = V / (3.6 Q) + fill time);
  - shortage: keep streams, shorten turns (volumes come from the priority levels); robust crops P + 1 when short;
    other green fields skip one turn in rotation when very short; perennials and sensitive fields are never skipped;
    no outlet gets zero two turns running; a missed turn goes first in its group next round (carry-over priority);
  - balance: main canal with water in transit (released at the head, not yet at its offtake, 0.7 m/s), secondary
    canals with fill-and-drain and tail-escape spill; per day and per round.
Every number is fixed code. Simulation: not a water order, not a real WUA schedule, not a legal ranking.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from wp_core import canal_tree_allocation as A

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/canal_ops_rotation_v1.json").read_text(encoding="utf-8"))
HS = float(CFG["hand_stream_lps"])
QH = float(CFG["round"]["quarter_h"])
SLOT_H = float(CFG["round"]["slot_hours"])
QPS = int(round(SLOT_H / QH))                       # quarters per slot
SPEED_KMH = CFG["speed_m_per_s"] * 3.6
SECTION_ORDER = CFG["stage2_window"]["order"]
PROTECTED = {"perennial", "sensitive_field"}
LABEL = CFG["label"]
TOL = 1e-6


def hs_floor(q):
    return math.floor(q / HS + 1e-9) * HS


def hs_ceil(q):
    return math.ceil(q / HS - 1e-9) * HS


# ---------------------------------------------------------------- calendar
def dekad_start(period: str) -> datetime:
    y, m, k = int(period[:4]), int(period[5:7]), int(period[-1])
    return datetime(y, m, 1 + 10 * (k - 1), int(CFG["round"]["day_slot_start_h"]))


def round_bounds(period: str, days: int) -> list[tuple[datetime, int]]:
    """The dekad's two rounds as (start, number of 12-h slots): round A the first half of the slots, B the rest."""
    n = int(round(days * 24 / SLOT_H))
    a = n // 2
    t0 = dekad_start(period)
    return [(t0, a), (t0 + timedelta(hours=a * SLOT_H), n - a)]


def is_night(t: datetime) -> bool:
    return t.hour >= CFG["round"]["night_slot_start_h"] or t.hour < CFG["round"]["day_slot_start_h"]


# ---------------------------------------------------------------- static structure (once per network version)
@dataclass
class Child:                                        # a child of a secondary canal: tertiary inlet or direct outlet
    id: str
    s1: str
    pos: int                                        # tail-to-head position on the secondary (0 = tail)
    group: int
    stream: float                                   # working stream at the child (l/s)
    minimum: float
    keep: float                                     # secondary entry -> child
    pump: bool
    pump_limited: bool
    kind: str
    fill_q: int                                     # tertiary fill time in quarters (inlets only)
    outlets: list                                   # [(outlet id, keep inlet -> outlet)], tail first; direct outlet: itself


@dataclass
class Offtake:
    id: str
    s1: str
    section: str
    stage: str
    canal_km: float
    stream: float
    minimum: float
    cost: float                                     # head m3 per m3 at the offtake
    pump: bool
    pump_limited: bool
    fill_h: float                                   # secondary canal fill time (h) after 12 h dry
    travel_h: float                                 # head -> offtake at 0.7 m/s


class Static:
    def __init__(self, tree: A.Tree):
        nd = tree.nodes
        self.tree, self.nd, self.cost = tree, nd, tree.cost
        sc = CFG["streams"]
        stage2_start = float(nd.loc[nd.kind == "main_canal"].set_index("stage").upstream_m.get("II", 0.0))
        self.cost_II = float(tree.cost.get("II", 1.0))
        self.cap_II = float(nd.capacity_lps.get("II", np.inf))
        kids = nd[nd.parent.notna()].groupby("parent").node.apply(list).to_dict()
        self.offtakes, self.children, self.outlet_child, self.outlet_group = {}, {}, {}, {}
        for r in nd[nd.kind.isin(["main_gate", "main_pump"])].itertuples():
            s1 = f"{r.node}-S1"
            s1cap = float(nd.at[s1, "capacity_lps"])
            if r.pump:
                stream, limited = self._pump_stream(r, s1cap)
                minimum = min(float(r.unit_lps), stream)
            else:
                stream, limited = max(HS, hs_floor(float(r.capacity_lps))), False
                minimum = min(stream, hs_ceil(max(sc["min_open_share"] * float(r.capacity_lps), sc["main_offtake_min_lps"])))
            sec_kids = [k for k in kids.get(s1, []) if nd.at[k, "kind"] != "tail_escape"]
            s1_len = max((float(nd.at[k, "path_m"] or 0) for k in sec_kids), default=0.0) / 1000.0
            canal_km = (float(r.chainage_m) + (stage2_start if r.stage == "II" else 0.0)) / 1000.0
            self.offtakes[r.node] = Offtake(r.node, s1, r.section, r.stage, canal_km, stream, minimum,
                                            float(tree.cost[r.node]), bool(r.pump), limited, s1_len / SPEED_KMH,
                                            canal_km / SPEED_KMH)
            self.children[s1] = self._children(s1, s1cap, sec_kids, kids)
        self.order = [o for sec in SECTION_ORDER for o in sorted(
            (o for o in self.offtakes.values() if o.section == sec), key=lambda o: (-o.canal_km, o.id))]
        self.order = [o.id for o in self.order]
        self.end_cost = max(o.cost for o in self.offtakes.values())         # the end of stage II

    @staticmethod
    def _pump_stream(r, receiving_cap):
        unit = float(r.unit_lps)
        n = max(1, int(receiving_cap // unit))
        n = min(n, int(r.pump_units or n))
        stream = min(n * unit, receiving_cap)
        limited = (n * unit > receiving_cap + TOL) or (float(r.station_lps or 0) > receiving_cap + TOL)
        return stream, bool(limited)

    def _children(self, s1, s1cap, sec_kids, kids):
        nd, sc = self.nd, CFG["streams"]
        rows = []
        for k in sorted(sec_kids, key=lambda k: (-float(nd.at[k, "path_m"] or 0), k)):
            r = nd.loc[k]
            if r.kind in ("secondary_gate", "secondary_pump"):
                t1 = f"{k}-T1"
                t1cap = float(nd.at[t1, "capacity_lps"])
                if r.pump:
                    stream, limited = self._pump_stream(r, t1cap)
                else:
                    n = 2 if t1cap >= sc["secondary_gate_streams"]["two_when_tertiary_capacity_from_lps"] else 1
                    stream, limited = n * HS, False
                outs = sorted(kids.get(t1, []), key=lambda o: (-float(nd.at[o, "path_m"] or 0), o))
                t1_len = max((float(nd.at[o, "path_m"] or 0) for o in outs), default=0.0) / 1000.0
                fill_q = int(math.ceil(t1_len / SPEED_KMH / QH - 1e-9))
                outlets = [(o, float(nd.at[o, "keep"])) for o in outs]
                cap = float(r.capacity_lps)
            else:
                cap = float(r.capacity_lps)
                if r.pump:
                    stream, limited = self._pump_stream(r, cap)
                else:
                    stream, limited = max(1, int(cap // HS)) * HS, False
                fill_q, outlets = 0, [(k, 1.0)]
            minimum = min(stream, max(sc["tertiary_min_lps"], sc["min_open_share"] * cap))
            rows.append(Child(k, s1, len(rows), 0, stream, minimum, float(nd.at[k, "keep"]), bool(r.pump), limited,
                              r.kind, fill_q, outlets))
        # positions tail-to-head; rotation groups: adjacent children whose streams fit in the secondary, at most k
        if rows:
            k_max = max(1, int(s1cap // float(np.median([c.stream for c in rows]))))
            g, load, size = 0, 0.0, 0
            for c in rows:
                if size and (load + c.stream > s1cap + TOL or size >= k_max):
                    g, load, size = g + 1, 0.0, 0
                c.group, load, size = g, load + c.stream, size + 1
        for c in rows:
            for o, _ in c.outlets:
                self.outlet_child[o] = c
            for i, (o, _) in enumerate(c.outlets):   # rotation group of an outlet: on a tertiary, one outlet per group
                self.outlet_group[o] = i if c.fill_q or c.kind != "direct_outlet" else c.group
        return rows


# ---------------------------------------------------------------- crop periods (section 4)
def crop_key(group: str, crop: str) -> str:
    if group == "sensitive_field":
        return "sensitive_field"
    if group == "perennial":
        return crop if crop in ("vineyard", "orchard") else "perennial"
    return "alfalfa" if crop == "alfalfa" else "other_green_field"


def row_period(r, month: int, round_days: float) -> int:
    pc = CFG["periods"]
    if month == 7:
        return int(pc["july_rounds"][crop_key(r.group, getattr(r, "crop", ""))])
    taw, p, etc, net = (float(getattr(r, k, np.nan)) for k in ("taw_mm", "p", "etc_day", "net_day"))
    if not (taw > 0 and net > 0):
        return pc["max_rounds"]
    p_adj = float(np.clip(p + 0.04 * (5 - etc), 0.1, 0.8))
    interval = min(p_adj * taw, pc["max_gross_depth_mm"] * pc["field_efficiency"]) / net
    return int(np.clip(math.floor(interval / round_days), pc["min_rounds"], pc["max_rounds"]))


def outlet_periods(rows: pd.DataFrame, month: int, round_days: float) -> dict:
    """Outlet period = the shortest period among the crop groups with at least 30 % of the outlet's need."""
    pc = CFG["periods"]
    key = [crop_key(g, c) for g, c in zip(rows.group, rows.get("crop", pd.Series([""] * len(rows), index=rows.index)))]
    if month == 7:
        per = np.array([pc["july_rounds"][k] for k in key], dtype=float)
    else:
        taw, p, etc, net = (rows.get(c, pd.Series(np.nan, index=rows.index)).to_numpy(float)
                            for c in ("taw_mm", "p", "etc_day", "net_day"))
        p_adj = np.clip(p + 0.04 * (5 - etc), 0.1, 0.8)
        with np.errstate(divide="ignore", invalid="ignore"):
            interval = np.minimum(p_adj * taw, pc["max_gross_depth_mm"] * pc["field_efficiency"]) / net
        per = np.where((taw > 0) & (net > 0), np.clip(np.floor(interval / round_days), pc["min_rounds"], pc["max_rounds"]),
                       pc["max_rounds"])
    d = pd.DataFrame({"outlet": rows.outlet.to_numpy(), "need": rows.need.to_numpy(float), "P": per})
    d["share"] = d.need / d.groupby("outlet").need.transform("sum").replace(0, 1)
    big = d[d.share >= pc["group_share_counted"]].groupby("outlet").P.min()
    allp = d.groupby("outlet").P.min()
    return big.reindex(allp.index).fillna(allp).astype(int).to_dict()


# ---------------------------------------------------------------- state carried from round to round
@dataclass
class State:
    round_no: int = 0
    main_ptr: int = 0
    s1_ptr: dict = field(default_factory=dict)
    skip_ptr: dict = field(default_factory=dict)
    zero_last: set = field(default_factory=set)          # outlets due and given nothing in their last turn
    carry: dict = field(default_factory=dict)            # outlet -> m3 not delivered (next turn; carry-over priority)
    skipped_last: set = field(default_factory=set)
    last_start_night: dict = field(default_factory=dict)  # offtake -> its last round's turn started at night
    wet: set = field(default_factory=set)                 # secondary canals still wet at the end of the last round
    history: dict = field(default_factory=dict)          # round_no -> {outlet: m3 rate per day}

    def copy(self):
        return State(self.round_no, self.main_ptr, dict(self.s1_ptr), dict(self.skip_ptr), set(self.zero_last),
                     dict(self.carry), set(self.skipped_last), dict(self.last_start_night), set(self.wet),
                     dict(self.history))


# ---------------------------------------------------------------- one round
@dataclass
class RoundResult:
    label: str
    period: str
    start: datetime
    slots: int
    turns: list
    offtake_flow: dict                                   # offtake -> [l/s per slot]
    stage2_window_slots: int
    lead_order: list
    ramps: list
    balance: dict
    flags: dict
    outlet_due: dict
    outlet_delivered: dict


def _rate(rows: pd.DataFrame, days: int) -> dict:
    """m3 per day at each outlet from a dekad allocation."""
    return (rows.groupby("outlet").alloc.sum() / days).to_dict()


def run_round(st: Static, state: State, period: str, label: str, start: datetime, n_slots: int, rows: pd.DataFrame,
              days: int, q_head: float, rho: float) -> RoundResult:
    """Schedule one round. rows: the dekad's allocation (outlet x group with need, alloc and crop columns);
    q_head: head flow accepted (l/s, at most 12 m3/s); rho: head supply / head need for the dekad."""
    r_no = state.round_no
    round_days = n_slots * SLOT_H / 24
    rate = _rate(rows, days)
    state.history[r_no] = rate
    month = int(period[5:7])
    P = outlet_periods(rows, month, round_days)
    protected = {o: (g[g.group.isin(PROTECTED)].need.sum() >= CFG["periods"]["group_share_counted"] * g.need.sum())
                 for o, g in rows.groupby("outlet")}
    ratio = (rows.groupby("outlet").alloc.sum() / rows.groupby("outlet").need.sum().where(lambda v: v > 0)).to_dict()
    ha = rows.groupby("outlet").ha.sum().to_dict()
    sh = CFG["shortage"]
    flags = {"day_pumping": [], "pump_limited": sorted({o.id for o in st.offtakes.values() if o.pump_limited}),
             "carry_over": [], "skipped": [], "missed": [], "deferred_small": [], "p_plus_one": 0}

    # a child whose average round volume needs >= 85 % of the round at its working stream runs continuously (5.4):
    # its outlets are due every round
    round_h = n_slots * SLOT_H
    child_duty = {c.id: sum(rate.get(o, 0.0) * round_days / k for o, k in c.outlets) / (c.stream * round_h * 3.6)
                  for kids in st.children.values() for c in kids}
    cont_child = {cid for cid, d in child_duty.items() if d >= CFG["duty_continuous"]}
    flags["continuous_children"] = len(cont_child)
    # --- which outlets are due, and their turn volumes (step 4)
    due, vol, first = {}, {}, set()
    for o in rate:
        c = st.outlet_child.get(o)
        if c is None:
            continue
        p = 1 if c.id in cont_child else P.get(o, 1)
        if not protected.get(o) and sh["short_from"] <= rho < sh["normal_from"] and ratio.get(o, 1) < sh["p_plus_one_below_share"]:
            p, flags["p_plus_one"] = min(p + 1, CFG["periods"]["max_rounds"]), flags["p_plus_one"] + 1
        phase = st.outlet_group.get(o, c.group) % p
        is_due = (r_no - phase) % p == 0
        carried = o in state.carry
        if carried and not is_due and protected.get(o):
            is_due = True                                    # inserted turn for a protected outlet that missed
        if not is_due:
            continue
        v = sum(state.history.get(r_no - k, rate).get(o, 0.0) for k in range(p)) * round_days + state.carry.pop(o, 0.0)
        v = min(v, CFG["periods"]["max_gross_depth_mm"] * ha.get(o, 0.0) * 10)
        if v < CFG["smallest_turn_m3"]:
            if o in state.zero_last:
                v = CFG["smallest_turn_m3"]
            else:
                state.carry[o] = v
                flags["deferred_small"].append(o)
                continue
        due[o], vol[o] = p, v
        if carried or o in state.zero_last:
            first.add(o)
            flags["carry_over"].append(o)
    # very short supply: other green field outlets of one rotation group skip one turn, in rotation (8.2)
    if rho < sh["short_from"]:
        for s1, kids in st.children.items():
            groups = sorted({c.group for c in kids})
            if not groups:
                continue
            ptr = state.skip_ptr.get(s1, 0)
            g = groups[::-1][ptr % len(groups)]                 # from the head end; tail groups skip last
            state.skip_ptr[s1] = ptr + 1
            for c in kids:
                if c.group != g:
                    continue
                for o, _ in c.outlets:
                    if o in due and not protected.get(o) and o not in state.zero_last and o not in state.skipped_last:
                        due.pop(o)
                        state.carry[o] = state.carry.get(o, 0.0)          # nothing kept as debt; first next time
                        flags["skipped"].append(o)
    state.skipped_last = set(flags["skipped"])

    # --- fit the round's volumes to the round's supply (8.2: keep streams, shorten turns by priority)
    hc = st.cost
    head_v = {o: v * float(hc[o]) for o, v in vol.items()}
    avail = q_head * n_slots * SLOT_H * 3.6 * CFG["round_fill_share"]
    over = sum(head_v.values()) - avail
    flags["round_head_need_m3"], flags["round_head_avail_m3"] = round(sum(head_v.values())), round(avail)
    keep_share = A.ca.CFG["floors"]["zero_streak_floor_share"]          # a shortened turn keeps at least 30 %
    total_v = sum(head_v.values())
    if over > 0:
        for prot in (False, True):                        # other green fields first, protected crops last
            part = sum(h * (1 - keep_share) for o, h in head_v.items() if bool(protected.get(o)) == prot)
            if part <= 0:
                continue
            f = max(0.0, 1 - over / part)                 # share of the cuttable part that stays
            for o in [o for o in vol if bool(protected.get(o)) == prot]:
                new = vol[o] * (keep_share + (1 - keep_share) * f)          # a shorter turn; no debt is kept
                over -= (vol[o] - new) * float(hc[o])
                vol[o] = new
            if over <= 0:
                break
        flags["shortened"] = round(sum(v * float(hc[o]) for o, v in vol.items()) / max(total_v, 1), 3)
    # --- child and offtake volumes
    child_v, off_v = {}, {}
    for s1, kids in st.children.items():
        for c in kids:
            outs = [(o, k) for o, k in c.outlets if o in due]
            if not outs:
                continue
            v = sum(vol[o] / k for o, k in outs)
            if c.fill_q:
                v += c.stream * c.fill_q * QH * 3.6
            child_v[c.id] = v
    for o in st.offtakes.values():
        v = sum(child_v.get(c.id, 0.0) / c.keep for c in st.children[o.s1])
        if v > 0 and o.s1 not in state.wet:
            v += o.stream * o.fill_h * 3.6
        off_v[o.id] = v

    # an offtake stays open at least as long as its longest child turn (plus the secondary's fill time)
    min_h = {o.id: max((child_v[c.id] / (3.6 * c.stream) for c in st.children[o.s1] if c.id in child_v), default=0.0)
             + (o.fill_h if o.s1 not in state.wet else 0.0) for o in st.offtakes.values()}
    # --- main canal (step 5)
    main = _main_canal(st, state, off_v, n_slots, start, q_head, flags, min_h)
    # --- secondary and tertiary canals (steps 6, 7)
    ptr0 = dict(state.s1_ptr)
    res_by = {}

    def place(o, flows):
        """Run the secondary on these offtake flows; the offtake then takes only what its secondary runs in each
        slot (whole hand streams, at least its minimum); the rest stays in the main canal (end of stage II)."""
        if o.s1 in ptr0:
            state.s1_ptr[o.s1] = ptr0[o.s1]
        else:
            state.s1_ptr.pop(o.s1, None)
        res = _secondary(st, state, o, flows, start, n_slots, child_v, due, vol, first, protected, flags)
        for k, (old, new) in enumerate(zip(flows, res["flows"])):
            main["spill"][k] += (old - new) * o.cost
        main["flow"][o.id] = res["flows"]
        res_by[o.id] = res

    for o in st.offtakes.values():
        place(o, main["flow"][o.id])
    # water the queue cannot place in a slot is not taken at the head: the scenario supply is what is available
    # there, not a flow forced into the canal (with an owner flow input, D1, it would be spill at the end of stage II)
    not_taken = [max(x, 0.0) for x in main["spill"]]
    main["spill"] = [0.0] * n_slots
    turns = []
    delivered = {}
    sec_bal = []
    for o in st.offtakes.values():
        res = res_by[o.id]
        flows = main["flow"][o.id]
        turns += res["turns"]
        delivered.update(res["delivered"])
        sec_bal.append(res["balance"])
        if flows and flows[-1] > 0:
            state.wet.add(o.s1)
        else:
            state.wet.discard(o.s1)
    # --- outcome per outlet: zero flags and carry-over (step 10)
    zero = set()
    for o in due:
        got = delivered.get(o, 0.0)
        if got <= TOL:
            zero.add(o)
            flags["missed"].append(o)
        if got < vol[o] - CFG["smallest_turn_m3"]:
            # carry-over priority; the water itself is not kept as a debt beyond one round's share (3.4)
            state.carry[o] = min(state.carry.get(o, 0.0) + (vol[o] - got), rate.get(o, 0.0) * round_days)
    zero |= set(flags["skipped"])
    state.zero_last = (state.zero_last - set(due)) | zero
    state.round_no = r_no + 1
    main_turns, main["ramps"] = _main_rows(st, state, main, start, n_slots)
    turns = main_turns + turns
    _link_next(turns)
    bal = _balance(st, main, sec_bal, start, n_slots)
    bal["not_taken_at_head_m3"] = round(sum(not_taken) * SLOT_H * 3.6)
    return RoundResult(label, period, start, n_slots, turns, main["flow"], main["window"], main["lead"], main["ramps"],
                       bal, flags, {o: due[o] for o in due}, delivered)


# ---------------------------------------------------------------- main canal QUEUE_FILL (step 5, 10.4)
def _main_canal(st: Static, state: State, V: dict, n: int, start: datetime, q_head: float, flags: dict,
                min_h: dict | None = None) -> dict:
    """QUEUE_FILL on the main canal (10.4) with the head flow q_head (head-equivalent l/s) in every slot."""
    offs = st.offtakes
    slot_t = [start + timedelta(hours=k * SLOT_H) for k in range(n)]
    night = [is_night(t) for t in slot_t]
    hrs = n * SLOT_H
    min_h = min_h or {}
    need_slots = {o: (min(n, max(math.ceil(V[o] / (offs[o].stream * SLOT_H * 3.6) - 1e-9),
                                  math.ceil(min_h.get(o, 0.0) / SLOT_H - 1e-9))) if V[o] > 0 else 0) for o in offs}
    # whole hand streams spread over the slots it needs (5.3): between the minimum and the working stream
    plan_q = {o: (min(offs[o].stream, max(offs[o].minimum, hs_ceil(V[o] / (need_slots[o] * SLOT_H * 3.6))))
                  if need_slots[o] else 0.0) for o in offs}
    plan_q.update({o: offs[o].stream for o in offs if offs[o].pump})
    duty = {o: V[o] / (offs[o].stream * hrs * 3.6) if V[o] > 0 else 0.0 for o in offs}
    cont = {o for o in offs if duty[o] >= CFG["duty_continuous"] and not offs[o].pump}
    # stage II own window (7.2)
    st1 = [o for o in offs if offs[o].stage == "I" and V[o] > 0]
    # d* = the largest share of the round a stage I offtake must be open (its volume at full stream, or its
    # longest child turn, whichever is longer)
    d_star = max((max(duty[o], need_slots[o] / n) for o in st1), default=0.0)
    w1 = n * (1 - min(d_star, 1.0))
    v2_head = sum(V[o] * offs[o].cost for o in offs if offs[o].stage == "II")
    w2 = v2_head / (q_head * SLOT_H * 3.6) if q_head > 0 else 0.0
    w = int(math.floor(min(w1, w2) + 1e-9))
    v1_head = sum(V[o] * offs[o].cost for o in st1)
    ok = (q_head / st.cost_II <= st.cap_II + TOL and v1_head <= q_head * SLOT_H * 3.6 * (n - w) + TOL
          and w >= CFG["stage2_window"]["min_slots"])
    window = w if ok else 0
    order = st.order[state.main_ptr % len(st.order):] + st.order[:state.main_ptr % len(st.order)]
    remaining = dict(V)
    flow = {o: [0.0] * n for o in offs}
    started, started_order, rows = set(), [], []
    open_since = {}
    # continuous offtakes: whole hand streams spread over their open slots, earlier slots first
    cont_plan = {}
    for o in cont:
        slots = [k for k in range(n) if not (k < window and offs[o].stage == "I")]
        H = int(round(V[o] / (HS * SLOT_H * 3.6)))
        base, extra = divmod(H, max(len(slots), 1))
        cont_plan[o] = {k: min(offs[o].stream, max(offs[o].minimum, (base + (i < extra)) * HS)) for i, k in enumerate(slots)}

    def eligible(o, k):
        if remaining[o] <= TOL or (k < window and offs[o].stage == "I"):
            return False
        if offs[o].pump and not night[k]:
            nights_left = sum(1 for j in range(k, n) if night[j] and not (j < window and offs[o].stage == "I"))
            return remaining[o] > nights_left * offs[o].stream * SLOT_H * 3.6 + TOL
        return True

    for k in range(n):
        free, free2 = q_head, st.cap_II * st.cost_II                # head-equivalent l/s; stage II entry in head terms

        def mn(o):
            return offs[o].minimum

        def sm(o):
            return offs[o].stream

        def take(o, q):
            nonlocal free, free2
            c = q * offs[o].cost
            if c > free + TOL or (offs[o].stage == "II" and c > free2 + TOL):
                return False
            free -= c
            if offs[o].stage == "II":
                free2 -= c
            flow[o][k] = q
            return True

        def fit(o, q):
            lim = free / offs[o].cost
            if offs[o].stage == "II":
                lim = min(lim, free2 / offs[o].cost)
            return min(q, hs_floor(lim) if not offs[o].pump else (q if q <= lim + TOL else 0.0))

        # 1-2. running and continuous offtakes
        for o in order:
            if o in cont and k in cont_plan.get(o, {}) and remaining[o] > TOL:
                q = fit(o, min(cont_plan[o][k], max(mn(o), hs_ceil(remaining[o] / (SLOT_H * 3.6)))))
                if q >= mn(o) - TOL and take(o, q):
                    started.add(o)
            elif o in started and eligible(o, k):
                q = min(plan_q[o], max(mn(o), hs_ceil(remaining[o] / (SLOT_H * 3.6))))
                if offs[o].pump:
                    q = sm(o)
                q = fit(o, q)
                if q >= mn(o) - TOL:
                    take(o, q)
        # 3. deadline starts
        for o in order:
            if o in started or o in cont or not eligible(o, k):
                continue
            if k >= n - need_slots[o]:
                q = fit(o, max(plan_q[o], min(sm(o), hs_ceil(remaining[o] / (max(n - k, 1) * SLOT_H * 3.6)))))
                if q >= mn(o) - TOL and take(o, q):
                    started.add(o)
                    started_order.append(o)
                    rows.append((o, "deadline"))
        # 4. queue starts, in order; a later offtake never starts before the one ahead of it
        for o in order:
            if o in started or o in cont or remaining[o] <= TOL:
                continue
            if not eligible(o, k):
                continue                                   # waiting for its stage II window or a night slot
            last = state.last_start_night.get(o)
            if last is not None and night[k] == last and k + 1 < n - need_slots[o] and not offs[o].pump:
                continue                                   # alternate day and night starts round by round (6.4)
            q = fit(o, plan_q[o])
            if q < mn(o) - TOL or not take(o, q):
                break
            started.add(o)
            started_order.append(o)
            rows.append((o, "queue"))
        # 6. leftover: lift a running turn that fell behind (it got less earlier) to what it needs to finish in the
        #    slots left, never above its working stream; more would only spill at its secondary's tail escape
        for o in order:
            if flow[o][k] > 0 and not offs[o].pump:
                left = sum(1 for j in range(k, n) if not (j < window and offs[o].stage == "I"))
                want = min(sm(o), hs_ceil(remaining[o] / (max(left, 1) * SLOT_H * 3.6)))
                if want > flow[o][k]:
                    extra = fit(o, want) - flow[o][k]
                    if extra > 0:
                        free -= extra * offs[o].cost
                        if offs[o].stage == "II":
                            free2 -= extra * offs[o].cost
                        flow[o][k] += extra
        for o in offs:
            if flow[o][k] > 0:
                remaining[o] = max(0.0, remaining[o] - flow[o][k] * SLOT_H * 3.6)
                if offs[o].pump and not night[k]:
                    flags["day_pumping"].append(f"{o}@{slot_t[k]:%Y-%m-%d %H:%M}")
        flow.setdefault("_spill", [0.0] * n)[k] = max(free, 0.0)
    spill = flow.pop("_spill")
    if started_order:                                   # rolling queue: the next round continues after the last start
        state.main_ptr = (state.main_ptr + order.index(started_order[-1]) + 1) % len(order)
    lead = [s for s in SECTION_ORDER if any(offs[o].section == s and V[o] > 0 for o in offs)]
    return {"flow": flow, "spill": spill, "window": window, "lead": lead, "slot_t": slot_t, "night": night,
            "remaining": remaining, "q_head": q_head, "cont": cont, "order": order}


def _main_rows(st: Static, state: State, main: dict, start: datetime, n: int):
    """Main-canal turns (one row per run of open slots) and ramps, from the final slot flows."""
    offs, flow, night, slot_t, cont, order = (st.offtakes, main["flow"], main["night"], main["slot_t"], main["cont"],
                                              main["order"])
    turns, ramps = [], []
    pos = {o: i for i, o in enumerate(order)}
    for o in offs:
        f = flow[o]
        k = 0
        while k < n:
            if f[k] <= 0:
                k += 1
                continue
            j = k
            while j < n and f[j] > 0:
                j += 1
            fl = []
            if offs[o].pump and any(not night[x] for x in range(k, j)):
                fl.append("day_pumping")
            if offs[o].pump_limited:
                fl.append("pump_limited")
            turns.append({"level": "main", "unit": o, "parent": offs[o].section, "queue_pos": pos[o],
                          "open": slot_t[k], "close": slot_t[j - 1] + timedelta(hours=SLOT_H),
                          "lps": round(max(f[k:j]), 1), "m3": round(sum(f[k:j]) * SLOT_H * 3.6),
                          "night_h": sum(SLOT_H for x in range(k, j) if night[x]),
                          "mode": "continuous" if o in cont else "rotating", "flags": fl,
                          "slot_lps": [round(x, 1) for x in f[k:j]]})
            k = j
        first_open = next((x for x in range(n) if f[x] > 0), None)
        if first_open is not None and o not in cont:
            state.last_start_night[o] = night[first_open]
        prev = 0.0
        for k2 in range(n + 1):
            cur = f[k2] if k2 < n else 0.0
            if abs(cur - prev) > TOL:
                t = slot_t[k2] if k2 < n else start + timedelta(hours=n * SLOT_H)
                ramps.append(_ramp(o, t, prev, cur, offs[o]))
            prev = cur
    return turns, ramps


def _ramp(o, t, frm, to, off: Offtake) -> dict:
    """A change at a change time, in 2-h steps of at most 20 % of capacity (head regulator and main offtakes)."""
    step = CFG["ramp"]["share_of_capacity"] * max(off.stream, off.minimum)
    n = max(1, math.ceil(abs(to - frm) / step - 1e-9))
    steps = [(t + timedelta(hours=CFG["ramp"]["step_h"] * i), round(frm + (to - frm) * (i + 1) / n, 1)) for i in range(n)]
    return {"unit": o, "at": t, "from": round(frm, 1), "to": round(to, 1), "steps": steps}


# ---------------------------------------------------------------- secondary and tertiary QUEUE_FILL (steps 6, 7)
def _secondary(st, state, off, flows, start, n, child_v, due, vol, first, protected, flags):
    kids = [c for c in st.children[off.s1] if child_v.get(c.id, 0) > 0]
    nq = n * QPS
    budget = [flows[q // QPS] for q in range(nq)]
    open_q = [q for q in range(nq) if budget[q] > 0]
    turns, delivered = [], {}
    bal = {"in_m3": sum(flows) * SLOT_H * 3.6, "fill_m3": 0.0, "to_children_m3": 0.0, "spill_m3": 0.0,
           "tertiary_fill_m3": 0.0, "per_day": {}}
    if not open_q:
        return {"turns": turns, "delivered": delivered, "balance": bal, "flows": list(flows), "taken": {}}
    # fill time after 12 h dry: the first quarters of an opening fill the canal (fill-and-drain loss)
    fill = [0.0] * nq
    dry_q = int(CFG["dry_after_h"] / QH)
    fill_q = int(math.ceil(off.fill_h / QH - 1e-9))
    last_open = -10 ** 9 if off.s1 not in state.wet else -1
    q = 0
    while q < nq:
        if budget[q] > 0:
            if q - last_open > dry_q:
                for x in range(q, min(nq, q + fill_q)):
                    if budget[x] > 0:
                        fill[x] = budget[x]
            while q < nq and budget[q] > 0:
                q += 1
            last_open = q - 1
        q += 1
    # queue: rolling, tail first; carry-over and last-zero outlets first in their rotation group
    ptr = state.s1_ptr.get(off.s1, 0) % max(len(st.children[off.s1]), 1)
    ordered = st.children[off.s1][ptr:] + st.children[off.s1][:ptr]
    ordered = [c for c in ordered if c in kids]
    pri = [c for c in ordered if any(o in first for o, _ in c.outlets)]
    queue = _group_priority(ordered, pri)
    rem = {c.id: child_v[c.id] for c in kids}
    dur = {c.id: max(1, int(round(rem[c.id] / (3.6 * c.stream) / QH))) for c in kids}
    last_q = max(open_q)
    latest = {c.id: last_q + 1 - dur[c.id] for c in kids}
    running, done, started, interrupted = {}, set(), [], set()
    c_by = {c.id: c for c in kids}
    night_q = [is_night(start + timedelta(hours=x * QH)) for x in range(nq)]
    use = [0.0] * nq
    # events: slot changes, fill changes, deadlines and turn ends; nothing else can change the canal in between
    marks = set(range(0, nq, QPS)) | {x for x in range(1, nq) if fill[x] != fill[x - 1]}
    marks |= {x for x in latest.values() if 0 <= x < nq}
    nights_from = [0] * (nq + 1)
    for x in range(nq - 1, -1, -1):
        nights_from[x] = nights_from[x + 1] + (1 if night_q[x] and budget[x] > 0 else 0)
    q = 0
    while q < nq:
        avail = budget[q] - fill[q]
        for cid in [c for c, r in running.items() if r["end"] <= q]:
            _close(running.pop(cid), q, turns, start, night_q)
            done.add(cid)
        draw = sum(r["draw"] for r in running.values())
        # inflow drop at a slot change (a pump's day slot, the end of an offtake turn): interrupt the newest
        # non-protected turns first; an interrupted turn resumes, first in the queue, when water is back
        while draw > avail + TOL and running:
            cid = sorted(running, key=lambda x: (running[x]["protected"], -running[x]["start"]))[0]
            r = running.pop(cid)
            r["mode"] = "interrupted"
            _close(r, q, turns, start, night_q)
            dur[cid] = max(0, r["end"] - q)
            if dur[cid] <= 0:
                done.add(cid)
            else:
                interrupted.add(cid)
                if c_by[cid].fill_q:                      # a tertiary that stood dry fills again when it resumes
                    dur[cid] += c_by[cid].fill_q
            draw -= r["draw"]
        free = avail - draw

        def can_start(c):                     # pumps: night first; day only when the nights left are too few
            if c.pump and not night_q[q] and q < latest[c.id]:
                return rem[c.id] > nights_from[q] * c.stream * QH * 3.6
            return True

        def stream_now(c):
            """min(working stream, free) in whole hand streams, if at or above the child's minimum (10.4 step 4);
            pumps run whole units only."""
            if c.stream / c.keep <= free + TOL:
                return c.stream
            s = 0.0 if c.pump else hs_floor(free * c.keep)
            return s if s >= c.minimum - TOL and s > 0 else 0.0

        def go(c, how):
            nonlocal free
            s = stream_now(c)
            if s <= 0 or not can_start(c):
                return False
            q_run = dur[c.id] if s == c.stream else max(1, int(math.ceil(dur[c.id] * c.stream / s - 1e-9)))
            running[c.id] = _open(c, q, q_run, protected, how, s)
            free -= s / c.keep
            return True

        if avail > 0:
            for c in [c_by[x] for x in sorted(interrupted, key=lambda x: c_by[x].pos)]:   # resume first
                if go(c, "queue"):
                    interrupted.discard(c.id)
            for c in [c for c in queue if c.id not in running and c.id not in done and c.id not in interrupted
                      and q >= latest[c.id]]:
                if go(c, "deadline"):
                    started.append(c.id)
            for c in queue:
                if c.id in running or c.id in done or c.id in interrupted or not can_start(c):
                    continue
                if not go(c, "queue"):
                    break                              # a later child never starts before the one ahead of it
                started.append(c.id)
        nxt = min([nq] + [r["end"] for r in running.values()] + [x for x in marks if x > q])
        for x in range(q, nxt):
            use[x] = max(avail - free, 0.0) if avail > 0 else 0.0
        q = nxt
    for cid in list(running):
        _close(running.pop(cid), nq, turns, start, night_q)
    # deliveries to outlets, tertiary sequencing (step 7): a child's pieces in time order; a tertiary fills first,
    # then its stream goes to one farm outlet at a time (carry-over outlets first, then tail first)
    out_turns = []
    pieces = {}
    for t in turns:
        pieces.setdefault(t["unit"], []).append(t)
    for cid, ts in pieces.items():
        c = c_by[cid]
        ts.sort(key=lambda t: t["_start"])
        outs = [(o, k) for o, k in c.outlets if o in due]
        outs = [x for x in outs if x[0] in first] + [x for x in outs if x[0] not in first]
        need_q = {o: max(1, int(round(vol[o] / k / (3.6 * c.stream) / QH))) for o, k in outs}
        run_q = sum(max(0, t["_q"] - min(c.fill_q, t["_q"])) for t in ts)
        if c.kind != "direct_outlet" and outs and sum(need_q.values()) > run_q:
            # the inlet ran shorter than planned: every due outlet gets a shorter turn (8.1), none is left at zero
            # while there is time for one quarter each (carry-over outlets first)
            f = run_q / sum(need_q.values())
            q1 = {o: max(1, int(need_q[o] * f)) for o, _ in outs}
            while sum(q1.values()) > run_q and any(v > 1 for v in q1.values()):
                big = max(q1, key=lambda x: q1[x])
                q1[big] -= 1
            need_q = q1
        i = 0
        for t in ts:
            f_q = min(c.fill_q, t["_q"])
            bal["tertiary_fill_m3"] += t["lps"] * f_q * QH * 3.6
            sq = t["lps"]                                            # the stream this piece ran at
            t["m3"] = round(sq * t["_q"] * QH * 3.6)
            if c.kind == "direct_outlet":
                delivered[c.id] = delivered.get(c.id, 0.0) + sq * (t["_q"] - f_q) * QH * 3.6
                continue
            t0, t_end = t["_start"] + f_q, t["_start"] + t["_q"]
            while i < len(outs) and t0 < t_end:
                o, k = outs[i]
                last = i == len(outs) - 1 and t is ts[-1]
                q_here = (t_end - t0) if last else min(need_q[o], t_end - t0)
                got = sq * k * q_here * QH * 3.6
                delivered[o] = delivered.get(o, 0.0) + got
                out_turns.append({"level": "tertiary", "unit": o, "parent": f"{c.id}-T1", "queue_pos": i,
                                  "open": start + timedelta(hours=t0 * QH), "close": start + timedelta(hours=(t0 + q_here) * QH),
                                  "lps": round(sq * k, 1), "m3": round(got), "mode": "rotating",
                                  "night_h": sum(QH for x in range(t0, t0 + q_here) if night_q[min(x, nq - 1)]),
                                  "flags": ["carry_over"] if o in first else []})
                need_q[o] -= q_here
                t0 += q_here
                if need_q[o] <= 0:
                    i += 1
    # rolling pointer: continue after the last child that started
    if started:
        allc = st.children[off.s1]
        idx = next(i for i, c in enumerate(allc) if c.id == started[-1])
        state.s1_ptr[off.s1] = (idx + 1) % len(allc)
    for t in turns:
        t.pop("_q", None)
        t.pop("_start", None)
    trimmed = []
    for k in range(n):
        peak = max(use[x] + fill[x] for x in range(k * QPS, (k + 1) * QPS))
        trimmed.append(0.0 if peak <= TOL else min(flows[k], max(off.minimum, hs_ceil(peak))))
    budget = [trimmed[x // QPS] for x in range(nq)]
    bal["in_m3"] = sum(trimmed) * SLOT_H * 3.6
    bal["to_children_m3"] = sum(use[q] * QH * 3.6 for q in range(nq))
    bal["fill_m3"] = sum(fill) * QH * 3.6
    bal["spill_m3"] = sum(max(budget[q] - fill[q] - use[q], 0.0) for q in range(nq)) * QH * 3.6
    for q in range(nq):
        day = (start + timedelta(hours=q * QH) - timedelta(hours=CFG["round"]["day_slot_start_h"])).date().isoformat()
        d = bal["per_day"].setdefault(day, {"in_m3": 0.0, "out_m3": 0.0})
        d["in_m3"] += budget[q] * QH * 3.6
        d["out_m3"] += (fill[q] + use[q] + max(budget[q] - fill[q] - use[q], 0.0)) * QH * 3.6
    taken = {}
    for t in turns:
        c = next(x for x in kids if x.id == t["unit"])
        taken[c.id] = taken.get(c.id, 0.0) + c.stream * (t["close"] - t["open"]).total_seconds() / 3600 * 3.6
    return {"turns": turns + out_turns, "delivered": delivered, "balance": bal, "flows": trimmed, "taken": taken}


def _group_priority(ordered, pri):
    """Inside each rotation group (in queue order), carry-over and last-zero children go first."""
    seen, out = [], []
    for c in ordered:
        if c.group not in seen:
            seen.append(c.group)
    for g in seen:
        members = [c for c in ordered if c.group == g]
        out += [c for c in members if c in pri] + [c for c in members if c not in pri]
    return out


def _open(c, q, dur, protected, how, stream=None):
    stream = c.stream if stream is None else stream
    return {"unit": c.id, "parent": c.s1, "start": q, "end": q + dur, "draw": stream / c.keep, "stream": stream,
            "pump": c.pump, "how": how, "pos": c.pos, "group": c.group,
            "protected": any(protected.get(o) for o, _ in c.outlets), "pump_limited": c.pump_limited, "mode": "rotating"}


def _close(r, q, turns, start, night_q):
    fl = []
    if r["how"] == "deadline":
        fl.append("deadline_start")
    if r["pump"] and not all(night_q[r["start"]:q]):
        fl.append("day_pumping")
    if r["pump_limited"]:
        fl.append("pump_limited")
    if r["protected"]:
        fl.append("protected")
    turns.append({"level": "secondary", "unit": r["unit"], "parent": r["parent"], "queue_pos": r["pos"],
                  "group": r["group"], "open": start + timedelta(hours=r["start"] * QH),
                  "close": start + timedelta(hours=q * QH), "lps": r["stream"], "m3": 0,
                  "night_h": 0.0, "mode": r["mode"], "flags": fl, "_q": q - r["start"], "_start": r["start"]})


def _link_next(turns):
    """Who is next at each canal: the turn that opens next on the same parent after this one opens."""
    by = {}
    for t in turns:
        by.setdefault((t["level"], t["parent"]), []).append(t)
    for ts in by.values():
        ts.sort(key=lambda t: (t["open"], t["unit"]))
        for a, b in zip(ts, ts[1:] + [None]):
            a["next"] = b["unit"] if b else None
            a["next_open"] = b["open"] if b else None
        for t in ts:
            if t["level"] != "main" and not t.get("night_h"):
                t["night_h"] = _night_hours(t["open"], t["close"])


def _night_hours(a: datetime, b: datetime) -> float:
    h, t = 0.0, a
    while t < b:
        if is_night(t):
            h += QH
        t += timedelta(hours=QH)
    return h


# ---------------------------------------------------------------- balance with water in transit (R6)
def _balance(st: Static, main: dict, sec_bal: list, start: datetime, n: int) -> dict:
    """Main canal: water released at the head for an offtake leaves the head travel_h earlier and is in transit until
    it arrives (0.7 m/s). Per day: released + in transit at the start = arrived at offtakes + seepage on the way +
    spill at the end of stage II + in transit at the end. Secondary canals: in = to children + fill-and-drain + spill."""
    offs, flow, spill = st.offtakes, main["flow"], main["spill"]
    events = []                                         # (release start, release end, arrive start, arrive end, rate head l/s, delivered l/s)
    for o, f in flow.items():
        for k, q in enumerate(f):
            if q > 0:
                a = start + timedelta(hours=k * SLOT_H)
                b = a + timedelta(hours=SLOT_H)
                tr = timedelta(hours=offs[o].travel_h)
                events.append((a - tr, b - tr, a, b, q * offs[o].cost, q))
    end_h = max(o.travel_h for o in offs.values())
    for k, s in enumerate(spill):
        if s > 0:
            a = start + timedelta(hours=k * SLOT_H)
            b = a + timedelta(hours=SLOT_H)
            tr = timedelta(hours=end_h)
            events.append((a - tr, b - tr, a, b, s, s / st.end_cost))

    def overlap(x0, x1, y0, y1):
        return max(0.0, (min(x1, y1) - max(x0, y0)).total_seconds())

    def stored(t):
        return sum(r * (overlap(r0, r1, datetime.min, t) - overlap(a0, a1, datetime.min, t)) for r0, r1, a0, a1, r, _ in events) / 1000

    days, per_day = sorted({(start + timedelta(hours=k * SLOT_H)).date() for k in range(n)}), {}
    worst = 0.0
    for d in days:
        t0 = datetime(d.year, d.month, d.day, int(CFG["round"]["day_slot_start_h"]))
        t1 = t0 + timedelta(days=1)
        rel = sum(r * overlap(r0, r1, t0, t1) for r0, r1, a0, a1, r, _ in events) / 1000
        arr = sum(r * overlap(a0, a1, t0, t1) for r0, r1, a0, a1, r, _ in events) / 1000
        dlv = sum(dl * overlap(a0, a1, t0, t1) for r0, r1, a0, a1, r, dl in events) / 1000
        s0, s1 = stored(t0), stored(t1)
        res = rel + s0 - (arr + s1)
        worst = max(worst, abs(res) / max(rel + s0, 1.0))
        per_day[d.isoformat()] = {"released_m3": round(rel), "in_transit_start_m3": round(s0), "arrived_m3": round(dlv),
                                  "seepage_m3": round(arr - dlv), "in_transit_end_m3": round(s1)}
    t_end = start + timedelta(hours=n * SLOT_H)
    rel = sum(r * (r1 - r0).total_seconds() for r0, r1, a0, a1, r, _ in events) / 1000
    sec_res = max((abs(b["in_m3"] - b["to_children_m3"] - b["fill_m3"] - b["spill_m3"]) / max(b["in_m3"], 1.0)
                   for b in sec_bal), default=0.0)
    sec_day = max((abs(d["in_m3"] - d["out_m3"]) / max(d["in_m3"], 1.0) for b in sec_bal for d in b["per_day"].values()),
                  default=0.0)
    sec_res = max(sec_res, sec_day)
    head_max = _peak_release(events)
    return {"head_released_m3": round(rel), "in_transit_start_m3": round(stored(start)),
            "in_transit_end_m3": round(stored(t_end)),
            "spill_end_of_stage_II_m3": round(sum(s for s in spill) * SLOT_H * 3.6),
            "secondary_fill_and_drain_m3": round(sum(b["fill_m3"] for b in sec_bal)),
            "secondary_spill_m3": round(sum(b["spill_m3"] for b in sec_bal)),
            "max_day_residual_pct": round(100 * worst, 6), "max_secondary_residual_pct": round(100 * sec_res, 6),
            "head_peak_release_lps": round(head_max, 1), "closes": bool(worst <= A.BALANCE_TOL and sec_res <= A.BALANCE_TOL),
            "per_day": per_day}


def _peak_release(events) -> float:
    pts = sorted({e[0] for e in events} | {e[1] for e in events})
    peak = 0.0
    for t in pts:
        rate = sum(r for r0, r1, a0, a1, r, _ in events if r0 <= t < r1)
        peak = max(peak, rate)
    return peak


# ---------------------------------------------------------------- a run over several rounds
@dataclass
class Season:
    rounds: list = field(default_factory=list)
    state: State = field(default_factory=State)


def run_dekad(st: Static, state: State, period: str, rows: pd.DataFrame, days: int, head_m3: float, head_need_m3: float):
    """Both rounds of a dekad. head_m3: accepted head volume for the dekad (12 m3/s cap applied by the caller)."""
    q_head = head_m3 * 1000 / (days * 86400)
    rho = head_m3 / head_need_m3 if head_need_m3 > 0 else 1.0
    out = []
    for lab, (t0, n) in zip("AB", round_bounds(period, days)):
        out.append(run_round(st, state, period, lab, t0, n, rows, days, q_head, rho))
    return out


def slice_turns(rounds: list, t0: datetime, t1: datetime) -> list:
    """The turns that are open at some time in [t0, t1): a day or week plan is a slice of the full schedule."""
    return [t for r in rounds for t in r.turns if t["open"] < t1 and t["close"] > t0]
