"""Canal operations core (canal_ops_sim_v1): fixed-code water sharing, losses, turns, soil water and invariants.

Every number the Irrigation Assistant shows comes from here; agents only choose parameters and explain results.
Rules and parameters: config/canal_ops_sim_v1.json (frozen). Units: volumes m3 per dekad, flows l/s, hours, mm.

Water chain (one place only): supply S is gross at the canal head; allocation x[g] is gross at gate g after losses;
need[g] = net crop need / field efficiency. Delivering x[g] at a gate uses x[g] * cost[g] at the head, where cost
reflects main-canal and branch losses by distance.

Sharing is progressive filling (max-min fair water-filling) under one supply limit and per-branch capacity limits:
  x[g](t) = min(upper[g], min(t, t_branch*) * weight[g])
where t_branch* saturates the branch capacity and t is raised until the supply is used. This is exact for this
structure, deterministic and solver-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

CFG = json.loads((Path(__file__).resolve().parents[1] / "config/canal_ops_sim_v1.json").read_text(encoding="utf-8"))
GROUPS = ("perennial", "young_perennial", "sensitive_field", "other_green_field")
TOL = 1e-6


# ---------------------------------------------------------------- network quantities
def dekad_days(year: int, month: int, dekad: int) -> int:
    """Calendar dekad length: 10, 10 and 8-11 days."""
    if dekad < 3:
        return 10
    nxt = pd.Timestamp(year + (month == 12), month % 12 + 1, 1)
    return int((nxt - pd.Timestamp(year, month, 21)).days)


def head_cost(gates: pd.DataFrame, branches: pd.DataFrame, cfg=CFG) -> pd.Series:
    """m3 needed at the canal head per m3 delivered at each gate (losses by distance, assumed)."""
    lo = cfg["losses"]
    b = branches.set_index("branch")
    main_km = gates.branch.map(b.chainage_m) / 1000.0
    if lo["stage2_draws_through_stage1"]:
        main_km = main_km + gates.branch.map(b.upstream_m).fillna(0) / 1000.0
    keep_main = (1 - lo["main_canal_pct_per_km"] / 100.0) ** main_km
    keep_branch = (1 - lo["branch_pct_per_km"] / 100.0) ** gates.dist_km
    return 1.0 / (keep_main * keep_branch)


def branch_volume_cap(branches: pd.DataFrame, days: int) -> pd.Series:
    """Most a branch can carry in a dekad running continuously at its capacity (m3)."""
    return branches.set_index("branch").capacity_lps * 3.6 * 24 * days


# ---------------------------------------------------------------- progressive filling
def _branch_saturation(w, u, cap):
    """Largest t at which sum(min(u, t*w)) <= cap for one branch (inf if never saturated)."""
    if (np.minimum(u, np.inf) * 1).sum() <= cap + TOL:
        return np.inf
    lo, hi = 0.0, 1.0
    while np.minimum(u, hi * w).sum() < cap:
        hi *= 2
    for _ in range(80):
        mid = (lo + hi) / 2
        if np.minimum(u, mid * w).sum() <= cap:
            lo = mid
        else:
            hi = mid
    return lo


def fill(weight, upper, cost, supply, branch, branch_cap):
    """Max-min fair filling of gate increments.

    weight[g] > 0 shares the water, upper[g] >= 0 is the most gate g may receive at this step, cost[g] is head m3 per
    gate m3, supply is the head volume available, branch[g] is each gate's branch, branch_cap maps branch -> remaining
    volume. Returns gate volumes x with sum(cost*x) <= supply, x <= upper, per-branch sums <= branch_cap.
    """
    w = np.asarray(weight, float)
    u = np.maximum(np.asarray(upper, float), 0)
    a = np.asarray(cost, float)
    br = np.asarray(branch)
    w = np.where(u > 0, np.maximum(w, 1e-12), 0.0)
    tb = np.full(len(w), np.inf)
    for b in np.unique(br):
        m = br == b
        tb[m] = _branch_saturation(w[m], u[m], max(branch_cap.get(b, np.inf), 0.0))

    def x_at(t):
        return np.minimum(u, np.minimum(t, tb) * w)

    full = x_at(1e18)
    if (a * full).sum() <= supply + TOL:
        return full
    lo, hi = 0.0, 1.0
    while (a * x_at(hi)).sum() < supply:
        hi *= 2
    for _ in range(80):
        mid = (lo + hi) / 2
        if (a * x_at(mid)).sum() <= supply:
            lo = mid
        else:
            hi = mid
    return x_at(lo)


# ---------------------------------------------------------------- sharing rules
@dataclass
class Plan:
    rule: str
    alloc: pd.DataFrame            # gate x group: need, alloc (gross m3 at gate)
    supply: float                  # head m3 available
    used: float                    # head m3 used
    floors_met: bool
    notes: list = field(default_factory=list)

    def gate_totals(self):
        return self.alloc.groupby("gate")[["need", "alloc"]].sum()


def _setup(demand, gates, branches, days, cfg):
    d = demand.merge(gates[["gate", "branch", "dist_km", "ha"]], on="gate", how="left", suffixes=("", "_gate"))
    eff = d.ltype.map(cfg["water_chain"]["field_efficiency"]).fillna(0.6)
    d["need"] = d.net_m3 / eff                     # gross at gate: efficiency applied exactly once, here
    cost = head_cost(gates, branches, cfg)
    d["cost"] = d.gate.map(dict(zip(gates.gate, cost)))
    caps = branch_volume_cap(branches, days).to_dict()
    return d, caps


def _level_targets(d, cfg):
    """Rule A level targets per row (cumulative share of need), honouring floor conditions."""
    fl = cfg["floors"]
    can_floor = (d.stage_weight >= fl["apply_min_stage_weight"]) & ~(fl["never_while_ripening"] & d.ripening)
    perennial_floor = d.ltype.map(fl["perennial_share_of_etc"]).fillna(0.5)
    targets = []
    for lv in cfg["rules"]["A"]["levels"]:
        if lv["group"] == "perennial":
            share = np.where(d.group == "young_perennial", lv.get("young_share", lv["share"]),
                             np.where(d.group == "perennial",
                                      perennial_floor if lv["key"] == "L1" else lv["share"], 0.0))
            share = np.where(can_floor | (lv["key"] != "L1"), share, 0.0)
        elif lv["group"] == "all":
            share = np.ones(len(d))
        else:
            share = np.where(d.group == lv["group"], lv["share"], 0.0)
            if lv["key"] == "L2":
                share = np.where(can_floor, share, 0.0)
        targets.append((lv["key"], np.asarray(share, float)))
    return targets


def run_levels(d, levels, supply, caps):
    """Fill level by level; within a level, in proportion to the level's increment. Returns gate-row allocations."""
    x = np.zeros(len(d))
    rem_supply = supply
    rem_caps = dict(caps)
    floors_met = True
    met = {}
    for key, share in levels:
        target = np.maximum(share * d.need.to_numpy(), x)
        inc = target - x
        if inc.sum() <= TOL:
            met[key] = True
            continue
        bc = {b: rem_caps.get(b, np.inf) for b in d.branch.unique()}
        y = fill(inc, inc, d.cost.to_numpy(), rem_supply, d.branch.to_numpy(), bc)
        x += y
        rem_supply -= float((d.cost.to_numpy() * y).sum())
        for b, v in pd.Series(y).groupby(d.branch.to_numpy()).sum().items():
            rem_caps[b] = rem_caps.get(b, np.inf) - v
        met[key] = bool((target - x).sum() <= 1e-3 * max(target.sum(), 1))
    return x, met


def _zero_streak_level(d, zero_last, cfg):
    """Rows (gate, group) that received nothing last dekad get a first-priority floor (hard rule: no green
    gate cluster at zero two dekads running). zero_last comes from the period ledger."""
    if not zero_last:
        return []
    key = list(zip(d.gate, d.group))
    share = np.array([cfg["floors"]["zero_streak_floor_share"] if k in zero_last else 0.0 for k in key])
    return [("Z", share)]


def rule_A(demand, gates, branches, supply, days, cfg=CFG, zero_last=None) -> Plan:
    d, caps = _setup(demand, gates, branches, days, cfg)
    x, met = run_levels(d, _zero_streak_level(d, zero_last, cfg) + _level_targets(d, cfg), supply, caps)
    d["alloc"] = x
    floors_met = all(met.get(k, True) for k in ("Z", "L1", "L2"))
    notes = [] if floors_met else ["floors not met: floor levels scaled in proportion"]
    return Plan("A", d, supply, float((d.cost * d.alloc).sum()), floors_met, notes)


def rule_B(demand, gates, branches, supply, days, cfg=CFG, weight_col="ha") -> Plan:
    """Equal water per irrigated (or requested) hectare, capped at need."""
    d, caps = _setup(demand, gates, branches, days, cfg)
    w = d[weight_col].clip(lower=0).to_numpy() if weight_col in d else d.ha.to_numpy()
    x = fill(w, d.need.to_numpy(), d.cost.to_numpy(), supply, d.branch.to_numpy(), caps)
    d["alloc"] = x
    return Plan("B", d, supply, float((d.cost * d.alloc).sum()), True)


def rule_C(demand, gates, branches, supply, days, limits, cfg=CFG):
    """In proportion to a planned limit per stage. Returns None when no limit is connected."""
    if not limits:
        return None
    d, caps = _setup(demand, gates, branches, days, cfg)
    stage = d.branch.map(branches.set_index("branch").stage)
    tot = sum(limits.values())
    x = np.zeros(len(d))
    for st, lim in limits.items():
        m = (stage == st).to_numpy()
        if m.any():
            x[m] = fill(d.need[m].to_numpy(), d.need[m].to_numpy(), d.cost[m].to_numpy(), supply * lim / tot,
                        d.branch[m].to_numpy(), caps)
    d["alloc"] = x
    return Plan("C", d, supply, float((d.cost * d.alloc).sum()), True)


def simulate_plan(demand, gates, branches, supply, days, group_weights=None, branch_floors=None, cfg=CFG,
                  zero_last=None, all_floor=0.0) -> Plan:
    """The agent's decision space: branch floors (share of need, filled first) then rule A levels with group weights
    (relative priority inside the proportional level L5). Standing constraints arrive as branch_floors."""
    d, caps = _setup(demand, gates, branches, days, cfg)
    levels = _zero_streak_level(d, zero_last, cfg)
    if all_floor:                                   # a minimum share for every gate and group, filled first
        levels.append(("G", np.full(len(d), float(all_floor))))
    if branch_floors:
        share = d.branch.map(branch_floors).fillna(0.0).to_numpy()
        levels.append(("F", share))
    base = _level_targets(d, cfg)
    levels += base[:-1]
    x, met = run_levels(d, levels, supply, caps)
    gw = d.group.map(group_weights or {}).fillna(1.0).to_numpy()
    rem = supply - float((d.cost.to_numpy() * x).sum())
    used_b = pd.Series(x).groupby(d.branch.to_numpy()).sum()
    bc = {b: caps.get(b, np.inf) - used_b.get(b, 0.0) for b in d.branch.unique()}
    inc = np.maximum(d.need.to_numpy() - x, 0)
    x += fill(inc * gw, inc, d.cost.to_numpy(), max(rem, 0), d.branch.to_numpy(), bc)
    d["alloc"] = x
    floors_met = all(met.get(k, True) for k in ("Z", "G", "F", "L1", "L2"))
    return Plan("agent", d, supply, float((d.cost * d.alloc).sum()), floors_met,
                [] if floors_met else ["floors not met: floor levels scaled in proportion"])


# ---------------------------------------------------------------- metrics, turns, soil water
def metrics(plan: Plan) -> dict:
    d = plan.alloc
    g = plan.gate_totals()
    pct = (g.alloc / g.need.where(g.need > 0)).dropna()
    by_group = (d.groupby("group").alloc.sum() / d.groupby("group").need.sum().where(lambda s: s > 0)).dropna()
    return {"rule": plan.rule, "supply_m3": round(plan.supply), "used_m3": round(plan.used),
            "need_m3": round(float(d.need.sum())), "alloc_m3": round(float(d.alloc.sum())),
            "need_met_pct": round(100 * float(d.alloc.sum() / max(d.need.sum(), 1)), 1),
            "need_met_by_group_pct": {k: round(100 * float(v), 1) for k, v in by_group.items()},
            "worst_gate_pct": round(100 * float(pct.min()), 1) if len(pct) else None,
            "equity_min_over_mean": round(float(pct.min() / pct.mean()), 3) if len(pct) and pct.mean() > 0 else None,
            "floors_met": plan.floors_met}


def build_turns(plan: Plan, branches: pd.DataFrame, days: int, order: str = "tail_first", cfg=CFG) -> pd.DataFrame:
    """Turn chart: each branch runs continuously; its gates rotate in lanes of one gate stream each.
    hours = V / (0.8 * Q * 3.6) + fill hours. Flags lanes that do not fit the dekad."""
    t = cfg["turns"]
    g = plan.alloc.groupby(["gate", "branch"], as_index=False).agg(alloc=("alloc", "sum"), dist_km=("dist_km", "first"))
    b = branches.set_index("branch")
    rows = []
    for br, gg in g.groupby("branch"):
        q_branch = float(b.loc[br, "capacity_lps"])
        stream = min(t["gate_stream_lps"], q_branch)
        lanes = max(1, int(q_branch * t["efficiency_of_flow"] // stream))
        gg = gg[gg.alloc > TOL].sort_values("dist_km", ascending=(order != "tail_first"))
        lane_end = [0.0] * lanes
        for r in gg.itertuples():
            hours = r.alloc / (t["efficiency_of_flow"] * stream * 3.6) + t["fill_hours"]
            lane = int(np.argmin(lane_end))
            start = lane_end[lane]
            lane_end[lane] = start + hours
            rows.append({"branch": br, "gate": r.gate, "lane": lane, "start_h": round(start, 1),
                         "end_h": round(start + hours, 1), "hours": round(hours, 1),
                         "fits_dekad": start + hours <= 24 * days,
                         "night": any(((h % 24) >= 20) or ((h % 24) < 6) for h in np.arange(start, start + hours, 1.0))})
        total = sum(lane_end)
        low = total and (sum(r["hours"] for r in rows if r["branch"] == br) / (24 * days * lanes)) < t["min_flow_share"]
        for r in rows:
            if r["branch"] == br:
                r["section_turns"] = bool(low)
    return pd.DataFrame(rows)


def bucket_update(dr, taw, p, etc, rain_eff, applied_net, days=1):
    """One soil-water step of `days` days (FAO-56 ch. 8). dr = depletion (mm); etc, rain_eff, applied_net in mm for
    the step. Returns new depletion, actual ET (mm) and Ks."""
    p_adj = float(np.clip(p + 0.04 * (5 - etc / max(days, 1)), 0.1, 0.8))
    raw = p_adj * taw
    ks = 1.0 if dr <= raw else max(0.0, (taw - dr) / ((1 - p_adj) * taw))
    eta = ks * etc
    new = min(taw, max(0.0, dr + eta - rain_eff - applied_net))
    return new, eta, ks


def yield_proxy(eta_sum, etc_sum, ky=1.0, band=CFG["yield_proxy"]["band"]):
    """Indicative relative loss (field land only): Ky (1 - ETa/ETc), with a +-band range."""
    if etc_sum <= 0:
        return None
    loss = ky * (1 - eta_sum / etc_sum)
    return {"loss": round(max(loss, 0), 3), "low": round(max(loss * (1 - band), 0), 3), "high": round(max(loss * (1 + band), 0), 3)}


# ---------------------------------------------------------------- invariants (used by the Checker)
def invariants(plan: Plan, caps: dict | None = None) -> list[str]:
    d = plan.alloc
    errs = []
    if (d.alloc < -TOL).any():
        errs.append("negative allocation")
    if (d.alloc > d.need + 1e-3).any():
        errs.append("allocation above need")
    if plan.used > plan.supply * (1 + 1e-6) + 1:
        errs.append("head supply exceeded")
    if caps:
        over = d.groupby("branch").alloc.sum() - pd.Series(caps)
        if (over.dropna() > 1).any():
            errs.append("branch capacity exceeded")
    return errs
