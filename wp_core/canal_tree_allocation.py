"""Water sharing down the tree network v2 (locked plan v6, checklist D3; 2b link 6 and 2c hydraulics).

The v1 core (wp_core/canal_allocation.py) shares water between gates of one branch level and stays as it is. This
module does the same job on the multi-level tree of data/analysis/canal_ops_sim_v1/network_v2:
head -> main gates and pumps -> secondary canal -> secondary gates, pumps and direct outlets -> tertiary canal ->
tertiary gates -> parcels. Units: m3 per dekad, l/s, hours.

Rows are (outlet, crop group): the lowest unit is an outlet (tertiary gate or direct outlet) with the parcels it
serves, grouped as in v1 (perennial, sensitive field, other green field). Need = net plan-time need / field efficiency
(v1 config, applied once). Water delivered at an outlet costs more at every node above it because each link loses
water, in proportion to distance. Rates come from the loss chain (config/canal_ops_losses_v1.json, owner: the main
canal loses 15 % and the secondary network 30 % on average); without it, network v2's 0.5 / 2 / 3 %/km (assumed).

Sharing is the v1 progressive filling, generalised to nested capacities: every node's capacity (l/s x dekad seconds)
bounds the water entering it. Within a step, x[i](t) = min(upper[i], weight[i] * min(t, tau[i])), where tau[i] is the
lowest saturation level of any node above row i, found depth by depth from the outlets up (exact for a tree), and t
rises until the head supply is used. Priority levels (rules A, B, the agent plan, floors, "never zero twice") are
filled one after another with the same levels at every node, so a parent is never cut in proportion.

Minimum flows decide how a volume is run, not how much: a gate whose dekad average flow is below its minimum opening
(at least 30 % of capacity and 20 l/s tertiary / 100 l/s secondary, network v2 `min_open_lps`) runs in rotation at
that minimum for fewer hours; a gate with no water is closed. Below 40 % of the main canal capacity the three
sections (upper stage I, lower stage I, stage II) take turns. Every link closes its balance:
in = offtake + water passed to the children + seepage on those links + spill.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from wp_core import canal_allocation as ca

ROOT = Path(__file__).resolve().parents[1]
# the network in use (v2 or the terrain-routed v3) is named in one place: config/canal_ops_network_active.json
NETWORK = json.loads((ROOT / "config/canal_ops_network_active.json").read_text(encoding="utf-8"))
NET_CFG = json.loads((ROOT / NETWORK["network_config"]).read_text(encoding="utf-8"))
NV2 = ROOT / NETWORK["network_dir"]
NETWORK_LOSS_PCT_PER_KM = {"main": NET_CFG["main_canal"]["loss_pct_per_km"], **NET_CFG["sizing"]["loss_pct_per_km"]}
LOSS_CFG = ROOT / NETWORK["losses"]
# loss chain v1 (owner: main canal 15 %, secondary network 30 % on average), calibrated by scripts/canal_ops_loss_chain_v1.py
LOSS_PCT_PER_KM = (json.loads(LOSS_CFG.read_text(encoding="utf-8"))["rates_pct_per_km"] if LOSS_CFG.exists()
                   else NETWORK_LOSS_PCT_PER_KM)
MAIN_CAPACITY_LPS = float(NET_CFG["main_canal"]["capacity_lps"])
SECTION_TURN_SHARE = 0.4                  # 2c: below 40 % of the main canal capacity, sections take turns
BALANCE_TOL = 0.001                       # 2c: every link closes within 0.1 % per dekad
OUTLET_KINDS = {"tertiary_gate", "direct_outlet"}
GATE_KINDS = {"main_gate", "main_pump", "secondary_gate", "secondary_pump", "tertiary_gate", "direct_outlet"}
TOL = 1e-6
BIG = 1e30                                # 'no limit' level; finite so that 0 x BIG stays 0


# ---------------------------------------------------------------- the tree (static)
@dataclass
class Tree:
    nodes: pd.DataFrame            # index node: kind, parent, stage, section, capacity_lps, min_open_lps, keep, depth
    path: dict                     # outlet -> [head, ..., outlet] node ids
    cost: pd.Series                # head m3 per m3 entering the node


def link_km(r) -> float:
    """Length of the link from the parent into this node (km)."""
    if r.kind == "main_canal":
        return (r.upstream_m or 0.0) / 1000.0 if r.parent else 0.0
    if r.kind in ("main_gate", "main_pump"):
        return r.chainage_m / 1000.0
    if r.kind in ("secondary_gate", "secondary_pump", "direct_outlet", "tertiary_gate"):
        return (0.0 if pd.isna(r.path_m) else r.path_m) / 1000.0
    return 0.0                                          # a canal starts at its gate; an escape takes no water here


def link_rate(r, rates: dict | None = None) -> float:
    rates = rates or LOSS_PCT_PER_KM
    if r.kind in ("main_canal", "main_gate", "main_pump"):
        return rates["main"]
    if r.kind == "tertiary_gate":
        return rates["tertiary"]
    return rates["secondary"]


def build_tree(nodes: pd.DataFrame, rates: dict | None = None) -> Tree:
    nd = nodes.set_index("node", drop=False).rename_axis(None).copy()
    nd["parent"] = nd.parent.where(nd.parent.notna(), None)
    nd["upstream_m"] = nd.upstream_m.fillna(0.0)
    nd["keep"] = [(1 - link_rate(r, rates) / 100.0) ** link_km(r) for r in nd.itertuples()]
    depth, cost = {}, {}
    for n in sorted(nd.index, key=len):                 # a parent's id is a prefix of its children's (I before II)
        p = nd.at[n, "parent"]
        depth[n] = 0 if p is None else depth[p] + 1
        cost[n] = (1.0 if p is None else cost[p]) / nd.at[n, "keep"]
    nd["depth"] = pd.Series(depth)
    path = {}
    for o in nd.index[nd.kind.isin(OUTLET_KINDS)]:
        chain, n = [], o
        while n is not None:
            chain.append(n)
            n = nd.at[n, "parent"]
        path[o] = chain[::-1]
    return Tree(nd, path, pd.Series(cost))


@lru_cache(maxsize=1)
def tree() -> Tree:
    return build_tree(pd.read_parquet(NV2 / "nodes.parquet"))


# ---------------------------------------------------------------- demand rows (outlet x crop group)
def demand_rows(parcel_need: pd.DataFrame) -> pd.DataFrame:
    """parcel_need: one dekad, columns outlet, ltype, area_ha, stage_weight, ripening, net_m3 (plan-time values).
    Returns rows per (outlet, group) with the columns the v1 level rules use."""
    d = parcel_need[parcel_need.net_m3 > 0].copy()
    sens = (d.stage_weight >= 0.8) & ~d.ripening.astype(bool)
    d["group"] = np.where(d.ltype.isin(["orchard", "vineyard", "perennial"]), "perennial",
                          np.where(sens, "sensitive_field", "other_green_field"))
    d = d.assign(sw=d.stage_weight * d.area_ha, rp=d.ripening.astype(float) * d.area_ha)
    agg = dict(ha=("area_ha", "sum"), net_m3=("net_m3", "sum"), sw=("sw", "sum"), rp=("rp", "sum"),
               ltype=("ltype", lambda s: s.mode().iat[0]))
    crop = {"bucket_group", "taw_mm", "p", "etc_mm_exp", "net_mm_full_exp", "days"} <= set(d.columns)
    if crop:                                         # crop fields for the rotation periods (canal_rotation, FAO-56)
        d = d.assign(tw=d.taw_mm * d.area_ha, pw=d.p * d.area_ha, ew=d.etc_mm_exp / d.days * d.area_ha,
                     nw=d.net_mm_full_exp / d.days * d.area_ha)
        agg.update(crop=("bucket_group", lambda s: s.mode().iat[0]), tw=("tw", "sum"), pw=("pw", "sum"),
                   ew=("ew", "sum"), nw=("nw", "sum"))
    g = d.groupby(["outlet", "group"], as_index=False).agg(**agg)
    g["stage_weight"] = g.sw / g.ha
    g["ripening"] = (g.rp / g.ha) > 0.5
    if crop:
        g = g.assign(taw_mm=g.tw / g.ha, p=g.pw / g.ha, etc_day=g.ew / g.ha, net_day=g.nw / g.ha).drop(
            columns=["tw", "pw", "ew", "nw"])
    return g.drop(columns=["sw", "rp"]).sort_values(["outlet", "group"]).reset_index(drop=True)


# ---------------------------------------------------------------- nested progressive filling
class Structure:
    """Row-node incidence for one set of rows: which capacity nodes each row passes, with the volume factor
    a[N, i] = cost[i] / cost[N] (m3 entering N per m3 delivered at row i's outlet)."""

    def __init__(self, t: Tree, rows: pd.DataFrame):
        self.t, self.rows = t, rows
        nd = t.nodes
        ids = [n for n in nd.index]
        self.idx = {n: k for k, n in enumerate(ids)}
        self.ids = ids
        pn, pr, pa = [], [], []
        cost_row = np.array([t.cost[o] for o in rows.outlet])
        for i, o in enumerate(rows.outlet):
            for n in t.path[o]:
                pn.append(self.idx[n])
                pr.append(i)
                pa.append(cost_row[i] / t.cost[n])
        self.pn, self.pr, self.pa = np.array(pn), np.array(pr), np.array(pa)
        self.cost = cost_row
        self.depth = nd.depth.to_numpy()[self.pn]
        self.levels = sorted(set(self.depth.tolist()), reverse=True)

    def inflow(self, x: np.ndarray) -> np.ndarray:
        """Water entering every node for row deliveries x."""
        return np.bincount(self.pn, self.pa * x[self.pr], minlength=len(self.ids))


def fill_tree(s: Structure, weight, upper, supply, cap_left: np.ndarray):
    """Max-min fair filling with nested node capacities (cap_left: m3 still free at each node entry) and one head
    supply. Returns row deliveries y with y <= upper, inflow(y) <= cap_left at every node, sum(cost*y) <= supply."""
    w = np.asarray(weight, float)
    u = np.maximum(np.asarray(upper, float), 0)
    w = np.where(u > 0, np.maximum(w, 1e-12), 0.0)
    tau = np.full(len(u), BIG)
    for lev in s.levels:                                # deepest nodes first
        m = s.depth == lev
        pn, pr, pa = s.pn[m], s.pr[m], s.pa[m]
        nodes = np.unique(pn)
        cap = cap_left[nodes]
        lookup = np.full(len(s.ids), -1)
        lookup[nodes] = np.arange(len(nodes))
        k = lookup[pn]

        def load(t):                                    # t per node -> water entering each node
            tt = np.minimum(t[k], tau[pr])
            return np.bincount(k, pa * np.minimum(u[pr], w[pr] * tt), minlength=len(nodes))

        full = load(np.full(len(nodes), BIG))
        bind = full > cap + TOL
        if not bind.any():
            continue
        lo, hi = np.zeros(len(nodes)), np.ones(len(nodes))
        for _ in range(200):                            # find an upper bound for every binding node
            over = bind & (load(hi) < cap)
            if not over.any():
                break
            hi = np.where(over, hi * 4, hi)
        for _ in range(70):
            mid = (lo + hi) / 2
            ok = load(mid) <= cap
            lo, hi = np.where(ok, mid, lo), np.where(ok, hi, mid)
        tn = np.where(bind, lo, BIG)
        np.minimum.at(tau, pr, tn[k])

    def x_at(t):
        return np.minimum(u, w * np.minimum(t, tau))

    full = x_at(BIG)
    if (s.cost * full).sum() <= supply + TOL:
        return full
    lo, hi = 0.0, 1.0
    while (s.cost * x_at(hi)).sum() < supply:
        hi *= 2
    for _ in range(80):
        mid = (lo + hi) / 2
        if (s.cost * x_at(mid)).sum() <= supply:
            lo = mid
        else:
            hi = mid
    return x_at(lo)


# ---------------------------------------------------------------- plans
@dataclass
class TreePlan:
    rule: str
    rows: pd.DataFrame             # outlet x group: need, alloc (m3 at the outlet)
    supply: float                  # head m3 available after the 12 m3/s cap
    used: float                    # head m3 taken
    days: int
    floors_met: bool
    head_spill: float = 0.0        # m3 offered above the main canal capacity (spill, never delivered)
    notes: list = field(default_factory=list)
    structure: Structure = None

    def node_table(self) -> pd.DataFrame:
        return node_table(self)


def _caps(s: Structure, days: int) -> np.ndarray:
    nd = s.t.nodes
    cap = nd.capacity_lps.to_numpy(float) * 86.4 * days        # l/s over the dekad -> m3
    return np.where(np.isnan(cap), np.inf, cap)


def head_supply(volume_m3: float, days: int) -> tuple[float, float]:
    """Head volume accepted by the main canal (12 m3/s cap) and the volume spilled above it."""
    cap = MAIN_CAPACITY_LPS * 86.4 * days
    return min(volume_m3, cap), max(volume_m3 - cap, 0.0)


def run_levels(s: Structure, d: pd.DataFrame, levels, supply, cap):
    x = np.zeros(len(d))
    left, rem = cap.copy(), supply
    met = {}
    need = d.need.to_numpy()
    for key, share in levels:
        target = np.maximum(share * need, x)
        inc = target - x
        if inc.sum() <= TOL:
            met[key] = True
            continue
        y = fill_tree(s, inc, inc, rem, left)
        x += y
        rem -= float((s.cost * y).sum())
        left = left - s.inflow(y)
        met[key] = bool((target - x).sum() <= 1e-3 * max(target.sum(), 1))
    return x, met, left, rem


def _setup(rows: pd.DataFrame, cfg=ca.CFG):
    d = rows.copy()
    d["gate"] = d.outlet                              # the v1 level rules key "never zero twice" on (gate, group)
    d["need"] = d.net_m3 / d.ltype.map(cfg["water_chain"]["field_efficiency"]).fillna(0.6)
    return d


def _node_floor_share(d: pd.DataFrame, t: Tree, node_floors: dict) -> np.ndarray:
    """A floor on a node applies to every row below it; the highest floor on a row's path wins."""
    share = np.zeros(len(d))
    for i, o in enumerate(d.outlet):
        share[i] = max((float(node_floors.get(n, 0.0)) for n in t.path[o]), default=0.0)
    return share


def rule_A(rows, supply, days, t: Tree = None, zero_last=None, head_spill=0.0, cfg=ca.CFG) -> TreePlan:
    t = t or tree()
    d = _setup(rows, cfg)
    s = Structure(t, d)
    levels = ca._zero_streak_level(d, zero_last, cfg) + ca._level_targets(d, cfg)
    x, met, _, _ = run_levels(s, d, levels, supply, _caps(s, days))
    d["alloc"] = x
    ok = all(met.get(k, True) for k in ("Z", "L1", "L2"))
    return TreePlan("A", d, supply, float((s.cost * x).sum()), days, ok, head_spill,
                    [] if ok else ["floors not met: floor levels scaled in proportion"], s)


def rule_B(rows, supply, days, t: Tree = None, head_spill=0.0, cfg=ca.CFG) -> TreePlan:
    """Equal water per irrigated hectare, capped at need, under every node capacity."""
    t = t or tree()
    d = _setup(rows, cfg)
    s = Structure(t, d)
    x = fill_tree(s, d.ha.to_numpy(), d.need.to_numpy(), supply, _caps(s, days))
    d["alloc"] = x
    return TreePlan("B", d, supply, float((s.cost * x).sum()), days, True, head_spill, [], s)


def simulate_plan(rows, supply, days, t: Tree = None, group_weights=None, node_floors=None, all_floor=0.0,
                  zero_last=None, head_spill=0.0, cfg=ca.CFG) -> TreePlan:
    """The agent's decision space on the tree: "never zero twice" first, then a minimum share for every outlet
    (all_floor), node floors, rule A's floor levels, and the rest with group weights."""
    t = t or tree()
    d = _setup(rows, cfg)
    s = Structure(t, d)
    levels = ca._zero_streak_level(d, zero_last, cfg)
    if all_floor:
        levels.append(("G", np.full(len(d), float(all_floor))))
    if node_floors:
        levels.append(("F", _node_floor_share(d, t, node_floors)))
    levels += ca._level_targets(d, cfg)[:-1]
    x, met, left, rem = run_levels(s, d, levels, supply, _caps(s, days))
    gw = d.group.map(group_weights or {}).fillna(1.0).to_numpy()
    inc = np.maximum(d.need.to_numpy() - x, 0)
    x += fill_tree(s, inc * gw, inc, max(rem, 0.0), left)
    d["alloc"] = x
    ok = all(met.get(k, True) for k in ("Z", "G", "F", "L1", "L2"))
    return TreePlan("agent", d, supply, float((s.cost * x).sum()), days, ok, head_spill,
                    [] if ok else ["floors not met: floor levels scaled in proportion"], s)


# ---------------------------------------------------------------- results per node
def node_table(plan: TreePlan) -> pd.DataFrame:
    """Per node: need and water entering it, share of need met below it, average flow, how the gate runs, and its
    balance (in = offtake + passed to children + seepage on those links + spill)."""
    s, t, d = plan.structure, plan.structure.t, plan.rows
    nd = t.nodes
    x, need = d.alloc.to_numpy(), d.need.to_numpy()
    inflow = s.inflow(x)
    need_in = s.inflow(need)
    deliv = np.bincount(s.pn, x[s.pr], minlength=len(s.ids))       # delivered at outlets below
    need_below = np.bincount(s.pn, need[s.pr], minlength=len(s.ids))
    out = pd.DataFrame({"node": s.ids, "kind": nd.kind.to_numpy(), "parent": nd.parent.to_numpy(),
                        "section": nd.section.to_numpy(), "stage": nd.stage.to_numpy(),
                        "capacity_lps": nd.capacity_lps.to_numpy(), "min_open_lps": nd.min_open_lps.to_numpy(),
                        "keep": nd.keep.to_numpy(), "need_m3": need_in, "in_m3": inflow, "delivered_m3": deliv,
                        "need_below_m3": need_below}).set_index("node", drop=False).rename_axis(None)
    out["pct"] = np.where(out.need_below_m3 > 0, 100 * out.delivered_m3 / out.need_below_m3.where(out.need_below_m3 > 0), np.nan)
    secs = plan.days * 86400.0
    out["avg_lps"] = out.in_m3 * 1000 / secs
    # how the volume is run: continuous, rotation at the minimum opening, or closed
    mn = out.min_open_lps.fillna(0.0)
    opening = np.minimum(np.maximum(mn, 0.0), out.capacity_lps.fillna(np.inf))
    rot = (out.in_m3 > TOL) & (out.avg_lps < opening - 1e-9)
    out["mode"] = np.where(out.in_m3 <= TOL, "closed", np.where(rot, "rotation", "continuous"))
    out["open_lps"] = np.where(out["mode"] == "rotation", opening, np.where(out["mode"] == "closed", 0.0, out.avg_lps))
    out["hours"] = np.where(out.open_lps > 0, out.in_m3 / (out.open_lps * 3.6), 0.0)
    # balance: children take what enters them; the link into each child loses in_child * (1/keep - 1)
    kids = out[out.parent.notna()]
    to_kids = kids.groupby("parent").in_m3.sum()
    seep = (kids.in_m3 * (1 / kids.keep - 1)).groupby(kids.parent).sum()
    out["to_children_m3"] = out.node.map(to_kids).fillna(0.0)
    out["seepage_m3"] = out.node.map(seep).fillna(0.0)
    out["offtake_m3"] = np.where(out.kind.isin(OUTLET_KINDS), out.delivered_m3, 0.0)
    out["spill_m3"] = 0.0
    out["residual_m3"] = out.in_m3 - out.offtake_m3 - out.to_children_m3 - out.seepage_m3 - out.spill_m3
    return out


def balance(plan: TreePlan, nodes: pd.DataFrame | None = None) -> dict:
    """Head and link balance. Head: offered = accepted + spill; accepted >= taken."""
    nt = nodes if nodes is not None else node_table(plan)
    rel = (nt.residual_m3.abs() / nt.in_m3.where(nt.in_m3 > 0)).fillna(0.0)
    head = nt.loc[nt.parent.isna()].iloc[0]
    seep = float(nt.seepage_m3.sum())
    return {"head_offered_m3": round(plan.supply + plan.head_spill), "head_spill_m3": round(plan.head_spill),
            "head_in_m3": round(float(head.in_m3)), "delivered_m3": round(float(plan.rows.alloc.sum())),
            "seepage_m3": round(seep), "not_taken_m3": round(max(plan.supply - plan.used, 0.0)),
            "max_link_residual_pct": round(100 * float(rel.max()), 4),
            "closes": bool(rel.max() <= BALANCE_TOL)}


def section_turns(plan: TreePlan, nodes: pd.DataFrame | None = None) -> dict | None:
    """Below 40 % of main canal capacity on average, the three sections take turns at that flow."""
    nt = nodes if nodes is not None else node_table(plan)
    q = SECTION_TURN_SHARE * MAIN_CAPACITY_LPS
    if plan.used * 1000 / (plan.days * 86400) >= q:
        return None
    off = nt[nt.kind.isin(["main_gate", "main_pump"])]
    head_cost = off.node.map(plan.structure.t.cost)
    vol = (off.in_m3 * head_cost).groupby(off.section).sum()
    return {sec: round(float(v * 1000 / q / 86400), 2) for sec, v in vol.items() if v > 0}


def metrics(plan: TreePlan, nodes: pd.DataFrame | None = None) -> dict:
    d = plan.rows
    nt = nodes if nodes is not None else node_table(plan)
    o = nt[nt.kind.isin(OUTLET_KINDS) & (nt.need_below_m3 > 0)]
    pct = o.delivered_m3 / o.need_below_m3
    by_group = (d.groupby("group").alloc.sum() / d.groupby("group").need.sum().where(lambda v: v > 0)).dropna()
    return {"rule": plan.rule, "supply_m3": round(plan.supply), "used_m3": round(plan.used),
            "need_m3": round(float(d.need.sum())), "alloc_m3": round(float(d.alloc.sum())),
            "need_met_pct": round(100 * float(d.alloc.sum() / max(d.need.sum(), 1)), 1),
            # how effectively the head water is used: the share of it that reaches the farm outlets (the rest is
            # lost on the way, loss chain v1); need met is the indicator of how well that water covers the need
            "delivered_share_of_head_pct": round(100 * float(d.alloc.sum() / plan.used), 1) if plan.used > 0 else None,
            "need_met_by_group_pct": {k: round(100 * float(v), 1) for k, v in by_group.items()},
            "worst_outlet_pct": round(100 * float(pct.min()), 1) if len(pct) else None,
            "equity_min_over_mean": round(float(pct.min() / pct.mean()), 3) if len(pct) and pct.mean() > 0 else None,
            "outlets_closed": int((o.delivered_m3 <= TOL).sum()), "outlets": int(len(o)),
            "floors_met": plan.floors_met}


def invariants(plan: TreePlan, nodes: pd.DataFrame | None = None) -> list[str]:
    d, nt = plan.rows, (nodes if nodes is not None else node_table(plan))
    errs = []
    if (d.alloc < -TOL).any():
        errs.append("negative allocation")
    if (d.alloc > d.need + 1e-3).any():
        errs.append("allocation above need")
    if plan.used > plan.supply * (1 + 1e-6) + 1:
        errs.append("head supply exceeded")
    cap = nt.capacity_lps * 86.4 * plan.days
    if ((nt.in_m3 - cap) > 1 + 1e-6 * cap).fillna(False).any():
        errs.append("node capacity exceeded")
    par = nt.parent.map(nt.in_m3)
    if ((nt.in_m3 - par) > 1e-6 * par.fillna(0) + 1).fillna(False).any():
        errs.append("child above its parent")
    if not balance(plan, nt)["closes"]:
        errs.append("balance does not close within 0.1 %")
    open_ = nt[nt["mode"] != "closed"]
    low = open_.min_open_lps.notna() & (open_.open_lps < np.minimum(open_.min_open_lps, open_.capacity_lps) - 1e-6)
    if low.any():
        errs.append("gate opened below its minimum flow")
    return errs


def turns(plan: TreePlan, nodes: pd.DataFrame | None = None, cfg=ca.CFG) -> pd.DataFrame:
    """Turn chart per secondary canal: its gates, pumps and direct outlets run at their opening flow for their hours
    (plus fill time), tail first, in as many lanes as the canal's capacity carries at once."""
    nt = nodes if nodes is not None else node_table(plan)
    tc, path = cfg["turns"], plan.structure.t.nodes.path_m
    rows = []
    for sec, g in nt[nt.kind.isin({"secondary_gate", "secondary_pump", "direct_outlet"}) & (nt.in_m3 > TOL)].groupby("parent"):
        cap = float(nt.at[sec, "capacity_lps"])
        lanes = max(1, int(cap * tc["efficiency_of_flow"] // max(float(g.open_lps.median()), 1.0)))
        lane_end = [0.0] * lanes
        for n in sorted(g.index, key=lambda k: -float(path.get(k) or 0)):
            hours = float(g.at[n, "hours"]) + tc["fill_hours"]
            lane = int(np.argmin(lane_end))
            start = lane_end[lane]
            lane_end[lane] = start + hours
            rows.append({"branch": sec, "gate": n, "lane": lane, "start_h": round(start, 1), "end_h": round(start + hours, 1),
                         "hours": round(hours, 1), "open_lps": round(float(g.at[n, "open_lps"]), 1), "mode": g.at[n, "mode"],
                         "fits_dekad": start + hours <= 24 * plan.days,
                         "night": any(((h % 24) >= 20) or ((h % 24) < 6) for h in np.arange(start, start + hours, 1.0))})
    return pd.DataFrame(rows)
