"""Tool functions for the Irrigation Assistant agents (canal_ops_sim_v1). Pure Python; the SDK adapter wraps them as
in-process MCP tools named mcp__<server>__<tool>.

Rules enforced here (not in prompts):
  - every result gets an id (R1, R2, ...) and is stored, so the Checker can fill {{R<n>.<key>}} placeholders;
  - each tool returns only fields on its allowlist (tier T0/T1), anything else is refused (G3.11);
  - periods after `as_of` are refused (no hindsight);
  - simulate_plan is capped per agent per period (budget);
  - save_proposal accepts only a sim run id, never numbers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import pandas as pd

from wp_core import canal_allocation as ca

ALLOWED_FIELDS = {
    "get_network": {"branches", "gates_count", "stages", "label"},
    "deliverable": {"scenario", "period", "supply_m3", "need_m3", "head_need_m3", "label"},
    "compare_rules": {"period", "scenario", "rules", "label"},
    "simulate_plan": {"sim_run_id", "metrics", "branches", "floors_met", "notes", "label"},
    "get_history": {"periods"},
    "get_constraints": {"constraints"},
    "tail_risk": {"branches"},
    "save_proposal": {"proposal", "status"},
}
SERVERS = {"get_network": "canal", "deliverable": "canal", "compare_rules": "canal", "simulate_plan": "canal",
           "get_history": "canal", "get_constraints": "canal", "tail_risk": "canal", "save_proposal": "canal"}


class ToolRefused(Exception):
    pass


@dataclass
class World:
    """Everything the tools read: network, per-period demand, ledger, as_of. Built by fixed code."""
    branches: pd.DataFrame
    gates: pd.DataFrame
    demand: dict                      # period -> demand rows (gate, group, ltype, ha, net_m3, stage_weight, ripening)
    days: dict                        # period -> days in period
    head_capacity_m3: dict            # period -> normal supply at the head (m3)
    ledger: object
    as_of: str
    results: dict = field(default_factory=dict)
    sims: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)

    def _store(self, tool, data):
        extra = set(data) - ALLOWED_FIELDS[tool]
        if extra:
            raise ToolRefused(f"{tool} tried to return fields outside its allowlist: {sorted(extra)}")
        rid = f"R{len(self.results) + 1}"
        self.results[rid] = data
        return {"rid": rid, "data": data}

    def _period(self, period):
        if period > self.as_of:
            raise ToolRefused("that period is after the planning date; no hindsight")
        if period not in self.demand:
            raise ToolRefused("unknown period")
        return period


def _supply(world, period, scenario):
    f = ca.CFG["supply"]["scenarios"].get(scenario)
    if f is None:
        raise ToolRefused("unknown scenario")
    return world.head_capacity_m3[period] * f


def _branch_rows(plan):
    t = plan.alloc.groupby("branch").agg(need=("need", "sum"), alloc=("alloc", "sum"))
    t["pct"] = (100 * t.alloc / t.need.where(t.need > 0)).round(1)
    return {b: {"need_m3": round(float(r.need)), "alloc_m3": round(float(r.alloc)), "pct": None if pd.isna(r.pct) else float(r.pct)}
            for b, r in t.iterrows()}


# ---------------------------------------------------------------- tools
def get_network(world: World, agent: str = "main"):
    b = world.branches
    return world._store("get_network", {
        "branches": {r.branch: {"stage": r.stage, "type": r.offtake_type, "capacity_lps": float(r.capacity_lps),
                                "gates": int((world.gates.branch == r.branch).sum())} for r in b.itertuples()},
        "gates_count": int(len(world.gates)), "stages": sorted(b.stage.unique().tolist()),
        "label": ca.CFG["labels"]["network"]})


def deliverable(world: World, period: str, scenario: str = "normal", agent: str = "main"):
    world._period(period)
    d = world.demand[period]
    need = float((d.net_m3 / d.ltype.map(ca.CFG["water_chain"]["field_efficiency"]).fillna(0.6)).sum())
    cost = ca.head_cost(world.gates, world.branches)
    gate_need = (d.assign(n=d.net_m3 / d.ltype.map(ca.CFG["water_chain"]["field_efficiency"]).fillna(0.6))
                 .groupby("gate").n.sum())
    head_need = float((gate_need * gate_need.index.map(dict(zip(world.gates.gate, cost)))).sum())
    return world._store("deliverable", {"scenario": scenario, "period": period,
                                        "supply_m3": round(_supply(world, period, scenario)),
                                        "need_m3": round(need), "head_need_m3": round(head_need),
                                        "label": ca.CFG["labels"]["supply"]})


def compare_rules(world: World, period: str, scenario: str = "normal", agent: str = "main"):
    world._period(period)
    s, days, d = _supply(world, period, scenario), world.days[period], world.demand[period]
    zl = world.ledger.zero_last(period) if world.ledger else set()
    out = {}
    for name, plan in (("A", ca.rule_A(d, world.gates, world.branches, s, days, zero_last=zl)),
                       ("B", ca.rule_B(d, world.gates, world.branches, s, days))):
        out[name] = {"metrics": ca.metrics(plan), "branches": _branch_rows(plan)}
    out["C"] = {"status": ca.CFG["rules"]["C"]["when_missing"]}
    return world._store("compare_rules", {"period": period, "scenario": scenario, "rules": out,
                                          "label": ca.CFG["labels"]["rule"]})


def simulate_plan(world: World, period: str, scenario: str = "normal", group_weights=None, branch_floors=None,
                  all_floor: float = 0.0, agent: str = "planner"):
    world._period(period)
    key = (agent, period)
    world.budget[key] = world.budget.get(key, 0) + 1
    if world.budget[key] > ca.CFG["agent_budget"]["max_simulations_per_agent_per_dekad"]:
        raise ToolRefused("simulation budget for this period is used up")
    gw = {k: float(v) for k, v in (group_weights or {}).items() if k in ca.GROUPS and 0 <= float(v) <= 5}
    bf = {k: float(v) for k, v in (branch_floors or {}).items() if k in set(world.branches.branch) and 0 <= float(v) <= 1}
    for c in world.ledger.constraints(period).values() if world.ledger else []:
        if c.get("kind") == "branch_floor":                 # standing constraints are injected by the server
            bf[c["branch"]] = max(bf.get(c["branch"], 0), float(c["value"]))
    s, days, d = _supply(world, period, scenario), world.days[period], world.demand[period]
    zl = world.ledger.zero_last(period) if world.ledger else set()
    plan = ca.simulate_plan(d, world.gates, world.branches, s, days, gw, bf, zero_last=zl,
                            all_floor=min(max(float(all_floor or 0), 0.0), 0.5))
    errs = ca.invariants(plan, ca.branch_volume_cap(world.branches, days).to_dict())
    if errs:
        raise ToolRefused("plan failed invariants: " + "; ".join(errs))
    af = min(max(float(all_floor or 0), 0.0), 0.5)
    params = json.dumps({"p": period, "s": scenario, "gw": gw, "bf": bf, "af": af}, sort_keys=True)
    sid = "S" + hashlib.sha256(params.encode()).hexdigest()[:10]
    world.sims[sid] = {"plan": plan, "params": params, "period": period, "scenario": scenario}
    return world._store("simulate_plan", {"sim_run_id": sid, "metrics": ca.metrics(plan), "branches": _branch_rows(plan),
                                          "floors_met": plan.floors_met, "notes": plan.notes,
                                          "label": ca.CFG["labels"]["rule"]})


def tail_risk(world: World, period: str, sim_run_id: str, agent: str = "reviewer"):
    world._period(period)
    sim = world.sims.get(sim_run_id)
    if not sim:
        raise ToolRefused("unknown simulation")
    rows = _branch_rows(sim["plan"])
    ch = world.branches.set_index("branch").chainage_m + world.branches.set_index("branch").upstream_m
    tail = ch.rank(pct=True)
    return world._store("tail_risk", {"branches": {b: {"tail_position": round(float(tail[b]), 2), "pct": r["pct"],
                                                       "flag": bool(tail[b] >= 0.66 and (r["pct"] or 0) < 50)}
                                                   for b, r in rows.items()}})


def get_history(world: World, agent: str = "main"):
    h = world.ledger.history(world.as_of) if world.ledger else []
    return world._store("get_history", {"periods": [r["period"] for r in h]})


def get_constraints(world: World, agent: str = "main"):
    c = world.ledger.constraints(world.as_of) if world.ledger else {}
    return world._store("get_constraints", {"constraints": {k: {kk: v[kk] for kk in ("kind", "branch", "value", "version")
                                                                  if kk in v} for k, v in c.items()}})


def save_proposal(world: World, sim_run_id: str, author: str = "main", agent: str = "main"):
    sim = world.sims.get(sim_run_id)
    if not sim:
        raise ToolRefused("save_proposal takes only a known simulation id")
    input_hash = input_hash_of(world, sim["period"])
    pid = world.ledger.save_proposal(sim_run_id, input_hash, {"params": sim["params"]}, author)
    return world._store("save_proposal", {"proposal": pid, "status": "draft"})


def input_hash_of(world: World, period: str) -> str:
    d = world.demand[period]
    h = hashlib.sha256(pd.util.hash_pandas_object(d, index=False).values.tobytes())
    h.update(json.dumps(world.ledger.constraints(period) if world.ledger else {}, sort_keys=True, default=str).encode())
    return h.hexdigest()


TOOLS = {f.__name__: f for f in (get_network, deliverable, compare_rules, simulate_plan, tail_risk, get_history,
                                 get_constraints, save_proposal)}
