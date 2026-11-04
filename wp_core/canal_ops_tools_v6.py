"""Plan v6 agent tools: read tools on the tree network v2 and the map layers, water sharing down the tree with
land-type and community breakdowns per tree node, and the payload limits of plan section 2c. Pure Python, fixed code.

Live chat on 8554 uses this registry (owner decision 2026-11-02, "Switch now"); live answers stay off until the owner
approves the new payload (scripts/canal_ops_payload_review.py, typed APPROVE PAYLOAD).

Rules enforced here (not in prompts):
  - every result passes a field allowlist (ALLOWED_FIELDS_V6) and a size cap (MAX_RESULT_BYTES, about 6 KB);
  - totals first; every list is a page of at most PAGE_MAX items keyed by id, with a cursor to the next page;
    allocation lists hold ALLOC_PAGE nodes (owner-approved part-3 deviation 4) and drill down one tree level at a time;
  - hectares are rounded to 2 decimals;
  - no hindsight: dekads after the planning date are refused, parcel history stops before the planning year, and
    need uses the plan-time ("expected") values only;
  - internal profile only: parcel ids and cadastral codes are returned, never coordinates, internal parcel ids or
    paths. public_qa gets none of these tools (wp_core/canal_ops_policy_v6.py);
  - the data are read once, under the read guard (no SQL archive, nothing from the parent project);
  - simulate_plan is capped per agent per dekad; save_proposal and the submit tools take simulation ids, never numbers.

Water sharing (compare_rules, simulate_plan, tail_risk) runs on the tree (wp_core/canal_tree_allocation.py, checklist
D3). Normal head supply = supply v1.1 (P90 of the 2021-2024 zone gross need at the head per calendar dekad), computed
with the tree's losses; scenarios scale it; the 12 m3/s main canal cap applies and any excess is spill.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from wp_core import canal_allocation as ca
from wp_core import canal_ops_tools as T
from wp_core import canal_rotation as RT
from wp_core import canal_tree_allocation as A
from wp_core import read_guard

ROOT = Path(__file__).resolve().parents[1]
NV2 = A.NV2                               # the network in use (config/canal_ops_network_active.json)
MAP = ROOT / A.NETWORK["map_dir"]
DEMAND = ROOT / "data/analysis/canal_ops_sim_v1/demand/parcel_dekad_2025.parquet"
REFERENCE = ROOT / "data/analysis/canal_ops_sim_v1/demand/capacity_reference_2021_2024.parquet"
LAND_TYPES = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1/releases/landtypes_v1_5/parcels.parquet"
CODES = ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"
SUPPLY_CFG = ROOT / "config/canal_ops_supply_v1_1.json"

PAGE_MAX = 25
ALLOC_PAGE = 20
SCHEDULE_PAGE = 16                        # schedule rows carry clock times and who is next: 16 stay under 6 KB
MAX_RESULT_BYTES = 6000
MAP_CONTEXT_MAX_TOKENS = 1500
SIM_LABEL = "Simulation: synthetic network on the real main canal, not the real WUA network, not a water order"
CFG_ROUNDS = RT.CFG["round"]["rounds_per_dekad"]
ROTATION_RULES = {"round_days": "5 or 5.5", "hand_stream_lps": RT.HS, "change_times": ["07:00", "19:00"],
                  "night_slot": RT.CFG["pumps"]["night_slot"], "tertiary_min_lps": RT.CFG["streams"]["tertiary_min_lps"],
                  "min_open_share_pct": round(100 * RT.CFG["streams"]["min_open_share"]),
                  "july_period_days": {k: v * 5 for k, v in RT.CFG["periods"]["july_rounds"].items()},
                  "section_order": RT.SECTION_ORDER}
NEED_BASIS = "plan-time dekad need on land types v1.4 (owner: v1.4 demand for the pilot)"
HISTORY_CUT = "satellite seasons before the planning year only (no hindsight)"
GATE_KINDS = A.GATE_KINDS
NODE_ID = re.compile(r"^(?:I|II)(?:-[GP]\d{2}(?:-S1(?:-[GP]\d+(?:-T1(?:-G\d+)?)?|-D\d+|-E)?)?)?$")
ITEM_ID = re.compile(r"^f\d{5}$")
CODE = re.compile(r"^\d{2}-\d{3}-\d{4}-\d{4}$")
TYPES = ["FIELD_CROP", "NO_CROP", "CROPLAND_UNCERTAIN", "ORCHARD", "VINEYARD", "PERENNIAL", "GREENHOUSE"]
TERRAIN = {"gravity": "ha_gravity", "pumping": "ha_pumping", "terrain uncertain": "ha_terrain_uncertain",
           "terrain not covered": "ha_terrain_not_covered"}

PAGED = {"cursor"}
ALLOWED_FIELDS_V6 = {
    # v1 shapes, unchanged
    "deliverable": set(T.ALLOWED_FIELDS["deliverable"]),
    "get_history": set(T.ALLOWED_FIELDS["get_history"]),
    "get_constraints": set(T.ALLOWED_FIELDS["get_constraints"]),
    "save_proposal": set(T.ALLOWED_FIELDS["save_proposal"]),
    # the tree in totals, then the offtakes
    "get_network": {"totals", "offtakes", "label"} | PAGED,
    # water sharing on the tree: totals first, then one tree level (offtakes or a node's children), with breakdowns
    "compare_rules": {"period", "scenario", "rules", "node", "nodes", "label"} | PAGED,
    "simulate_plan": {"sim_run_id", "applied", "metrics", "floors_met", "notes", "balance", "rounds", "node", "nodes",
                      "label"} | PAGED,
    "tail_risk": {"totals", "node", "nodes"} | PAGED,
    # read tools on the tree network v2 and the map
    "get_tree": {"node", "parent", "position", "below", "children", "label"} | PAGED,
    "get_gate": {"gate", "communities", "need", "parcels", "label"} | PAGED,
    "get_parcel": {"parcel", "cadastral_code", "land_type", "land_type_group", "area_ha", "community", "status",
                   "canal_demand", "served_by", "terrain_state", "need", "history", "history_basis", "label"},
    "get_community": {"community", "parcels", "ha", "farm_ha", "ha_by_type", "terrain", "offtakes", "gates",
                      "secondary_canals", "label"} | PAGED,
    "find": {"query", "matches"} | PAGED,
    # the round schedule (canal rotation logic): a day, week or dekad as a slice of the season's schedule
    "schedule": {"scope", "period", "from", "to", "node", "rules", "rounds", "balance", "pump_limited", "turns",
                 "label"} | PAGED,
    # structured results of the planner and reviewer subagents (ids only)
    "submit_planner": {"candidates"},
    "submit_reviewer": {"objections"},
    # chat router (owner 2026-11-02): every agent the router asks replies in one fixed shape
    "submit_reply": {"agent", "status", "answer", "missing", "suggest"},
}
TOOLS_V6 = sorted(ALLOWED_FIELDS_V6)
READ_TOOLS = {"get_tree", "get_gate", "get_parcel", "get_community", "find"}
SCHEDULE_TOOLS = {"schedule"}
SUBMIT_TOOLS = {"submit_planner", "submit_reviewer", "submit_reply"}


def ha(v) -> float:
    return round(float(v or 0.0), 2)


def page(items: list, cursor=0, limit: int = PAGE_MAX):
    """items: [(id, value)] in display order -> ({id: value} for this page, cursor block)."""
    if limit > PAGE_MAX:
        raise ValueError("page larger than the cap")
    try:
        c = int(cursor or 0)
    except (TypeError, ValueError):
        raise T.ToolRefused("cursor must be a whole number")
    if c < 0 or (items and c >= len(items)):
        raise T.ToolRefused("cursor out of range")
    part = items[c:c + limit]
    nxt = c + limit if c + limit < len(items) else None
    return dict(part), {"total": len(items), "returned": len(part), "next": nxt}


def example_ids(nodes):
    """Representative ids of the network in use, so tests and the payload sample never hard-code one network's ids:
    the first stage I gate with direct outlets, the first stage II gate, the largest gate with direct outlets, a direct
    outlet in the middle of its secondary, a secondary gate with its tertiary, and the first main-canal pump station."""
    nd = nodes.set_index("node", drop=False).rename_axis(None)
    ids = set(nd.node)
    mg = nd[nd.kind == "main_gate"].sort_values(["stage", "chainage_m", "node"])
    has_d = lambda m: any(i.startswith(f"{m}-S1-D") for i in ids)                   # noqa: E731
    first = next(m for m in mg.node if m.startswith("I-") and has_d(m))
    big = max((m for m in mg.node if has_d(m)), key=lambda m: (float(nd.at[m, "ha"]), m))
    num = lambda i: int(re.search(r"(\d+)$", i).group(1))                            # noqa: E731
    d = sorted((i for i in ids if i.startswith(f"{big}-S1-D")), key=num)
    sg = sorted((i for i in ids if re.fullmatch(rf"{re.escape(big)}-S1-G\d+", i)), key=num)
    pumps = nd[nd.kind == "main_pump"].sort_values(["stage", "chainage_m", "node"]).node
    return {"gate": first, "gate2": next(m for m in mg.node if m.startswith("II-")), "big": big, "big_s1": f"{big}-S1",
            "direct": d[len(d) // 2], "sec_gate": sg[len(sg) // 2], "tertiary": f"{sg[len(sg) // 2]}-T1",
            "pump": pumps.iloc[0] if len(pumps) else None}


# ---------------------------------------------------------------- data, read once under the read guard
class MapData:
    """Tree nodes, parcel assignment, land types v1.5, codes, plan-time need and the 2021-2024 need reference, with
    the files read recorded. Everything the v6 tools and the tree allocation use comes from here."""

    def __init__(self):
        with read_guard.ReadGuard() as g:
            nodes = pd.read_parquet(NV2 / "nodes.parquet")
            pn = pd.read_parquet(NV2 / "parcel_node.parquet", columns=[
                "internal_parcel_id", "item_id", "cls", "farm", "community", "area_ha", "status", "canal_demand",
                "main", "secondary", "unit", "outlet", "terrain_state"])
            hist = pd.read_parquet(LAND_TYPES, columns=["item_id", "green_days", "cycles", "cuts"])
            codes = pd.read_parquet(CODES, columns=["internal_parcel_id", "cadastre_code"])
            legend = json.loads((MAP / "layers.json").read_text(encoding="utf-8"))["legend"]
            need = pd.read_parquet(DEMAND, columns=["pid", "dekad_id", "days", "ltype", "area_ha", "stage_weight",
                                                    "ripening", "net_m3_full_exp", "bucket_group", "taw_mm", "p",
                                                    "etc_mm_exp", "net_mm_full_exp"])
            ref = pd.read_parquet(REFERENCE, columns=["pid", "year", "dekad_id", "net_m3_full_exp"])
            supply_cfg = json.loads(SUPPLY_CFG.read_text(encoding="utf-8"))
            im = A.NETWORK["id_map_v1"]
            idmap = pd.read_parquet(NV2 / im["file"], columns=["kind", "v1_label", im["id_column"]]).rename(
                columns={im["id_column"]: "v2_id"})
        self.files_read, self.violations = g.files, g.violations
        self.group = {c["key"]: grp["key"] for grp in legend["groups"] for c in grp["classes"]}
        self.titles = {c["key"]: c["title"] for grp in legend["groups"] for c in grp["classes"]}
        self.tree = A.build_tree(nodes)
        cum = {}                                 # path from the offtake: each level's path_m starts at its parent;
        for r in sorted(nodes.itertuples(), key=lambda r: len(r.node)):     # a parent id is a prefix of its children's
            own = 0.0 if r.kind == "main_canal" or pd.isna(r.path_m) else float(r.path_m)
            cum[r.node] = own + (cum.get(r.parent, 0.0) if r.kind not in ("main_canal", "main_gate", "main_pump") else 0.0)
        for r in nodes[nodes.kind == "tail_escape"].itertuples():           # the escape sits at the secondary's tail
            cum[r.node] = max((v for k, v in cum.items() if k.startswith(r.parent + "-")), default=0.0)
        stage_start = nodes[nodes.kind == "main_canal"].set_index("stage").upstream_m.fillna(0.0)
        nodes = nodes.assign(offtake_m=nodes.node.map(cum).fillna(0.0),
                             canal_m=nodes.chainage_m + nodes.stage.map(stage_start))
        self.canal_m = float(nodes[nodes.kind.isin(["main_gate", "main_pump"])].canal_m.max())   # last offtake
        main_of = nodes.node.str.extract(r"^((?:I|II)-[GP]\d{2})", expand=False)
        self.max_path = nodes.assign(main=main_of).groupby("main").offtake_m.max().to_dict()
        tail = np.where(nodes.kind.isin(["main_gate", "main_pump"]), nodes.canal_m / self.canal_m,
                        nodes.offtake_m / main_of.map(self.max_path).replace(0, np.nan))
        nodes = nodes.assign(tail_share=np.round(pd.Series(tail, index=nodes.index).fillna(0.0), 2))
        self.nodes = nodes.set_index("node", drop=False).rename_axis(None)
        self.children = nodes.dropna(subset=["parent"]).groupby("parent").node.apply(list).to_dict()
        self.offtakes = nodes[nodes.kind.isin(["main_gate", "main_pump"])].sort_values("canal_m").node.tolist()
        pn = pn.merge(codes.drop_duplicates("internal_parcel_id"), on="internal_parcel_id", how="left")
        self.parcels = pn.merge(hist, on="item_id", how="left").set_index("item_id", drop=False).rename_axis(None)
        self.by_code = self.parcels.dropna(subset=["cadastre_code"]).drop_duplicates("cadastre_code") \
            .set_index("cadastre_code").item_id.to_dict()
        # plan-time need (the "expected" columns only: no hindsight) on the parcels with canal demand
        served = pn[pn.canal_demand == "canal demand"][["internal_parcel_id", "item_id", "outlet"]]
        need = need.merge(served, left_on="pid", right_on="internal_parcel_id")
        self.need_by = need[need.net_m3_full_exp > 0].set_index(["item_id", "dekad_id"]).net_m3_full_exp
        self.days = need.groupby("dekad_id").days.first().astype(int).to_dict()
        self.rows = {p: A.demand_rows(g.rename(columns={"net_m3_full_exp": "net_m3"})) for p, g in need.groupby("dekad_id")}
        # supply v1.1 on the tree: P90 over 2021-2024 of the zone's gross need at the head, per calendar dekad
        eff = ca.CFG["water_chain"]["field_efficiency"]["field"]
        ref = ref[ref.year.isin(supply_cfg["years"]) & (ref.net_m3_full_exp > 0)].merge(served, left_on="pid",
                                                                                         right_on="internal_parcel_id")
        ref = ref.assign(head_m3=ref.net_m3_full_exp / eff * ref.outlet.map(self.tree.cost))
        per = ref.groupby(["year", "dekad_id"]).head_m3.sum().reset_index()
        p90 = per.assign(cal=per.dekad_id.str[5:]).groupby("cal").head_m3.quantile(supply_cfg["percentile"]).to_dict()
        shut = set(supply_cfg["shutdown_months"])
        self.normal_supply = {p: 0.0 if int(p[5:7]) in shut else float(p90.get(p[5:], 0.0)) for p in self.rows}
        self.supply_label = supply_cfg["label"]
        # standing constraints written on the v1 branches (I-B12) apply to the v2 offtake that took them over
        self.v1_to_v2 = idmap[idmap.kind.str.startswith("v1 branch")].set_index("v1_label").v2_id.to_dict()


def _breakdown(r) -> dict:
    """Per tree node: hectares by land-type group (v1.5) and the communities it serves."""
    com = [c for c in str(r.communities or "").split(";") if c]
    return {"ha_field_land": ha(r.ha_FIELD_CROP + r.ha_NO_CROP + r.ha_CROPLAND_UNCERTAIN),
            "ha_perennial": ha(r.ha_ORCHARD + r.ha_VINEYARD + r.ha_PERENNIAL), "ha_greenhouse": ha(r.ha_GREENHOUSE),
            "community": r.community, "communities": len(com)}


@lru_cache(maxsize=1)
def map_data() -> MapData:
    return MapData()


# ---------------------------------------------------------------- the v6 world
@dataclass
class World6(T.World):
    """Allocation on the tree. demand = rows per (outlet, crop group) per dekad; head_capacity_m3 = normal head supply
    (supply v1.1 on the tree). The store applies the v6 allowlists and the size cap."""
    maps: MapData = None

    def _store(self, tool, data):
        extra = set(data) - ALLOWED_FIELDS_V6[tool]
        if extra:
            raise T.ToolRefused(f"{tool} tried to return fields outside its allowlist: {sorted(extra)}")
        size = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
        if size > MAX_RESULT_BYTES:
            raise T.ToolRefused(f"{tool} result too large ({size} bytes); ask for a smaller page")
        rid = f"R{len(self.results) + 1}"
        while rid in self.results:                                  # audit I2: ids never repeat
            rid = f"R{int(rid[1:]) + 1}"
        self.results[rid] = data
        return {"rid": rid, "data": data}

    def planning_year(self) -> int:
        return int(self.as_of[:4])


def v6_world(ledger_path, as_of="2025-07-D3") -> World6:
    from wp_core.canal_ops_ledger import Ledger
    m = map_data()
    nd = m.nodes
    return World6(nd[nd.kind.isin(["main_gate", "main_pump"])], nd[nd.kind.isin(A.OUTLET_KINDS)], m.rows, m.days,
                  m.normal_supply, Ledger(ledger_path), as_of, maps=m)


# ---------------------------------------------------------------- allocation on the tree
def _supply(world, period, scenario):
    f = ca.CFG["supply"]["scenarios"].get(scenario)
    if f is None:
        raise T.ToolRefused("unknown scenario")
    return A.head_supply(world.head_capacity_m3[period] * f, world.days[period])      # (accepted, spill above 12 m3/s)


def _level(world, node):
    """The nodes to list: the main-canal offtakes, or the children of `node` (drill-down)."""
    if node in (None, "", "I", "II"):
        return [n for n in world.maps.offtakes if node in (None, "") or world.maps.nodes.at[n, "stage"] == node]
    r = _node(world, node)
    return [k for k in world.maps.children.get(r.node, []) if world.maps.nodes.at[k, "kind"] != "tail_escape"]


def _rows(world, ids, fields, sort_key, cursor):
    nd = world.maps.nodes
    rows = [(n, {**fields(n), **_breakdown(nd.loc[n])}) for n in ids]
    rows.sort(key=sort_key)
    return page(rows, cursor, ALLOC_PAGE)


def _pct(nt, n):
    v = nt.at[n, "pct"]
    return None if pd.isna(v) else round(float(v), 1)


def _zero_last(world, period):
    return world.ledger.zero_last(period) if world.ledger else set()


def deliverable(world: World6, period: str, scenario: str = "normal", agent: str = "main"):
    world._period(period)
    rows = world.demand[period]
    need = rows.net_m3 / rows.ltype.map(ca.CFG["water_chain"]["field_efficiency"]).fillna(0.6)
    supply, _ = _supply(world, period, scenario)
    return world._store("deliverable", {"scenario": scenario, "period": period, "supply_m3": round(supply),
                                        "need_m3": round(float(need.sum())),
                                        "head_need_m3": round(float((need * rows.outlet.map(world.maps.tree.cost)).sum())),
                                        "label": world.maps.supply_label})


def compare_rules(world: World6, period: str, scenario: str = "normal", node: str | None = None, cursor=0,
                  agent: str = "main"):
    """Rules A and B on the tree: totals, then the nodes of one level (offtakes, or a node's children), worst first."""
    world._period(period)
    (supply, spill), days, rows = _supply(world, period, scenario), world.days[period], world.demand[period]
    zl = _zero_last(world, period)
    key = f"rules:{period}:{scenario}"
    cached = world.sims.get(key)
    if cached and cached["zero_last"] == zl:              # drill-down pages reuse the same two plans
        plans, nts = cached["plans"], cached["nodes"]
    else:
        plans = {"A": A.rule_A(rows, supply, days, world.maps.tree, zl, spill),
                 "B": A.rule_B(rows, supply, days, world.maps.tree, spill)}
        nts = {k: p.node_table() for k, p in plans.items()}
        world.sims[key] = {"plans": plans, "nodes": nts, "period": period, "scenario": scenario, "zero_last": zl}
    ids = _level(world, node)
    part, cur = _rows(world, ids, lambda n: {"need_m3": round(float(nts["A"].at[n, "need_m3"])),
                                             "A_pct": _pct(nts["A"], n), "B_pct": _pct(nts["B"], n)},
                      lambda x: min(v for v in (x[1]["A_pct"], x[1]["B_pct"], 1e9) if v is not None), cursor)
    rules = {k: {"metrics": A.metrics(p, nts[k]), "balance_closes": A.balance(p, nts[k])["closes"]} for k, p in plans.items()}
    rules["C"] = {"status": ca.CFG["rules"]["C"]["when_missing"]}
    return world._store("compare_rules", {"period": period, "scenario": scenario, "rules": rules, "node": node or "offtakes",
                                          "nodes": part, "cursor": cur, "label": ca.CFG["labels"]["rule"]})


def _floors(world, node_floors, period):
    ok = {}
    for k, v in (node_floors or {}).items():
        if isinstance(k, str) and NODE_ID.match(k) and k in world.maps.nodes.index and 0 <= float(v) <= 1:
            ok[k] = float(v)
    ids = world.maps.v1_to_v2
    for c in world.ledger.constraints(period).values() if world.ledger else []:
        if c.get("kind") == "branch_floor":                 # standing constraints are injected by the server
            n = c["branch"] if c["branch"] in world.maps.nodes.index else ids.get(c["branch"])
            if n:
                ok[n] = max(ok.get(n, 0.0), float(c["value"]))
    return ok


def simulate_plan(world: World6, period: str, scenario: str = "normal", group_weights=None, node_floors=None,
                  all_floor: float = 0.0, node: str | None = None, cursor=0, agent: str = "planner"):
    world._period(period)
    gw = {k: float(v) for k, v in (group_weights or {}).items() if k in ca.GROUPS and 0 <= float(v) <= 5}
    nf = _floors(world, node_floors, period)
    af = min(max(float(all_floor or 0), 0.0), 0.5)
    key = (agent, period)                                           # audit M2: counted after the arguments pass
    world.budget[key] = world.budget.get(key, 0) + 1
    if world.budget[key] > ca.CFG["agent_budget"]["max_simulations_per_agent_per_dekad"]:
        raise T.ToolRefused("simulation budget for this period is used up")
    (supply, spill), days = _supply(world, period, scenario), world.days[period]
    plan = A.simulate_plan(world.demand[period], supply, days, world.maps.tree, gw, nf, af,
                           _zero_last(world, period), spill)
    nt = plan.node_table()
    errs = A.invariants(plan, nt)
    if errs:
        raise T.ToolRefused("plan failed invariants: " + "; ".join(errs))
    params = json.dumps({"p": period, "s": scenario, "gw": gw, "nf": nf, "af": af}, sort_keys=True)
    sid = "S" + hashlib.sha256(params.encode()).hexdigest()[:10]
    world.sims[sid] = {"plan": plan, "nodes": nt, "params": params, "period": period, "scenario": scenario}
    part, cur = _rows(world, _level(world, node), lambda n: _node_result(nt, n, world), _worst_first, cursor)
    b = A.balance(plan, nt)
    return world._store("simulate_plan", {
        "sim_run_id": sid, "applied": {"group_weights": gw, "node_floors": nf, "all_floor": af},   # the inputs used, citable
        "metrics": A.metrics(plan, nt), "floors_met": plan.floors_met, "notes": plan.notes,
        "balance": {k: b[k] for k in ("head_in_m3", "delivered_m3", "seepage_m3", "closes")},
        "rounds": {"per_dekad": CFG_ROUNDS, "section_order": RT.SECTION_ORDER, "clock_times": "use schedule"},
        "node": node or "offtakes", "nodes": part,
        "cursor": cur, "label": ca.CFG["labels"]["rule"]})


def _node_result(nt, n, world=None):
    out = {"need_m3": round(float(nt.at[n, "need_m3"])), "in_m3": round(float(nt.at[n, "in_m3"])), "pct": _pct(nt, n)}
    q = _queue_pos(world, n) if world is not None else None
    if q is not None:
        out["queue_pos"] = q                       # place in the tail-to-head queue of its canal (rounds and turns:
    return out                                     # use schedule)


def _queue_pos(world, n):
    st = _static(world)
    if n in st.offtakes:
        return st.order.index(n)
    c = st.outlet_child.get(n) or next((c for kids in st.children.values() for c in kids if c.id == n), None)
    return None if c is None else (c.pos if c.id == n else [o for o, _ in c.outlets].index(n))


def _worst_first(x):
    return x[1]["pct"] if x[1]["pct"] is not None else 1e9


def tail_risk(world: World6, period: str, sim_run_id: str, node: str | None = None, cursor=0, agent: str = "reviewer"):
    """Tail-end nodes of one level with a low share of need met in a simulation (flag: tail share >= 0.66, < 50 %)."""
    world._period(period)
    sim = world.sims.get(sim_run_id)
    if not sim or "plan" not in sim:
        raise T.ToolRefused("unknown simulation")
    nt, nd = sim["nodes"], world.maps.nodes
    ids = _level(world, node)
    rows = [(n, {"tail_share": float(nd.at[n, "tail_share"]), "pct": _pct(nt, n),
                 "flag": bool(nd.at[n, "tail_share"] >= 0.66 and (_pct(nt, n) if _pct(nt, n) is not None else 100) < 50)})
            for n in ids]
    flagged = sum(r["flag"] for _, r in rows)
    part, cur = _rows(world, ids, lambda n: dict(rows)[n], lambda x: (not x[1]["flag"], -x[1]["tail_share"]), cursor)
    return world._store("tail_risk", {"totals": {"nodes": len(rows), "flagged": flagged}, "node": node or "offtakes",
                                      "nodes": part, "cursor": cur})


def get_network(world: World6, cursor=0, agent: str = "main"):
    """The tree in totals, then the main-canal offtakes (paged) with their land-type and community breakdowns."""
    nd = world.maps.nodes
    counts = nd.kind.value_counts().to_dict()
    part, cur = _rows(world, world.maps.offtakes, lambda n: {"label": nd.at[n, "label"], "kind": nd.at[n, "kind"],
                                                              "ha": ha(nd.at[n, "ha"]),
                                                              "capacity_lps": float(nd.at[n, "capacity_lps"])},
                      lambda x: world.maps.offtakes.index(x[0]), cursor)
    return world._store("get_network", {"totals": {k: int(v) for k, v in counts.items()}, "offtakes": part,
                                        "cursor": cur, "label": SIM_LABEL})


get_history, get_constraints, save_proposal = T.get_history, T.get_constraints, T.save_proposal


def submit_planner(world: World6, sim_run_ids: list, agent: str = "planner"):
    """The planner's structured result: up to 3 candidate simulation ids, best first. Ids only, never numbers."""
    ids = [s for s in (sim_run_ids or [])][:3]
    if not ids or any(s not in world.sims or "plan" not in world.sims[s] for s in ids):
        raise T.ToolRefused("submit_planner takes 1-3 known simulation ids")
    return world._store("submit_planner", {"candidates": ids})


REVIEW_METRICS = {"tail pct", "worst outlet pct", "perennial pct", "sensitive field pct", "equity"}


def submit_reply(world: World6, status: str, answer: str = "", missing: str = "", suggest: str | None = None,
                 agent: str = "agronomy"):
    """An agent's structured reply to the chat router: answered / partly (what is missing, which agent could help) /
    outside_role. The answer passes the Checker's internal profile here already: numbers only as placeholders of tool
    results, ids and codes only as a tool returned them."""
    from wp_core import canal_ops_router as RO
    from wp_core.canal_ops_checker import check_internal
    try:
        r = RO.parse_reply({"status": status, "answer": answer, "missing": missing, "suggest": suggest})
    except RO.RouteRefused as e:
        raise T.ToolRefused(str(e)) from e
    mine = {k: v for k, v in world.results.items() if k not in getattr(world, "question_before", set())}   # audit I3
    if r["missing"] and not check_internal(r["missing"], {}).ok:                                    # audit C2
        raise T.ToolRefused("'missing' must be plain words: no figures, ids or codes")
    if r["answer"]:
        v = check_internal(r["answer"], mine)
        if not v.ok:
            raise T.ToolRefused("reply answer did not pass the checks: " + "; ".join(v.reasons[:3]))
    return world._store("submit_reply", {"agent": agent, **r})


def submit_reviewer(world: World6, objections: list, agent: str = "reviewer"):
    """The reviewer's structured result: up to 5 objections, each a tree node, a fixed metric name and the
    simulation id that shows it. No free text."""
    out = []
    for o in (objections or [])[:5]:
        if not isinstance(o, dict) or o.get("metric") not in REVIEW_METRICS or o.get("sim_run_id") not in world.sims:
            raise T.ToolRefused("each objection needs a known simulation id and one of the fixed metric names")
        _node(world, o.get("node"))
        out.append({"node": o["node"], "metric": o["metric"], "sim_run_id": o["sim_run_id"]})
    return world._store("submit_reviewer", {"objections": out})


# ---------------------------------------------------------------- read tools on the tree and the map
def _node(world, node_id):
    if not isinstance(node_id, str) or not NODE_ID.match(node_id.strip()):
        raise T.ToolRefused("unknown network id")
    nd = world.maps.nodes
    if node_id.strip() not in nd.index:
        raise T.ToolRefused("unknown network id")
    return nd.loc[node_id.strip()]


def _summary(r) -> dict:
    out = {"id": r.node, "label": r.label, "kind": r.kind, "level": int(r.level), "stage": r.stage, "section": r.section,
           "community": r.community, "parcels": int(r.n_parcels), "ha": ha(r.ha),
           "ha_by_type": {t: ha(r[f"ha_{t}"]) for t in TYPES if r[f"ha_{t}"] > 0},
           "terrain": {s: ha(r[c]) for s, c in TERRAIN.items() if r[c] > 0}}
    if pd.notna(r.capacity_lps):
        out["capacity_lps"] = float(r.capacity_lps)
    if r.terrain_state:
        out["terrain_state"] = r.terrain_state
    if r.pump:
        out.update(pump_units=int(r.pump_units), run_windows=r.run_windows, pump_basis=r.pump_basis)
    return out


def _parcels_below(world, node_id):
    p = world.maps.parcels
    return p[(p.outlet == node_id) | p.outlet.fillna("").str.startswith(node_id + "-")]


def get_tree(world: World6, node: str, cursor=0, agent: str = "main"):
    """A node of the tree: parent, children (paged), what lies below it and its head/tail position."""
    r = _node(world, node)
    kids = world.maps.children.get(r.node, [])
    nd = world.maps.nodes
    rows = [(k, {"label": nd.at[k, "label"], "kind": nd.at[k, "kind"], "ha": ha(nd.at[k, "ha"]),
                 **({"capacity_lps": float(nd.at[k, "capacity_lps"])} if pd.notna(nd.at[k, "capacity_lps"]) else {})})
            for k in kids]
    part, cur = page(rows, cursor)
    below = nd[nd.node.str.startswith(r.node + "-")] if r.kind != "main_canal" else nd[nd.stage == r.stage]
    if r.kind == "main_canal":
        pos = {"stage": r.stage}
    elif r.kind in ("main_gate", "main_pump"):
        pos = {"canal_km": round(r.canal_m / 1000, 2), "tail_share": round(r.canal_m / world.maps.canal_m, 2)}
    else:
        main = re.match(r"^(?:I|II)-[GP]\d{2}", r.node).group(0)
        mx = world.maps.max_path.get(main) or 0
        pos = {"km_from_offtake": round(r.offtake_m / 1000, 2), "tail_share": round(r.offtake_m / mx, 2) if mx else None}
    counts = below.kind.value_counts().to_dict()
    return world._store("get_tree", {"node": _summary(r), "parent": r.parent, "position": pos,
                                     "below": {k: int(v) for k, v in counts.items()}, "children": part, "cursor": cur,
                                     "label": SIM_LABEL})


def get_gate(world: World6, gate: str, period: str | None = None, cursor=0, agent: str = "main"):
    """A gate, pump or outlet: parcels served (paged), hectares by type, communities, plan-time need."""
    r = _node(world, gate)
    if r.kind not in GATE_KINDS:
        raise T.ToolRefused("not a gate, pump or outlet; use get_tree for canals")
    period = world._period(period or world.as_of)
    ps = _parcels_below(world, r.node).sort_values("area_ha", ascending=False)
    com = ps.groupby("community").area_ha.sum().sort_values(ascending=False)
    need = world.maps.need_by.reindex(pd.MultiIndex.from_arrays([ps.item_id, [period] * len(ps)])).fillna(0).sum()
    rows = [(i, {"type": c, "ha": ha(a)}) for i, c, a in zip(ps.item_id, ps.cls, ps.area_ha)]
    part, cur = page(rows, cursor)
    return world._store("get_gate", {"gate": _summary(r), "communities": {k: ha(v) for k, v in com.head(5).items()},
                                     "need": {"period": period, "need_m3": round(float(need)), "basis": NEED_BASIS},
                                     "parcels": part, "cursor": cur, "label": SIM_LABEL})


def get_parcel(world: World6, parcel: str, period: str | None = None, agent: str = "main"):
    """One parcel by parcel id (f + five digits) or cadastral code. Internal profile only."""
    key = str(parcel or "").strip()
    if CODE.match(key):
        key = world.maps.by_code.get(key, "")
    if not ITEM_ID.match(key) or key not in world.maps.parcels.index:
        raise T.ToolRefused("unknown parcel")
    period = world._period(period or world.as_of)
    p = world.maps.parcels.loc[key]
    year = world.planning_year()
    series = {k: json.loads(p[k]) if isinstance(p[k], str) else {} for k in ("green_days", "cycles", "cuts")}
    history = {y: {"green_days": series["green_days"].get(y), "crop_cycles": series["cycles"].get(y),
                   "cuts": series["cuts"].get(y)} for y in sorted(series["green_days"]) if int(y) < year}
    need = world.maps.need_by.get((key, period))
    return world._store("get_parcel", {
        "parcel": key, "cadastral_code": p.cadastre_code if isinstance(p.cadastre_code, str) else None,
        "land_type": p.cls, "land_type_group": world.maps.group.get(p.cls), "area_ha": ha(p.area_ha),
        "community": p.community, "status": p.status, "canal_demand": p.canal_demand,
        "served_by": {k: p[k] for k in ("main", "secondary", "unit", "outlet") if isinstance(p[k], str)},
        "terrain_state": p.terrain_state if isinstance(p.terrain_state, str) else None,
        "need": {"period": period, "need_m3": round(float(need or 0)), "basis": NEED_BASIS},
        "history": history, "history_basis": HISTORY_CUT, "label": SIM_LABEL})


def _community_name(world, name):
    q = str(name or "").strip().casefold()
    names = {n.casefold(): n for n in world.maps.parcels.community.dropna().unique()}
    if q not in names:
        raise T.ToolRefused("unknown community; use find")
    return names[q]


def get_community(world: World6, name: str, cursor=0, agent: str = "main"):
    """A community: parcels and hectares by land type, terrain, the offtakes serving it (paged), gate counts."""
    n = _community_name(world, name)
    p = world.maps.parcels[world.maps.parcels.community == n]
    farm = p[p.farm]
    nd = world.maps.nodes
    off = farm.dropna(subset=["main"]).groupby("main").area_ha.sum().sort_values(ascending=False)
    rows = [(k, {"label": nd.at[k, "label"], "ha_in_community": ha(v)}) for k, v in off.items()]
    part, cur = page(rows, cursor)
    here = nd[nd.community == n]
    terrain = farm.groupby("terrain_state").area_ha.sum()
    return world._store("get_community", {
        "community": n, "parcels": int(len(p)), "ha": ha(p.area_ha.sum()), "farm_ha": ha(farm.area_ha.sum()),
        "ha_by_type": {k: ha(v) for k, v in p.groupby("cls").area_ha.sum().sort_values(ascending=False).items()},
        "terrain": {k: ha(v) for k, v in terrain.items()}, "offtakes": part, "cursor": cur,
        "gates": int(here.kind.isin(GATE_KINDS).sum()), "secondary_canals": int((here.kind == "secondary_canal").sum()),
        "label": SIM_LABEL})


def find(world: World6, query: str, cursor=0, agent: str = "main"):
    """Find by network id or label, community name, or land type (offtakes with the most hectares of that type)."""
    q = str(query or "").strip()
    if not q or len(q) > 100:
        raise T.ToolRefused("query must be 1-100 characters")
    low = q.casefold()
    nd, maps = world.maps.nodes, world.maps
    rows = []
    t = next((k for k in TYPES if low in (k.casefold(), k.replace("_", " ").casefold(), maps.titles[k].casefold())), None)
    if t:
        top = nd[nd.kind.isin(["main_gate", "main_pump", "secondary_canal"])].sort_values(f"ha_{t}", ascending=False)
        rows = [(r.node, {"kind": r.kind, "label": r.label, "type_ha": ha(r[f"ha_{t}"])}) for _, r in top.iterrows()
                if r[f"ha_{t}"] > 0]
    else:
        names = sorted(n for n in maps.parcels.community.dropna().unique() if low in n.casefold())
        rows = [(n, {"kind": "community"}) for n in names]
        hit = nd[nd.node.str.casefold().str.startswith(low) | nd.label.str.casefold().str.contains(low, regex=False)]
        hit = hit.sort_values(["level", "node"])
        rows += [(r.node, {"kind": r.kind, "label": r.label}) for r in hit.itertuples()]
    if not rows:
        return world._store("find", {"query": q, "matches": {}, "cursor": {"total": 0, "returned": 0, "next": None}})
    part, cur = page(rows, cursor)
    return world._store("find", {"query": q, "matches": part, "cursor": cur})


# ---------------------------------------------------------------- the round schedule (canal rotation logic)
def _static(world):
    if getattr(world, "_rot_static", None) is None:
        world._rot_static = RT.Static(world.maps.tree)
    return world._rot_static


def _rule_a_rows(world, period, scenario):
    """The dekad's volumes under rule A (cached with compare_rules' plans when they exist)."""
    key = f"rules:{period}:{scenario}"
    if key in world.sims:
        return world.sims[key]["plans"]["A"].rows
    (supply, spill), days = _supply(world, period, scenario), world.days[period]
    return A.rule_A(world.demand[period], supply, days, world.maps.tree, _zero_last(world, period), spill).rows


def season_rounds(world: World6, until: str, scenario: str = "normal", sim_run_id: str | None = None) -> list:
    """Every round from the season's first dekad with water to `until`, with the rotation state carried through
    (the queue does not restart). Earlier dekads use rule A; the last dekad uses the simulation when one is given.
    The chain under rule A is cached per scenario, so a day, week or dekad answer is a slice of the same rows."""
    world._period(until)
    periods = [p for p in sorted(world.demand) if p[:4] == until[:4] and p <= until and world.head_capacity_m3[p] > 0]
    if not periods:
        return []
    chain = world.__dict__.setdefault("_rot_chain", {}).setdefault(scenario, [])   # [(period, rounds, state after)]
    st = _static(world)
    sim = world.sims.get(sim_run_id) if sim_run_id else None
    if sim is not None and sim.get("scenario") != scenario:
        raise T.ToolRefused("that simulation used another scenario")
    out = []
    for p in periods:
        last = p == until and sim is not None and sim.get("period") == p
        done = [c for c in chain if c[0] == p]
        if done and not last:
            out += done[0][1]
            continue
        state = (chain[-1][2] if chain and chain[-1][0] < p else RT.State()).copy()
        rows = sim["plan"].rows if last else _rule_a_rows(world, p, scenario)
        (supply, _), days = _supply(world, p, scenario), world.days[p]
        need_head = float((rows.need * rows.outlet.map(world.maps.tree.cost)).sum())
        rr = RT.run_dekad(st, state, p, rows, days, supply, need_head)
        if not last:
            chain.append((p, rr, state))
        out += rr
    return out


def _when(t):
    return t.strftime("%m-%d %H:%M") if t else None


def schedule(world: World6, period: str | None = None, scope: str = "dekad", date: str | None = None,
             node: str | None = None, scenario: str = "normal", sim_run_id: str | None = None, cursor=0,
             agent: str = "main"):
    """The round schedule for a dekad, or a day or week from `date` (YYYY-MM-DD): totals first (rounds with their
    stage II window and lead order, the balance with water in transit), then the turns at one level with clock
    times, l/s, m3 and who is next. node: none = main-canal offtakes; an offtake or secondary canal = its tertiary
    inlets and direct outlets; a secondary gate or tertiary canal = its farm outlets."""
    if scope not in ("day", "week", "dekad"):
        raise T.ToolRefused("scope is day, week or dekad")
    if scope == "dekad":
        if not period:
            raise T.ToolRefused("a dekad schedule needs period")
        world._period(period)
        t0 = RT.dekad_start(period)
        t1 = t0 + timedelta(days=world.days[period])
    else:
        try:
            d = datetime.strptime(str(date), "%Y-%m-%d")
        except ValueError:
            raise T.ToolRefused("date must be YYYY-MM-DD")
        k = 1 if d.day <= 10 else 2 if d.day <= 20 else 3
        period = f"{d:%Y-%m}-D{k}"
        world._period(period)
        t0 = d.replace(hour=RT.CFG["round"]["day_slot_start_h"])
        t1 = t0 + timedelta(days=1 if scope == "day" else 7)
    end_ok = RT.dekad_start(world.as_of) + timedelta(days=world.days[world.as_of])
    if t0 >= end_ok:
        raise T.ToolRefused("that date is after the planning date; no hindsight")
    t1 = min(t1, end_ok)
    last = max(p for p in world.demand if RT.dekad_start(p) < t1)
    rounds = season_rounds(world, last, scenario, sim_run_id)
    rounds = [r for r in rounds if r.start < t1 and r.start + timedelta(hours=r.slots * RT.SLOT_H) > t0]
    if node in (None, "", "I", "II"):
        level, parent = "main", None
    else:
        r = _node(world, node)
        if r.kind in ("main_gate", "main_pump", "secondary_canal"):
            level, parent = "secondary", node if r.kind == "secondary_canal" else f"{node}-S1"
        elif r.kind in ("secondary_gate", "secondary_pump", "tertiary_canal"):
            level, parent = "tertiary", node if r.kind == "tertiary_canal" else f"{node}-T1"
        else:
            raise T.ToolRefused("name a canal, an offtake or a secondary gate")
    turns = [t for t in RT.slice_turns(rounds, t0, t1) if t["level"] == level and (parent is None or t["parent"] == parent)]
    turns.sort(key=lambda t: (t["open"], t["unit"]))
    rows = [(f"{t['unit']}_{i}", {"unit": t["unit"], "open": _when(t["open"]), "close": _when(t["close"]),
                                  "lps": t["lps"], "m3": t["m3"], "queue_pos": t["queue_pos"], "next": t.get("next"),
                                  "next_open": _when(t.get("next_open")),
                                  **({"night_h": t["night_h"]} if t["night_h"] else {}),       # rows stay small
                                  **({"mode": t["mode"]} if t["mode"] != "rotating" else {}),
                                  **({"flags": t["flags"]} if t["flags"] else {})})
            for i, t in enumerate(turns)]
    part, cur = page(rows, cursor, SCHEDULE_PAGE) if rows else ({}, {"total": 0, "returned": 0, "next": None})
    # the lead order is the same every round (rules.section_order): stage II, lower stage I, upper stage I
    rinfo = {f"{r.period}{r.label}": {"start": _when(r.start), "days": round(r.slots * RT.SLOT_H / 24, 1),
                                      "stage_II_window_days": round(r.stage2_window_slots * RT.SLOT_H / 24, 1),
                                      "due": len(r.outlet_due), "missed": len(r.flags["missed"]),
                                      "skipped": len(r.flags["skipped"]), "carry_over": len(r.flags["carry_over"]),
                                      "day_pumping": len(r.flags["day_pumping"])} for r in rounds}
    bal = {"closes": all(r.balance["closes"] for r in rounds),
           "max_day_residual_pct": max((r.balance["max_day_residual_pct"] for r in rounds), default=0.0),
           "in_transit_start_m3": rounds[0].balance["in_transit_start_m3"] if rounds else 0,
           "secondary_fill_and_drain_m3": sum(r.balance["secondary_fill_and_drain_m3"] for r in rounds),
           "secondary_spill_m3": sum(r.balance["secondary_spill_m3"] for r in rounds),
           "not_taken_at_head_m3": sum(r.balance.get("not_taken_at_head_m3", 0) for r in rounds)}
    return world._store("schedule", {"scope": scope, "period": period, "from": _when(t0), "to": _when(t1),
                                     "node": node or "offtakes", **({"rules": ROTATION_RULES} if not cursor else {}),
                                     "rounds": rinfo, "balance": bal,
                                     "pump_limited": sorted({x for r in rounds for x in r.flags["pump_limited"]}),
                                     "turns": part, "cursor": cur, "label": RT.LABEL})


TOOLS = {f.__name__: f for f in (get_network, deliverable, compare_rules, simulate_plan, tail_risk, get_history,
                                 get_constraints, save_proposal, get_tree, get_gate, get_parcel, get_community, find,
                                 submit_planner, submit_reviewer, submit_reply, schedule)}
assert set(TOOLS) == set(ALLOWED_FIELDS_V6)


# ---------------------------------------------------------------- shared map context for every agent
MAP_CONTEXT = """Map knowledge (shared by all agents; figures always come from tools).
Area: the Lower Hrazdan observed zone, stages I and II: the parcels of the communities along the real Lower Hrazdan
canal. Never call it a command area. The network on it is synthetic (simulation), drawn by fixed rules.
Tree, top to bottom, with ids:
- main canal stages I and II (ids I, II);
(id patterns only, # = digits; never write an id a tool did not return)
- main-canal offtakes: gates <stage>-G## or pump stations <stage>-P##;
- one secondary canal per offtake: <offtake>-S#; its gates <secondary>-G# or pumps <secondary>-P#;
- a tertiary canal behind each secondary gate: <gate>-T#; its gates <tertiary>-G#;
- direct outlets on the secondary canal for parcels next to it: <secondary>-D#; a tail escape at the end: <secondary>-E.
Parcel ids are the letter f and five digits, only as a tool returned them; cadastral codes are internal only and
never leave the staff page; codes the owner types are removed before you see them, so ask for the parcel id instead.
Land types (v1.5, AI-interpreted from imagery, not field-verified): field land (field crops, no crop in 2025, 2025 use
not specified), perennial plantings (orchard, vineyard, type not specified), greenhouse, not farmland, unclassified,
not classifiable. Greenhouses and not-farmland have no canal demand. "Reached" by a canal is not "irrigated".
Terrain (terrain screening, synthetic): the canals follow the terrain, parcel borders and roads, never across farm land;
gravity, pumping or terrain uncertain (+-3 m) per tertiary unit or direct outlet. One pump station per lifted area
stands at the canal and lifts water into that area's own ditches; the gates below it are ordinary gates. Pumps are
whole units with night run windows; no head, energy or design figures exist.
Water sharing (compare_rules, simulate_plan, tail_risk) runs down this tree: totals first, then one level at a time
(offtakes, or the children of a node you name), each node with its land-type groups and largest community.
Tools: get_tree(node), get_gate(gate), get_community(name), find(query), get_parcel(parcel id or code; internal).
Every list holds at most 25 items (allocation lists 20); ask for the next page with cursor. Totals come first.
schedule(period or date, day | week | dekad, node) gives the rounds and turns with clock times and who is next;
its "rules" block holds the fixed rotation figures to cite.
"""
ROTATION_TEXT = """How this canal works (simulation rules for every agent):
The Lower Hrazdan main canal and the synthetic network below it work in turns. Each parent canal is sized for the
children that run together, not for all its children at once. So water moves in rounds at every level: main canal,
secondary canal, tertiary canal, farm outlet, parcel.
A round is half a dekad (5 or 5.5 days). In each round every unit that is due gets one turn, in a fixed queue from the
tail of the canal towards the head. In July, sensitive fields are due every round, other field crops and orchards
every second round, vineyards every third. A turn runs the full working stream (hand streams of 20 l/s) for fewer
hours. A gate is either closed or at or above its minimum opening. Pumps run whole units, at night first.
On the main canal the order is stage II, lower stage I, upper stage I. When supply is short, stage II gets its own
window and stage I waits.
When you plan or explain water delivery:
- Never suggest opening all gates at the same time, or any flow above a canal's capacity.
- Always explain in rounds and turns: what runs now, for how long, at how many l/s, who is next, and why (queue
  position, crop period, carry-over priority).
- Canal flows change only at 07:00 or 19:00 and take hours to travel. Inside the next two days only the emergency
  share-out rule applies.
- In shortage, keep streams and shorten turns. Perennials and sensitive crops are shortened, not skipped. Other green
  fields may skip one turn, in rotation. No unit gets zero two turns running.
- A missed turn goes to the front of its group next round. Call it carry-over priority, nothing else.
- Every schedule is a simulation: not a water order, not a real WUA schedule, not a legal ranking."""
MAP_CONTEXT = MAP_CONTEXT + """Use it: protect perennial-heavy and tail-end branches, name communities and gates, and keep every number a tool
placeholder.
""" + ROTATION_TEXT


# tool descriptions the model reads; part of the owner-approved payload (hashed in irrigation_assistant.payload_schema)
TOOL_TEXT = {
    "get_network": "The synthetic tree in totals, then the main-canal offtakes (paged).",
    "deliverable": "Supply and need at the head for a dekad and supply scenario (normal, short20, short40).",
    "compare_rules": "Sharing rules A and B down the tree for a dekad and scenario: totals, then one tree level "
                     "(offtakes, or the children of node), worst first.",
    "simulate_plan": "Simulate a plan on the tree: group_weights (0-5 per group), node_floors (0-1 share of need per "
                     "tree node), all_floor (0-0.5 minimum share for every outlet); node and cursor choose the level "
                     "shown. The result's applied block holds the weights and floors actually used: cite those.",
    "tail_risk": "Tail position and share of need per tree node for a simulation id.",
    "get_history": "Past dekads recorded before the planning date.",
    "get_constraints": "The owner's standing rules in force.",
    "save_proposal": "Save a simulated plan for the owner's review (never approves it).",
    "get_tree": "A node of the canal tree: parent, children, what lies below, head/tail position.",
    "get_gate": "A gate, pump or outlet: parcels served, hectares by land type, communities, need.",
    "get_parcel": "One parcel by parcel id (the letter f and five digits, as a tool returned it) or cadastral code "
                  "(internal page only).",
    "get_community": "A community: hectares by land type, terrain, offtakes serving it.",
    "find": "Find by network id or label, community name or land type.",
    "schedule": "The round schedule: scope day, week or dekad (period, or date YYYY-MM-DD); node = an offtake, "
                "secondary or tertiary canal for its turns; optional scenario and sim_run_id. Clock times, l/s, m3, "
                "who is next.",
    "submit_planner": "Planner: submit 1-3 simulation ids, best first.",
    "submit_reviewer": "Reviewer: submit up to 5 objections, each {node, metric, sim_run_id}; metric is one of "
                       + ", ".join(sorted(REVIEW_METRICS)) + ".",
    "submit_reply": "Your one reply to the router: status answered | partly | outside_role; answer (numbers only "
                    "as {{R<n>.<key>}} placeholders); missing (what is missing); suggest (the agent that could help).",
}
# the one repair turn after a failed check (fixed text; the Checker's reasons are filled in by code)
REPAIR_TEXT = ("Your answer was not shown: the checks found {reasons}. Rewrite the whole answer once, in the same "
               "language. Write every number only as a {{{{R<n>.<key>}}}} placeholder with a key path that exists in "
               "that result; ids only exactly as a tool returned them. If a figure is not in any tool result, say it "
               "is not available instead of writing it. Do not call new agents.")


def repair_prompt(reasons) -> str:
    return REPAIR_TEXT.format(reasons="; ".join(reasons[:6]))


def map_context() -> str:
    return MAP_CONTEXT


def rotation_text() -> str:
    return ROTATION_TEXT


def estimate_tokens(text: str) -> int:
    """Conservative estimate: one token per 3 UTF-8 bytes (English is about 4 characters per token)."""
    return -(-len(text.encode("utf-8")) // 3)


def payload_schema_v6() -> dict:
    """The v6 field schema for the part-5 payload review (not yet approved; the live chat does not use it)."""
    from wp_core.canal_ops_policy_v6 import AGENT_TOOLS_V6
    return {"fields": {t: sorted(f) for t, f in ALLOWED_FIELDS_V6.items()},
            "agents": {a: sorted(t) for a, t in AGENT_TOOLS_V6.items()},
            "limits": {"page_max": PAGE_MAX, "max_result_bytes": MAX_RESULT_BYTES,
                       "map_context_max_tokens": MAP_CONTEXT_MAX_TOKENS},
            "map_context_sha256": __import__("hashlib").sha256(MAP_CONTEXT.encode()).hexdigest()}

