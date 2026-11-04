"""Water delivery planner, step 5: the operational plan for a confirmed supply over a window of up to 10 days (owner
approval 2026-11-02, docs/OWNER_APPROVALS_2026_11_02_EN.md B1, B7; docs/FLOW_WINDOW_PLAN_PROPOSAL_2026_11_02_EN.md
section 3). Fixed code, never a model. Simulation on the synthetic network; not a water order.

Canal operating logic v2 (expert design reference 2026-11-02, docs/CANAL_LOGIC_DESIGN_REFERENCE_2026_11_02_EN.md,
owner-approved; config "operating_logic_v2"). Where it differs from the realism fixes 1-4 it supersedes them: check
gates with reach blocks are on by default below 50 % of the head reach's design flow (the 50 % minimum draw of fix 1
applies only with check_gates=False); the drain loss of fix 2 applies to every close; the fixed 0.7 m/s becomes a
flow-dependent velocity; "not taken" is gone (water is delivered, lost or spilled to the tail escape).

1. Outlets. Every farm outlet (tertiary gate or direct outlet) gathers its parcels that get water on the planning date
   (canal_planner_state: active or expected). A parcel is due when it reaches the stress point within the window; its
   turn is the depletion expected by mid-window, kept within the owner's 500-1,000 m3/ha (50-100 mm) at the field
   inlet. Nothing after the field inlet is modelled. Each outlet draws a whole number of 20 l/s hand streams in
   proportion to its area and turn, 20-50 l/s, at least 2 streams at earthen gates (outlets on tertiary canals).
2. Order of service (E), one combined order: feasibility (the offtake can draw), hard floors (never zero twice, then
   protected crops at stress), the rotation turn (carry-over), then the score S = 40 D + 25 C + 20 T + 15 R, ties by
   outlet name. Below 60 % of due demand, full turns go to fewer outlets: an outlet whose turn does not fit is skipped.
3. Head hydrograph (D, F; engineer's version): the head ramps up by at most 2.4 m3/s per 2 h from the window start; the
   ramps lie inside the window and the plateau is raised so released = confirmed (+-1 %). A ramp-down that would fall
   at the head between 23:00 and 05:00 starts at 05:00 instead; when the 12 m3/s cap binds, the plateau runs longer.
   Both are reported as deviations.
4. Timing in quarter hours of head time, set at each 07:00 / 19:00 change: below 50 % of the head reach's design flow
   the canal runs in reach blocks of whole 12-h slots, tail reach first, one switch per change time (B). Offtakes are
   set tail reach first, then by score, tail to head inside the secondary; the change times also pull further outlets
   in the order of service while head water is left. The 19:00 change sets continuous night streams (A): running
   outlets carry on, tail outlets and turns over 8 h always take water, others by a fixed acceptance share; no gate is
   opened or raised 23:00-05:00 at non-pump offtakes. Water no gate takes fills the canal (5 % of its volume, carried
   to the morning) and only then goes to the tail escape (capacity and a 5 % per block target, flagged above).
   Every close drains 50 % of the secondary's stored water; every reopen refills it (less 30 % if dry under 12 h); a
   gap under 6 h keeps a 20 l/s holding flow instead (C). The canal's last water goes to the last tail turns (D).
5. Water account: released at the head = main-canal seepage (escaped water included) + fill-and-drain + refill + drain
   at closes + holding flow + secondary and tertiary seepage + delivered at field inlets + tail escape; it closes
   exactly (asserted). "Effective" water = water delivered into fields that need it (no field losses modelled).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from wp_core import canal_planner_state as ST
from wp_core import canal_planner_supply as SU

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = json.loads((ROOT / "config/canal_ops_network_active.json").read_text(encoding="utf-8"))
NET = ROOT / ACTIVE["network_dir"]
LOSS = ROOT / ACTIVE["loss_chain_dir"]
ROT = json.loads((ROOT / "config/canal_ops_rotation_v1.json").read_text(encoding="utf-8"))
CFG = SU.CFG
HS = float(ROT["hand_stream_lps"]) / 1000                       # m3/s
SPEED = float(ROT["speed_m_per_s"])                             # design velocity (G1 makes it flow-dependent)
Q_H = 0.25                                                      # quarter hour
BLOCK = int(12 / Q_H)                                           # quarters in a 12-h block
RAMP = 2.4                                                      # m3/s per 2 h at the head (engineer, rotation ramp)
OFFTAKE_MIN = float(ROT["streams"]["main_offtake_min_lps"]) / 1000
TURN_MIN, TURN_MAX = CFG["turn"]["min_m3_per_ha"] / 10, CFG["turn"]["max_m3_per_ha"] / 10   # mm
CLASS_NAMES = {1: "zero last time", 2: "carry-over", 3: "perennial or sensitive field due", 4: "other due"}
TIER_NAMES = {1: "never zero twice", 2: "protected crop at stress", 3: "rotation turn (carry-over)", 4: "by score"}
REAL = CFG["realism"]                                           # expert realism fixes 1-4 (owner 2026-11-02)
OL = CFG["operating_logic_v2"]                                  # canal operating logic v2 (owner 2026-11-02)
NI, ES, RB, CR, RP, OD = (OL[k] for k in ("night", "escape", "reach_blocks", "close_reopen", "ramp", "order"))
ROUTE_FLAG = "supply exceeds what can be routed: reduce the head"


@lru_cache(maxsize=1)
def network():
    n = pd.read_parquet(NET / "nodes.parquet").set_index("node")
    pn = pd.read_parquet(NET / "parcel_node.parquet").set_index("internal_parcel_id")
    lc = pd.read_parquet(LOSS / "nodes.parquet").set_index("node")
    e = pd.read_parquet(NET / "edges.parquet")
    length = e.groupby("canal").length_m.sum()
    return n, pn, lc, length


def _dist_from_head(n: pd.DataFrame, offtake: str) -> float:
    r = n.loc[offtake]
    base = float(n.loc["II", "upstream_m"]) if r.stage == "II" else 0.0
    return base + float(r.chainage_m)


def speed(q: float, q_design: float) -> float:
    """G1: flow-dependent velocity v = 0.7 (Q / Q design)^0.4, at least 0.4 m/s (about 0.5 m/s at 40 % of design)."""
    v = OL["velocity"]
    if q <= 0 or q_design <= 0:
        return v["min_m_s"]
    return max(v["min_m_s"], v["design_m_s"] * min(q / q_design, 1.0) ** v["exponent"])


def groups(s: pd.DataFrame, d: date) -> np.ndarray:
    """perennial / sensitive_field / other_green_field (canal_ops_world rule: stage weight >= 0.8 and not ripening)."""
    sw = s.ltype.map(lambda t: ST.SR.stage_weight(t, d) if t in ST.SR.CFG["stage_weight"] else 0.0)
    sens = (sw >= 0.8) & ~s.ripening.fillna(False).astype(bool)
    return np.where(s.ltype.isin(ST.PERENNIAL), "perennial", np.where(sens, "sensitive_field", "other_green_field"))


def _accepts_night(name: str) -> bool:
    """A: the fixed night acceptance share, chosen by a stable hash of the outlet name (no randomness)."""
    return int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) / 16 ** 8 < NI["acceptance_share"]


def outlets(snap: pd.DataFrame, d: date, hours: float, losses: str = "assumed", history: dict | None = None) -> pd.DataFrame:
    """One row per farm outlet with due land: need (m3 at field inlets), class, tier and score, efficiency, arrival,
    canals, streams; sorted in the combined order of service (E)."""
    n, pn, lc, length = network()
    ls = CFG["losses_sensitivity"]
    k_main = ls[losses]["main"] / ls["assumed"]["main"]
    k_sec = ls[losses]["secondary"] / ls["assumed"]["secondary"]
    s = snap.join(pn[["item_id", "main", "secondary", "unit", "outlet"]], how="inner")
    s = s[s.activity.isin(ST.ACT["water"]) & s.outlet.notna()].copy()
    s["group"] = groups(s, d)
    days = hours / 24
    dep_mid = np.minimum(s.taw_mm, s.depletion_mm + s.etc_mm_day * days / 2)
    s["due"] = s.days_to_stress.notna() & (s.days_to_stress <= math.ceil(days))
    s["turn_mm"] = np.clip(dep_mid, TURN_MIN, TURN_MAX)
    s["need_m3"] = np.where(s.due, s.turn_mm * 10 * s.area_ha, 0.0)
    due = s[s.due]
    hist = history or {}
    zero, carry = set(hist.get("zero_last", ())), set(hist.get("carry_over", ()))
    g = due.groupby("outlet")
    o = pd.DataFrame({"need_m3": g.need_m3.sum(), "due_ha": g.area_ha.sum(), "parcels": g.size(),
                      "urgency_days": g.days_to_stress.min(),
                      "protected_ha": due[due.group != "other_green_field"].groupby("outlet").area_ha.sum(),
                      "main": g.main.first(), "secondary": g.secondary.first(), "unit": g.unit.first()})
    o["protected_ha"] = o.protected_ha.fillna(0.0)
    o["klass"] = np.select([o.index.isin(zero), o.index.isin(carry), o.protected_ha > 0], [1, 2, 3], 4)
    mo = lc.loc[o.main, "loss_from_head_pct"].to_numpy(float) / 100
    so = lc.reindex(o.index).loss_from_offtake_pct.fillna(0).to_numpy(float) / 100
    o["eff_main"] = 1 - mo * k_main
    o["eff_sec"] = 1 - so * k_sec
    o["eff"] = o.eff_main * o.eff_sec
    o["cap_m3s"] = np.maximum(n.reindex(o.index).capacity_lps.fillna(20).to_numpy(float), 20) / 1000
    o["dist_m"] = [_dist_from_head(n, m) for m in o.main]
    own = n.reindex(o.index).path_m.fillna(0).to_numpy(float)
    gate = [n.parent.get(u) if str(n.kind.get(u)) == "tertiary_canal" else None for u in o.unit]
    o["sec_path_m"] = [float(n.path_m.get(g_) or 0) if g_ else p_ for g_, p_ in zip(gate, own)]   # along the secondary
    o["ter_path_m"] = np.where([g_ is not None for g_ in gate], own, 0.0)                       # inside the tertiary
    o["path_m"] = o.sec_path_m + o.ter_path_m
    o["arrival_h"] = (o.dist_m + o.path_m) / SPEED / 3600                  # at design velocity; plan() resets it
    # G2: whole hand streams in proportion to area and turn, 20-50 l/s; at least 2 streams at earthen gates
    st = OL["streams"]
    n_max = max(1, math.floor(st["outlet_max_lps"] / 1000 / HS + 1e-9))
    earthen = np.array([g_ is not None for g_ in gate])
    n_want = np.round(o.need_m3 / (HS * st["turn_target_h"] * 3600))
    n_str = np.clip(n_want, np.where(earthen, st["earthen_min_streams"], 1), n_max)
    o["stream_m3s"] = np.maximum(np.minimum(n_str, np.floor(o.cap_m3s / HS + 1e-9)), 1) * HS
    o["earthen"] = earthen
    o["pump"] = n.reindex(o.main).kind.eq("main_pump").to_numpy()
    o["stage"] = n.reindex(o.main).stage.to_numpy()
    o["section"] = n.reindex(o.main).section.to_numpy()
    # E: tail outlets = the tail third of their secondary by path along the canals
    rank = o.groupby("main").path_m.rank(ascending=False, method="first")
    o["tail"] = rank <= np.maximum(1, np.round(o.groupby("main").path_m.transform("size") * NI["tail_share_of_secondary"]))
    # E, step 4: S = 40 D + 25 C + 20 T + 15 R
    pc = due.assign(base=due.group.map(OD["crop_sensitivity"]), tol=due.group.map(OD["tolerance_days"]))
    pc["c"] = pc.base * (1 - np.clip(pc.days_to_stress / pc.tol, 0, 1)) * pc.area_ha
    owed = o.index.isin(zero) | o.index.isin(carry)
    o["D"] = np.where(owed, 1.0, np.clip(1 - o.urgency_days / OD["slack_days_scale"], 0, 1))
    o["C"] = pc.groupby("outlet").c.sum().reindex(o.index).fillna(0) / o.due_ha
    far = float((o.dist_m + o.path_m).max()) if len(o) else 1.0
    o["T"] = (o.dist_m + o.path_m) / max(far, 1.0)
    o["R"] = o.section.map(OD["section_turn"]).fillna(0.0).astype(float)
    w = OD["weights"]
    o["score"] = (w["D"] * o.D + w["C"] * o.C + w["T"] * o["T"] + w["R"] * o.R).round(6)
    # E, steps 2-3: never zero twice, protected crops at stress, the rotation turn; then the score; ties by name
    floor = (o.protected_ha > 0) & (o.urgency_days <= OD["protected_floor_days_to_stress"])
    o["tier"] = np.select([o.index.isin(zero), floor, o.index.isin(carry)], [1, 2, 3], 4)
    o.attrs["parcels"] = s
    o.index.name = "outlet"
    return o.assign(_s=-o.score, _n=o.index).sort_values(["tier", "_s", "_n"]).drop(columns=["_s", "_n"])


def head_plan(supply: dict) -> dict:
    """Head hydrograph in quarter hours (D, F; the engineer's version): ramps of at most 2.4 m3/s per 2 h inside the
    window, the plateau raised so released = confirmed (+-1 %). A ramp-down that would fall at the head between 23:00
    and 05:00 starts at 05:00 (it may then end after the window); when the 12 m3/s cap binds, the plateau runs longer.
    Both are deviations and are reported."""
    start = datetime.fromisoformat(supply["start"])
    win = int(supply["hours"] / Q_H)
    target = np.zeros(win)
    t = 0
    for seg in supply["segments"]:
        k = int(seg["hours"] / Q_H)
        target[t:t + k] = seg["m3s"]
        t += k
    confirmed = float(target.sum() * Q_H * 3600)
    per_step = RAMP * Q_H / 2

    def night(i):
        h = (start + timedelta(hours=i * Q_H)).hour
        return h >= RP["head_night_from_h"] or h < RP["head_night_to_h"]

    def build(lift, extra):
        lv = np.where(target > 0, np.minimum(target + lift, SU.CAP), 0.0)
        lv = np.concatenate([lv, np.full(extra, lv[-1])])                 # cap-bound: the plateau runs longer
        end = win + extra
        r = int(math.ceil(lv[-1] / per_step - 1e-9))
        down = end - r
        if any(night(i) for i in range(max(down, 0), end)):              # D: no falling water at night at the head
            five = [i for i in range(end + 1) if (start + timedelta(hours=i * Q_H)).hour == RP["down_earliest_h"]
                    and (start + timedelta(hours=i * Q_H)).minute == 0]
            down = five[-1] if five else down
        q, cur, i = [], 0.0, 0
        while i < down or cur > 1e-9:
            goal = lv[i] if i < down else 0.0
            cur = min(goal, cur + per_step) if goal >= cur else max(goal, cur - per_step)
            q.append(max(cur, 0.0))
            i += 1
        return np.array(q), down

    def vol(x):
        return float(x.sum() * Q_H * 3600)

    extra = 0
    if confirmed > 0:
        while vol(build(SU.CAP, extra)[0]) < confirmed * (1 - RP["volume_tolerance"] / 10) and extra < 4 * win:
            extra += 1                                                     # cap binds: extend the plateau
        lo, hi = -float(target[target > 0].min()), SU.CAP
        for _ in range(60):                                                # raise the plateau until released = confirmed
            mid = (lo + hi) / 2
            if vol(build(mid, extra)[0]) < confirmed:
                lo = mid
            else:
                hi = mid
        q, down = build(hi, extra)
    else:
        q, down = np.zeros(win), win
    released = vol(q)
    peak = float(q.max()) if len(q) else 0.0
    up_end = int(np.argmax(q >= peak - 1e-9)) if peak > 0 else 0
    after = max(0, len(q) - win - extra)
    return {"q": q, "start": start, "window_steps": win, "confirmed_m3": confirmed, "released_m3": released,
            "plateau_m3s": round(peak, 3), "ramp_up_m3": vol(q[:up_end]), "ramp_down_m3": vol(q[down:]),
            "extension_h": extra * Q_H, "ramp_down_after_window_h": after * Q_H,
            "within_tolerance": abs(released - confirmed) <= RP["volume_tolerance"] * confirmed + 1.0}


def head_flow(supply: dict) -> tuple[np.ndarray, datetime]:
    """Head hydrograph in quarter hours (head_plan) and its start."""
    h = head_plan(supply)
    return h["q"], h["start"]


@dataclass
class Plan:
    plan_id: str
    supply: dict
    planning: str
    losses: str
    outlets: pd.DataFrame
    offtakes: pd.DataFrame
    account: dict
    summary: dict


@lru_cache(maxsize=1)
def reach_design():
    """Design flow of the main canal just above each offtake (m3/s): the capacities of the offtakes at or below it,
    capped at the main canal's 12 m3/s. Used by the minimum-flow-to-draw rule (realism fix 1)."""
    n = network()[0]
    off = n[n.kind.isin(["main_gate", "main_pump"])]
    dist = pd.Series({k: _dist_from_head(n, k) for k in off.index})
    cap = off.capacity_lps.astype(float) / 1000
    return {k: min(SU.CAP, float(cap[dist >= dist[k]].sum())) for k in off.index}, dist.to_dict()


@lru_cache(maxsize=1)
def section_design():
    """B: a checked reach's own design flow (m3/s): the capacities of its offtakes (the reach below is closed)."""
    n = network()[0]
    off = n[n.kind.isin(["main_gate", "main_pump"])]
    return {s: min(SU.CAP, float(g.capacity_lps.astype(float).sum() / 1000)) for s, g in off.groupby("section")}


@lru_cache(maxsize=1)
def escape_efficiency():
    """Main-canal efficiency down to the tail escape (G5: escaped water also seeps), from the loss chain."""
    n, _, lc, _ = network()
    esc = lc[lc.kind == "tail_escape"]
    main_esc = esc[[str(n.parent.get(k)) in ("I", "II") for k in esc.index]] if len(esc) else esc
    pct = float((main_esc if len(main_esc) else lc[lc.kind == "main_pump"]).loss_from_head_pct.max())
    return 1 - pct / 100


@lru_cache(maxsize=1)
def main_volume_m3():
    """Main canal volume at design flow and velocity (for the 5 % night storage, A)."""
    _, dist = reach_design()
    return SU.CAP / OL["velocity"]["design_m_s"] * max(dist.values())


def _night(t: datetime) -> bool:
    return t.hour >= REAL["no_offtake_changes_from_h"] or t.hour < REAL["no_offtake_changes_to_h"]


def _streams_up(q: float) -> float:
    return math.ceil(max(q, 0.0) / HS - 1e-9) * HS


def _streams_down(q: float) -> float:
    return math.floor(max(q, 0.0) / HS + 1e-9) * HS


def _feasible_at(k: str, q_pass: float, mode: str, sec: dict, design: dict) -> bool:
    """1. Feasibility: the main canal passing the offtake can feed it. mode "blocks": reach blocks, >= 25 % of the
    checked reach's own design flow (B); "checked": a check gate below the offtake, >= 25 % of the reach's design flow;
    "plain": no check gates, >= 50 % of the reach's design flow (realism fix 1)."""
    if mode == "blocks":
        return q_pass >= RB["min_draw_share_checked_reach"] * sec[k] - 1e-9
    share = REAL["min_draw_share_with_check_gate" if mode == "checked" else "min_draw_share_of_reach_design"]
    return q_pass >= share * design[k] - 1e-9


def _greedy(o, budget, feasible, concentrate):
    """Who (E): whole outlets in the order of service while the head budget lasts (estimates; the timing settles the
    truth). Below 60 % of due demand an outlet whose full turn does not fit in the window is skipped, not a stop."""
    chosen, skipped, wet_sec, wet_ter = [], set(), set(), set()
    for name, r in o.iterrows():
        if r.main not in feasible or r.give_m3 <= 0:
            continue
        if concentrate and r.give_m3 < r.need_m3 - 1.0:
            skipped.add(name)
            continue
        fill = 0.0
        if r.secondary not in wet_sec:
            fill += r.stream_m3s / r.eff_sec * r.sec_path_m / SPEED / r.eff_main
        if r.unit != name and r.unit not in wet_ter:
            fill += r.stream_m3s * r.ter_path_m / SPEED / r.eff_sec / r.eff_main
        cost = r.give_m3 / r.eff + fill
        if cost > budget:
            continue
        budget -= cost
        chosen.append(name)
        wet_sec.add(r.secondary)
        wet_ter.add(r.unit)
    return chosen, skipped


def _reach_plan(o, chosen, nb):
    """B: whole 12-h blocks per reach, tail reach first, in proportion to the chosen outlets' head water, at least one
    block each; with fewer blocks than reaches the head reach takes its share at the end, as the canal drains down."""
    dem = {}
    for x in chosen:
        dem[o.at[x, "section"]] = dem.get(o.at[x, "section"], 0.0) + o.at[x, "give_m3"] / o.at[x, "eff"]
    secs = [s for s in RB["order"] if dem.get(s, 0) > 0] or list(RB["order"])
    if nb <= len(secs):
        return [{secs[b]} for b in range(nb - 1)] + [set(secs[nb - 1:])]
    tot = sum(dem.get(s, 0) for s in secs) or 1.0
    share = {s: (nb - len(secs)) * dem.get(s, 0) / tot for s in secs}
    n_b = {s: 1 + int(share[s]) for s in secs}
    for s in sorted(secs, key=lambda s: (-(share[s] - int(share[s])), RB["order"].index(s)))[:nb - sum(n_b.values())]:
        n_b[s] += 1
    return [{s} for s in secs for _ in range(n_b[s])]


def _timing(o, chosen, q_head, start, mode, v_main, holds=None, skipped=frozenset(), concentrate=False):
    """Quarter-hour simulation in head time (rules A-D, G). Volumes are at the offtake unless named; storage and escape
    at the head. Gate settings change only at the 07:00 / 19:00 changes; inside a block a secondary moves on to its
    next outlet (tail to head) when one finishes, and a gate is lowered or closed when its secondary runs out of work.
    At a change each open secondary is given at least the block's work (C: secondaries run >= 12 h); with concentrate
    an outlet is pulled only while its full turn fits the head water still to come (E5).
    holds: {offtake: quarters} where a gap under 6 h was seen; there the gate keeps a 20 l/s holding flow (C)."""
    blocks = mode == "blocks"
    n = network()[0]
    design, dist_all = reach_design()
    sdes = section_design()
    holds = holds or {}
    steps = len(q_head)
    dt = Q_H * 3600
    cols = ["main", "section", "eff_sec", "eff_main", "stream_m3s", "unit", "give_m3", "path_m", "sec_path_m",
            "ter_path_m", "night_ok", "tail", "large", "score", "pump"]
    R = {c: o[c].to_dict() for c in cols}
    offs = sorted(o.main.unique(), key=lambda k: (-dist_all[k], k))
    sec_of = {k: str(n.at[k, "section"]) for k in offs}
    sec_d = {k: sdes[sec_of[k]] for k in offs}
    eff_main = o.groupby("main").eff_main.first().to_dict()
    cap = {k: max(HS, _streams_down(float(n.at[k, "capacity_lps"]) / 1000)) for k in offs}       # whole streams
    pump = {k: str(n.at[k, "kind"]) == "main_pump" for k in offs}
    arr_h = {k: round(dist_all[k] / v_main / 3600 / Q_H) * Q_H for k in offs}     # on the quarter hour, as on the sheet
    sec_len = o.groupby("main").sec_path_m.max().to_dict()
    on_k = {k: sorted(o.index[o.main == k], key=lambda x: (-R["path_m"][x], x)) for k in offs}    # tail to head
    all_x = [x for k in offs for x in on_k[k]]
    chosen = list(chosen)
    inn = set(chosen)
    left = {x: R["give_m3"][x] / R["eff_sec"][x] for x in chosen}
    got, got_night = {}, {}
    first, last = {}, {}
    ter_fill = {u: float(g.stream_m3s.max() / g.eff_sec.min() * g.ter_path_m.max() / SPEED)
                for u, g in o[o.unit != o.index].groupby("unit")}
    draw = {k: np.zeros(steps) for k in offs}
    hold = {k: np.zeros(steps) for k in offs}
    fill_left = {k: 0.0 for k in offs}                 # seconds of filling still to do in the secondary
    fill_kind = {k: "fill_sec" for k in offs}          # first fill or refill
    fill_s_at = {}                                     # fill time at the running flow
    state = {k: "dry" for k in offs}                   # dry / open / closed
    closed_at = {}
    ev = {k: {"opens": 0, "closes": 0, "reopens": 0, "drain_m3": 0.0, "refill_m3": 0.0} for k in offs}
    acc = {"fill_sec": 0.0, "refill": 0.0, "fill_ter": 0.0, "drain": 0.0, "holding": 0.0, "esc_sec": 0.0,
           "esc_head": 0.0, "end_head": 0.0, "end_off": 0.0, "esc_end": 0.0}
    esc_q = np.zeros(steps)                            # escape at the escape (m3 per quarter, main + secondary)
    esc_main_q = np.zeros(steps)
    store = 0.0
    store_max = NI["storage_share_of_main_volume"] * main_volume_m3()
    blocked = {k: 0 for k in offs}
    held = {k: 0 for k in offs}
    nb = int(math.ceil(steps / BLOCK))
    reach = _reach_plan(o, chosen, nb) if blocks else [set(RB["order"])] * nb
    setting = {k: 0.0 for k in offs}
    order = list(offs)
    eligible = {}
    night_blk = False
    rem = {}                                           # head water still to come, less the planned turns (m3)

    def absorb(xs):
        return sum(R["stream_m3s"][x] / R["eff_sec"][x] for x in xs)

    def ok(x):
        return (R["section"][x] in reach[b]) and (not night_blk or R["night_ok"][x] or x in running)

    def pull(x):
        """Add an outlet to the plan if it may take water now; with concentrate only while its full turn fits."""
        if x in inn or x in skipped or not ok(x):
            return False
        cost = R["give_m3"][x] / R["eff_sec"][x] / R["eff_main"][x]
        key = R["section"][x] if blocks else "*"
        if concentrate and cost > rem.get(key, 0.0):
            return False
        rem[key] = rem.get(key, 0.0) - cost
        inn.add(x)
        chosen.append(x)
        left[x] = R["give_m3"][x] / R["eff_sec"][x]
        eligible.setdefault(R["main"][x], []).append(x)
        return True

    def todo(k):
        xs = [x for x in eligible.get(k, ()) if left.get(x, 0) > 1e-6]
        if not xs:                                     # A4 / C: the next outlet in the same secondary, tail to head
            xs = next(([x] for x in on_k[k] if pull(x)), [])
        return xs

    def close(k, i, q_prev):
        """C: a close drains the secondary; the set share of its stored water is lost, taken from the outlets on it
        that took water last (they need it again)."""
        lost = CR["drain_share"] * q_prev * fill_s_at.get(k, 0.0)
        for x in sorted([x for x in on_k[k] if got.get(x, 0) > 0], key=lambda x: (-last[x], x)):
            if lost <= 1e-9:
                break
            take = min(lost, got[x])
            got[x] -= take
            left[x] = left.get(x, 0.0) + take
            lost -= take
            ev[k]["drain_m3"] += take
            acc["drain"] += take
        state[k] = "closed"
        closed_at[k] = i
        ev[k]["closes"] += 1

    running = set()
    for i in range(steps):
        t = start + timedelta(hours=i * Q_H)
        b = i // BLOCK
        if i % BLOCK == 0:                             # a change time: 07:00 or 19:00 at the head
            night_blk = t.hour == 19                   # A: the 19:00 change sets the night streams
            running = {x for x in chosen if left.get(x, 0) > 1e-6 and last.get(x) == i - 1}
            eligible = {k: [] for k in offs}
            for x in all_x:
                if x in inn and left.get(x, 0) > 1e-6 and ok(x):
                    eligible[R["main"][x]].append(x)
            if night_blk:                              # A: running outlets, then tail / large turns, then the rest
                for k in offs:
                    eligible[k].sort(key=lambda x: (x not in running, not (R["tail"][x] or R["large"][x]),
                                                    -R["path_m"][x], x))
            s_k = {k: max([R["score"][x] for x in eligible[k]] or [0.0]) for k in offs}
            r_rank = {k: RB["order"].index(sec_of[k]) if sec_of[k] in RB["order"] else 9 for k in offs}
            if night_blk:                              # A: running, tail / large turns, pumps, the rest
                order = sorted(offs, key=lambda k: (not any(x in running for x in eligible[k]),
                                                    not any(R["tail"][x] or R["large"][x] for x in eligible[k]),
                                                    not pump[k], r_rank[k], -s_k[k], k))
            else:                                      # E timing: reach tail first, then the secondary by S
                order = sorted(offs, key=lambda k: (r_rank[k], -s_k[k], k))
            peak = float(q_head[i:i + BLOCK].max()) + (store / (BLOCK * dt) if not night_blk else 0.0)
            # head water still to come for each reach (its own blocks only, B), less the turns already planned there
            rem.clear()
            for s in (RB["order"] if blocks else ["*"]):
                mine = [j for j in range(i, steps) if not blocks or s in reach[j // BLOCK]]
                rem[s] = float(q_head[mine].sum()) * dt + store - sum(
                    v / R["eff_main"][x] for x, v in left.items() if v > 0 and (not blocks or R["section"][x] == s))
            used = 0.0
            prev_set = dict(setting)
            setting = {k: 0.0 for k in offs}

            def gate(k):
                xs = [x for x in eligible[k] if left.get(x, 0) > 1e-6]
                if not xs:
                    return 0.0
                # a change that reaches the offtake 23:00-05:00 waits there until 05:00 (fix 3, per quarter below)
                return min(cap[k], _streams_up(absorb(xs)))

            for k in order:
                if not _feasible_at(k, peak * eff_main[k], mode, sec_d, design):
                    continue
                q = min(gate(k), _streams_down((peak - used) * eff_main[k]))
                if q < min(OFFTAKE_MIN, cap[k]) - 1e-9 and prev_set.get(k, 0) <= 0:
                    q = 0.0
                setting[k] = q
                used += q / eff_main[k]
            # E: pull further outlets in the order of service while the block's head water is left
            if used < peak - HS:
                for x in o.index:
                    if used >= peak - HS:
                        break
                    k = R["main"][x]
                    if x in inn or not _feasible_at(k, peak * eff_main[k], mode, sec_d, design):
                        continue
                    if not pull(x):
                        continue
                    q = min(gate(k), setting[k] + _streams_down((peak - used) * eff_main[k]))
                    if q <= setting[k] or (setting[k] <= 0 and q < min(OFFTAKE_MIN, cap[k]) - 1e-9):
                        continue
                    used += (q - setting[k]) / eff_main[k]
                    setting[k] = q
            # C: an open secondary gets at least the block's work at its setting (rotation inside the secondary)
            for k in offs:
                if setting[k] <= 0:
                    continue
                work = sum(left[x] for x in eligible[k] if left.get(x, 0) > 0)
                for x in on_k[k]:
                    if work >= setting[k] * BLOCK * dt:
                        break
                    if pull(x):
                        work += left[x]
            for k in offs:                              # within a secondary: tail to head (night order kept)
                if not night_blk:
                    eligible[k].sort(key=lambda x: (-R["path_m"][x], x))
        free = q_head[i]                                # m3/s at the head
        if not night_blk and store > 0:                # canal storage (m3) goes back to the morning's turns
            free += store / dt
            store = 0.0
        for k in order:
            prev = draw[k][i - 1] + hold[k][i - 1] if i else 0.0
            q = 0.0
            if setting[k] > 0:
                # B: in a reach block the check gate holds the reach's level, so its inflow counts
                up = sum(draw[j][i - 1] / eff_main[j] for j in offs if dist_all[j] < dist_all[k]) \
                    if i and not blocks else 0.0
                passing = max(q_head[i] - up, 0.0) * eff_main[k]
                if not _feasible_at(k, passing, mode, sec_d, design):
                    blocked[k] += 1
                else:
                    xs = todo(k)
                    if xs:
                        want = min(setting[k], cap[k], _streams_up(absorb(xs)))
                        if not pump[k] and _night(t + timedelta(hours=arr_h[k])) and want > prev:
                            want = prev                 # fix 3: never opened or raised at night
                            held[k] += 1
                        q = min(want, _streams_down(free * eff_main[k]))
                        if prev <= 0 and q < min(OFFTAKE_MIN, cap[k], want) - 1e-9:
                            q = 0.0
            if q <= 0:
                if state[k] == "open":
                    if i in holds.get(k, ()) and free * eff_main[k] >= HS - 1e-9:   # C: a short gap is held
                        hold[k][i] = HS
                        free -= HS / eff_main[k]
                        acc["holding"] += HS * dt
                        continue
                    close(k, i, draw[k][i - 1] if i else 0.0)
                continue
            if state[k] != "open":                      # a dry secondary fills (G1: at the flow's velocity)
                fill_s_at[k] = sec_len.get(k, 0.0) / speed(q, cap[k])
                fill_left[k] = fill_s_at[k]
                fill_kind[k] = "fill_sec"
                ev[k]["opens"] += 1
            if state[k] == "closed":                    # C: a reopen refills, less 30 % if dry under 12 h
                if (i - closed_at[k]) * Q_H < CR["refill_discount_dry_under_h"]:
                    fill_left[k] *= 1 - CR["refill_discount"]
                fill_kind[k] = "refill"
                ev[k]["reopens"] += 1
            state[k] = "open"
            draw[k][i] = q
            free -= q / eff_main[k]
            avail = q * dt                              # m3 at the offtake this quarter
            if fill_left[k] > 0:                        # the secondary fills first
                share = min(1.0, fill_left[k] / dt)
                f = avail * share
                acc[fill_kind[k]] += f
                if fill_kind[k] == "refill":
                    ev[k]["refill_m3"] += f
                fill_left[k] -= share * dt
                avail -= f
            for x in todo(k):                           # outlets in order, one stream each
                if avail <= 1e-9:
                    break
                take = min(avail, R["stream_m3s"][x] / R["eff_sec"][x] * dt)
                u = R["unit"][x]
                if ter_fill.get(u, 0) > 0:              # a dry tertiary fills first
                    f = min(take, ter_fill[u])
                    acc["fill_ter"] += f
                    ter_fill[u] -= f
                    avail -= f
                    take -= f
                v = min(take, left[x])
                if v <= 0:
                    continue
                avail -= v
                left[x] -= v
                got[x] = got.get(x, 0.0) + v
                if night_blk:
                    got_night[x] = got_night.get(x, 0.0) + v
                first.setdefault(x, i)
                last[x] = i
            acc["esc_sec"] += avail                     # what the secondary could not use passes to its escape
            esc_q[i] += avail
        if free > 1e-12:                                # A5-6: canal storage for the morning, then the tail escape
            put = min(free * dt, store_max - store)
            store += put
            free -= put / dt
            acc["esc_head"] += free * dt
            esc_main_q[i] = free * dt * escape_efficiency()
            esc_q[i] += esc_main_q[i]
    for k in offs:                                      # the canal's last closes
        if state[k] == "open":
            close(k, steps, draw[k][-1] + hold[k][-1])
    # D: the water left in the canal goes to the last tail turns (delivered), the rest to the escape
    if store > 1e-9:
        tail_x = sorted([x for x in chosen if left.get(x, 0) > 1e-6 and draw[R["main"][x]][-BLOCK:].any()],
                        key=lambda x: (-dist_all[R["main"][x]] - R["path_m"][x], x))
        for x in tail_x:
            if store <= 1e-9:
                break
            em = eff_main[R["main"][x]]
            v = min(left[x], store * em)
            store -= v / em
            left[x] -= v
            got[x] = got.get(x, 0.0) + v
            first.setdefault(x, steps - 1)
            last[x] = steps - 1
            acc["end_head"] += v / em
            acc["end_off"] += v
        if store > 1e-12:
            acc["esc_head"] += store
            esc_q[-1] += store * escape_efficiency()   # drains out as the canal empties, not a spill peak
            acc["esc_end"] = store * escape_efficiency()
            store = 0.0
    info = {"blocked": blocked, "held": held, "arr_h": arr_h, "pump": pump, "design": {k: design[k] for k in offs},
            "events": ev, "acc": acc, "esc_q": esc_q, "esc_main_q": esc_main_q, "reach": reach, "chosen": chosen,
            "got_night": got_night, "cap": cap, "sec_len": sec_len}
    return draw, hold, first, last, got, info


def _short_gaps(draw: dict) -> dict:
    """C: quarters of every gap under 6 h between two runs of an offtake (where a holding flow replaces a close)."""
    lim = CR["holding_gap_under_h"] / Q_H
    out = {}
    for k, v in draw.items():
        on = np.flatnonzero(v > 0)
        if len(on) < 2:
            continue
        gaps = np.flatnonzero(np.diff(on) > 1)
        for g in gaps:
            a, b = on[g] + 1, on[g + 1]
            if b - a < lim:
                out.setdefault(k, set()).update(range(a, b))
    return out


def plan(supply: dict, losses: str = "assumed", band: str = "normal", history: dict | None = None,
         snap: pd.DataFrame | None = None, check_gates: bool | None = None) -> Plan:
    """The operational plan for a confirmed supply (canal_planner_supply.confirmed). check_gates: None (default) =
    check gates hold the level (>= 25 % of reach design flow to draw) and reach blocks run below 50 % of the head
    reach's design flow (B); True = check gates with reach blocks; False = no check gates (realism fix 1, 50 %)."""
    if supply["horizon"] != "operational":
        raise SU.SupplyRefused("this step plans windows of up to 10 days; longer windows are planned per dekad")
    n = network()[0]
    d = datetime.fromisoformat(supply["planning"]).date()
    hours = float(supply["hours"])
    snap = snap if snap is not None else ST.snapshot(d, band, int(math.ceil(hours / 24)) + 7)
    o = outlets(snap, d, hours, losses, history)
    hp = head_plan(supply)
    q_head, start = hp["q"], hp["start"]
    released = float(q_head.sum() * Q_H * 3600)
    design, dist_all = reach_design()
    sdes = section_design()
    peak = float(q_head.max()) if len(q_head) else 0.0
    head_off = min(dist_all, key=dist_all.get)
    low = peak < RB["on_below_share_of_design"] * design[head_off]
    mode = "plain" if check_gates is False else "blocks" if check_gates or low else "checked"
    blocks = mode == "blocks"
    v_main = speed(peak, SU.CAP)
    o["give_m3"] = np.minimum(o.need_m3, o.stream_m3s * hours * 3600)
    o["turn_h"] = o.give_m3 / o.stream_m3s / 3600
    o["large"] = o.turn_h > NI["large_turn_h"]
    o["night_ok"] = o["tail"] | o.large | np.array([_accepts_night(x) for x in o.index], dtype=bool)
    sec_of = n.section.to_dict()
    feasible = {k for k in o.main.unique()
                if _feasible_at(k, peak * float(o.eff_main[o.main == k].iloc[0]), mode,
                                {k: sdes[sec_of[k]]}, design)}
    concentrate = released < OD["concentrate_below_supply_share"] * float((o.need_m3 / o.eff).sum())
    chosen, skipped = _greedy(o, released, feasible, concentrate)
    draw, hold, first, last, got, info = _timing(o, chosen, q_head, start, mode, v_main, None, skipped, concentrate)
    holds = _short_gaps(draw)                            # second pass: short gaps keep a holding flow (C)
    if holds:
        draw, hold, first, last, got, info = _timing(o, chosen, q_head, start, mode, v_main, holds, skipped,
                                                     concentrate)
    acc, ev = info["acc"], info["events"]
    used = {k: v + hold[k] for k, v in draw.items() if (v + hold[k]).any()}
    peak_k = {k: float(v.max()) for k, v in used.items()}
    v_sec = {k: speed(peak_k.get(k, info["cap"][k]), info["cap"][k]) for k in draw}
    o["arrival_h"] = o.dist_m / v_main / 3600 + o.path_m / o.main.map(v_sec).fillna(SPEED) / 3600
    o["delivered_m3"] = pd.Series({k: got[k] * o.at[k, "eff_sec"] for k in got}, dtype=float
                                  ).reindex(o.index).fillna(0.0)
    o["night_m3"] = pd.Series({k: v * o.at[k, "eff_sec"] for k, v in info["got_night"].items()}, dtype=float
                              ).reindex(o.index).fillna(0.0).clip(upper=o.delivered_m3)
    o["status"] = np.where(o.delivered_m3 >= o.need_m3 - 1.0, "served",
                           np.where(o.delivered_m3 > 0, "partly", "not reached"))
    # fix 2: a change reaches an outlet over a band of +-30 % of its travel time; the turn counts from full flow
    band_ = REAL["arrival_band_share"]
    o["water_from"] = [start + timedelta(hours=first[k] * Q_H + o.at[k, "arrival_h"] * (1 - band_)) if k in first
                       else pd.NaT for k in o.index]
    o["open"] = [start + timedelta(hours=first[k] * Q_H + o.at[k, "arrival_h"] * (1 + band_)) if k in first else pd.NaT
                 for k in o.index]
    o["close"] = [start + timedelta(hours=(last[k] + 1) * Q_H + o.at[k, "arrival_h"] * (1 + band_)) if k in last
                  else pd.NaT for k in o.index]
    o["first_q"] = pd.Series(first, dtype=float).reindex(o.index)
    o["last_q"] = pd.Series(last, dtype=float).reindex(o.index)
    o["lps"] = np.where(o.delivered_m3 > 0, (o.stream_m3s * 1000).round(), np.nan)
    eff_main = o.groupby("main").eff_main.first()
    offt = pd.DataFrame({
        "at_offtake_m3": {k: float(v.sum() * Q_H * 3600) for k, v in used.items()},
        "head_m3": {k: float((v / eff_main[k]).sum() * Q_H * 3600) for k, v in used.items()},
        "peak_lps": {k: round(float(v.max()) * 1000) for k, v in used.items()},
        # same quarter-hour arrival as the gate changes (fix 3), so the sheet shows one time per gate action
        "open": {k: start + timedelta(hours=float(np.argmax(v > 0)) * Q_H + info["arr_h"][k]) for k, v in used.items()},
        "close": {k: start + timedelta(hours=(len(v) - float(np.argmax(v[::-1] > 0))) * Q_H + info["arr_h"][k])
                  for k, v in used.items()}})
    offt["pump"] = n.reindex(offt.index).kind.eq("main_pump").to_numpy()
    offt["stage"] = n.reindex(offt.index).stage.to_numpy()
    offt["section"] = n.reindex(offt.index).section.to_numpy()
    offt["arr_h"] = [info["arr_h"][k] for k in offt.index]
    offt["open_h"] = [float((used[k] > 0).sum() * Q_H) for k in offt.index]
    for c in ("closes", "reopens", "drain_m3", "refill_m3"):
        offt[c] = [round(ev[k][c]) if c.endswith("m3") else ev[k][c] for k in offt.index]
    offt["holding_q"] = [int((hold[k] > 0).sum()) for k in offt.index]

    # fix 3: each gate change at an offtake is timed to the arrival of the head change, on a quarter hour
    def changes(k, v):
        out, prev = [], 0.0
        for i, q in enumerate(v):
            if abs(q - prev) > 1e-9:
                head_t = start + timedelta(hours=i * Q_H)
                here = head_t + timedelta(hours=round(info["arr_h"][k] / Q_H) * Q_H)
                out.append({"head": f"{head_t:%Y-%m-%d %H:%M}", "here": f"{here:%Y-%m-%d %H:%M}",
                            "lps": round(q * 1000)})
            prev = q
        if prev > 0:
            here = start + timedelta(hours=len(v) * Q_H + info["arr_h"][k])
            out.append({"head": f"{start + timedelta(hours=len(v) * Q_H):%Y-%m-%d %H:%M}", "here": f"{here:%Y-%m-%d %H:%M}",
                        "lps": 0})
        return out
    offt["changes"] = [changes(k, used[k]) for k in offt.index]
    offt["night_holds_q"] = [info["held"].get(k, 0) for k in offt.index]
    infeasible = sorted(k for k in o.main.unique() if k not in feasible)
    cannot = sorted(set(infeasible) | {k for k, b in info["blocked"].items() if b and k not in used})
    o["low_canal"] = o.main.isin(cannot) & (o.delivered_m3 <= 0)
    o.loc[o.low_canal, "status"] = "not reached: canal level"
    # E: every outlet left without its full turn is carry-over (D = 1 next round), with a stated reason
    o["carry_over"] = o.status != "served"
    o["reason"] = np.select(
        [o.status == "served", o.low_canal, o.index.isin(skipped) & (o.delivered_m3 <= 0), o.status == "partly",
         ~o.night_ok & (o.delivered_m3 <= 0)],
        ["", "canal level: the offtake cannot draw at this flow",
         "full turn does not fit: supply under 60 % of due demand, fewer outlets get full turns",
         "turn not finished in the window", "lower in the order of service; outlet does not take night turns"],
        "lower in the order of service; the window's water went to outlets ahead of it")

    # 5. water account (closes exactly, G7)
    delivered = float(o.delivered_m3.sum())
    drawn_off = sum(float(v.sum() * Q_H * 3600) for v in used.values())
    drawn_head = sum(float((v / eff_main[k]).sum() * Q_H * 3600) for k, v in used.items())
    esc_out = float(info["esc_q"].sum())
    esc_main_out = float(info["esc_main_q"].sum()) + acc["esc_end"]
    main_seep = (drawn_head - drawn_off) + (acc["esc_head"] - esc_main_out) + (acc["end_head"] - acc["end_off"])
    sec_seep = float(sum(v * (1 - o.at[k, "eff_sec"]) for k, v in got.items()))
    terms = {"main_canal_seepage_m3": main_seep, "fill_and_drain_m3": acc["fill_sec"] + acc["fill_ter"],
             "refill_m3": acc["refill"], "secondary_tertiary_seepage_m3": sec_seep,
             "turn_end_drain_m3": acc["drain"], "holding_flow_m3": acc["holding"],
             "delivered_field_inlets_m3": delivered, "escape_m3": esc_out}
    gap = released - sum(terms.values())
    if abs(gap) > 1e-6 * max(released, 1.0) + 1e-3:                  # audit M3, G7: no unexplained water
        raise AssertionError(f"water account does not close: released {released} vs {terms}")
    account = {"released_head_m3": round(released), **{k: round(v) for k, v in terms.items()}}
    account["closes"] = abs(sum(v for k, v in account.items() if k != "released_head_m3")
                            - account["released_head_m3"]) <= len(terms)
    took = o.delivered_m3 > 0
    end = start + timedelta(hours=hours)
    late = sum(o.at[k, "delivered_m3"] * max(0.0, (o.at[k, "close"] - max(o.at[k, "open"], end)).total_seconds())
               / max((o.at[k, "close"] - o.at[k, "open"]).total_seconds(), 1.0) for k in o.index[took])
    p = o.attrs["parcels"]
    got_p = p[p.outlet.isin(o.index[took]) & p.due]
    due = p[p.due]
    nr = o[~took]
    groups_ = ("perennial", "sensitive_field", "other_green_field")
    # A, G4: the tail escape per 12-h block against its 5 % target and the escape capacity
    by_block = []
    for b0 in range(0, len(q_head), BLOCK):
        h_ = float(q_head[b0:b0 + BLOCK].sum() * Q_H * 3600)
        e_ = float(info["esc_q"][b0:b0 + BLOCK].sum())
        by_block.append({"start": f"{start + timedelta(hours=b0 * Q_H):%Y-%m-%d %H:%M}", "head_m3": round(h_),
                         "escape_m3": round(e_), "escape_pct": round(100 * e_ / h_, 1) if h_ else 0.0,
                         "over_target": bool(h_ and e_ > ES["target_share_per_block"] * h_ + 1.0),
                         "sections": sorted(info["reach"][b0 // BLOCK])})
    esc_peak = float(info["esc_main_q"].max() / (Q_H * 3600)) if len(q_head) else 0.0
    over_cap = esc_peak > ES["capacity_m3s"] + 1e-9
    deviations = []
    if hp["extension_h"]:
        deviations.append(f"12 m3/s cap binds: the plateau runs {hp['extension_h']:g} h longer than the window")
    if hp["ramp_down_after_window_h"]:
        deviations.append(f"ramp-down starts at 05:00 so falling water is not released at night: it ends "
                          f"{hp['ramp_down_after_window_h']:g} h after the window")
    short = sorted(k for k in used if offt.at[k, "open_h"] < CR["secondary_min_run_h"])
    summary = {
        "outlets_due": int(len(o)), "outlets_served": int((o.status == "served").sum()),
        "outlets_partly": int((o.status == "partly").sum()), "outlets_not_reached": int((~took).sum()),
        "due_ha": round(float(due.area_ha.sum()), 2), "ha_with_a_turn": round(float(got_p.area_ha.sum()), 2),
        "ha_with_a_turn_by_group": {g: round(float(got_p[got_p.group == g].area_ha.sum()), 2) for g in groups_},
        "due_ha_by_group": {g: round(float(due[due.group == g].area_ha.sum()), 2) for g in groups_},
        "not_reached_ha": round(float(nr.due_ha.sum()), 2),
        "not_reached_soonest_stress_days": float(nr.urgency_days.min()) if len(nr) else None,
        "water_ha_not_due": round(float(p[~p.due].area_ha.sum()), 2),
        "delivered_share_of_released_pct": round(100 * delivered / released, 1) if released else 0.0,
        "need_met_pct": round(100 * delivered / float(o.need_m3.sum()), 1) if o.need_m3.sum() else 100.0,
        "delivered_after_window_end_m3": round(late),
        "last_arrival_h": round(float(o.arrival_h[took].max()), 1) if took.any() else None,
        "offtakes_canal_too_low": cannot,
        "offtakes_canal_too_low_part_time": sorted(k for k, b in info["blocked"].items() if b and k in used),
        "not_reached_canal_level_ha": round(float(o.due_ha[o.low_canal].sum()), 2),
        "night_held_quarters": int(sum(info["held"].values())),
        "check_gate_note": "offtakes listed as canal too low would need a check gate downstream to draw "
                           f"(with one, the minimum drops to {REAL['min_draw_share_with_check_gate']:.0%} of reach design flow)",
        "label": CFG["label"]}
    summary["check_gates_assumed"] = mode != "plain"
    summary["reach_blocks_on"] = blocks
    summary.update({
        "operating_logic": OL["version"], "concentrate_full_turns": bool(concentrate),
        "main_velocity_m_s": round(v_main, 3),
        "head": {"confirmed_m3": round(hp["confirmed_m3"]), "released_m3": round(hp["released_m3"]),
                 "plateau_m3s": hp["plateau_m3s"], "ramp_up_m3": round(hp["ramp_up_m3"]),
                 "ramp_down_m3": round(hp["ramp_down_m3"]), "extension_h": hp["extension_h"],
                 "ramp_down_after_window_h": hp["ramp_down_after_window_h"],
                 "released_equals_confirmed": bool(hp["within_tolerance"]), "deviations": deviations},
        "reach_blocks": by_block if blocks else [],
        "escape_pct": round(100 * esc_out / released, 1) if released else 0.0,
        "escape_by_block": by_block, "escape_peak_m3s": round(esc_peak, 3),
        "escape_capacity_m3s": ES["capacity_m3s"],
        "routing_flag": ROUTE_FLAG if any(x["over_target"] for x in by_block) else None,
        "routing_error": ROUTE_FLAG if over_cap else None,
        "escape_near_limit": bool(not over_cap and esc_peak > ES["warn_share_of_capacity"] * ES["capacity_m3s"]),
        "escape_waste_warning": bool(released and esc_out > ES["waste_warn_share"] * released),
        "close_events": int(sum(e["closes"] for e in ev.values())),
        "reopen_events": int(sum(e["reopens"] for e in ev.values())),
        "close_reopen_loss_m3": round(acc["drain"] + acc["refill"]),
        "holding_quarters": int(sum((v > 0).sum() for v in hold.values())),
        "secondaries_under_12h": short,
        "end_canal_water_to_tail_m3": round(acc["end_off"]),
        "night_m3": round(float(o.night_m3.sum())),
        "outlets_with_night_turn": int((o.night_m3 > 0).sum()),
        "carry_over_reasons": {k: int(v) for k, v in o.reason[o.carry_over].value_counts().items()}})
    body = {"supply": supply["supply_id"], "losses": losses, "band": band, "check_gates": check_gates,
            "history": {k: sorted(v) for k, v in (history or {}).items()}, "network": ACTIVE["network_dir"],
            "planner": CFG["version"], "logic": OL["version"]}
    pid = "W" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
    return Plan(pid, supply, supply["planning"], losses, o, offt, account, summary)


def stress_test(p: Plan, head_extra: float, late_minutes: float) -> dict:
    """Realism fix 4: a test of how the plan behaves with real-world behaviour, run next to the plan (the plan is not
    changed). On every secondary, the outlets in the head third (by path along the canals) take head_extra more than
    planned, and every served turn runs late_minutes longer at its stream; the extra water is taken from the outlets
    farthest down the same secondary, tail first. Reports the tail shortfall. Names no person."""
    o = p.outlets
    d = o.delivered_m3.copy()
    took = d > 0
    tail = pd.Series(False, index=o.index)
    extra_total = 0.0
    for sec, g in o[took].groupby("secondary"):
        g = g.sort_values("path_m")
        k = max(1, int(round(len(g) * REAL["stress_test"]["head_share_of_outlets"])))
        head, rest = g.index[:k], g.index[k:]
        take = float((g.loc[head, "delivered_m3"] * head_extra).sum()
                     + (g.stream_m3s * late_minutes * 60).sum())                # overruns, at field inlets
        tail[rest] = True
        extra_total += take
        for x in reversed(list(rest)):                                       # tail first
            if take <= 1e-9:
                break
            cut = min(take, d[x])
            d[x] -= cut
            take -= cut
    planned_tail = float(o.delivered_m3[tail].sum())
    after_tail = float(d[tail].sum())
    lost_turn = int(((o.delivered_m3 > 0) & (d <= 1e-6) & tail).sum())
    need = float(o.need_m3.sum())
    return {"head_extra": head_extra, "late_minutes": late_minutes, "plan_id": p.plan_id,
            "extra_taken_m3": round(extra_total),
            "tail_planned_m3": round(planned_tail), "tail_after_m3": round(after_tail),
            "tail_shortfall_m3": round(planned_tail - after_tail),
            "tail_shortfall_pct": round(100 * (planned_tail - after_tail) / planned_tail, 1) if planned_tail else 0.0,
            "tail_outlets_losing_turn": lost_turn,
            "need_met_pct_plan": p.summary["need_met_pct"],
            "label": "test of how the plan behaves with real-world behaviour; simulation, names no person"}


def stress_tests(p: Plan) -> list[dict]:
    """Every combination of the configured head extras and late minutes."""
    st = REAL["stress_test"]
    return [stress_test(p, e, m) for e in st["head_extra"] for m in st["late_minutes"]]


def stress_rider_late(p: Plan, late_h: float | None = None) -> dict:
    """G6: the ditch rider reaches the gates late_h after the 19:00 change. Night turns that start in those hours lose
    them; the outlets still running at 19:00 keep their stream that long: what they still need is delivered, the rest
    passes to the escape. Run next to the plan (unchanged). Names no person."""
    late_h = OL["stress_test"]["rider_late_h"] if late_h is None else late_h
    o = p.outlets
    start = datetime.fromisoformat(p.supply["start"])

    def after19(q):                                                       # hours after the last 19:00 change
        t = start + timedelta(hours=q * Q_H)
        return (t.hour - 19) % 24 + t.minute / 60

    since = {k: after19(v) for k, v in o.first_q.dropna().items()}
    cut = {k: min(float(o.at[k, "delivered_m3"]), float(o.at[k, "stream_m3s"]) * 3600 * (late_h - h))
           for k, h in since.items() if h < late_h}                       # night turns that start late
    over = 0.0
    useful = 0.0
    for k, lq in o.last_q.dropna().items():
        if after19(lq + 1) == 0:                                          # stopped at the 19:00 change: runs on
            extra = float(o.at[k, "stream_m3s"]) * 3600 * late_h
            room = max(0.0, float(o.at[k, "need_m3"] - o.at[k, "delivered_m3"]))
            useful += min(extra, room)
            over += extra - min(extra, room)
    lost = sum(cut.values())
    need = float(o.need_m3.sum())
    return {"test": "ditch rider late at 19:00", "late_h": late_h, "plan_id": p.plan_id,
            "night_turns_hit": len(cut), "night_water_lost_m3": round(lost),
            "overrun_delivered_m3": round(useful), "overrun_to_escape_m3": round(over),
            "need_met_pct_plan": p.summary["need_met_pct"],
            "need_met_pct_after": round(100 * (float(o.delivered_m3.sum()) - lost + useful) / need, 1) if need else 100.0,
            "label": "test of how the plan behaves with real-world behaviour; simulation, names no person"}


def stress_night_only(p: Plan) -> dict:
    """G6: reaches served only by night turns: secondaries whose water came (almost) all at night, and what they lose
    if the outlets that take night turns only by the acceptance share decline them. Names no person."""
    o = p.outlets
    share = OL["stress_test"]["night_only_share"]
    by = o.groupby("main").agg(delivered=("delivered_m3", "sum"), night=("night_m3", "sum"), ha=("due_ha", "sum"))
    only = by[(by.delivered > 0) & (by.night >= share * by.delivered)]
    risk = o[o.main.isin(only.index) & (o.night_m3 > 0) & ~o["tail"] & ~o.large]
    need = float(o.need_m3.sum())
    lost = float(risk.night_m3.sum())
    return {"test": "reaches served only by night turns", "plan_id": p.plan_id,
            "offtakes_night_only": sorted(only.index), "night_only_ha": round(float(only.ha.sum()), 2),
            "outlets_at_risk": int(len(risk)), "at_risk_ha": round(float(risk.due_ha.sum()), 2),
            "at_risk_m3": round(lost), "need_met_pct_plan": p.summary["need_met_pct"],
            "need_met_pct_if_declined": round(100 * (float(o.delivered_m3.sum()) - lost) / need, 1) if need else 100.0,
            "label": "test of how the plan behaves with real-world behaviour; simulation, names no person"}


def operating_stress_tests(p: Plan) -> list[dict]:
    """G6: the two operating-logic stress tests."""
    return [stress_rider_late(p), stress_night_only(p)]
