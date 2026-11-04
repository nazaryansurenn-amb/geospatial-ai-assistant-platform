"""Plan v6 parts 3 and 3b: agent read tools on the tree network v2 and the map, water sharing on the tree with
breakdowns per tree node, payload limits, the Checker's internal profile and the widened per-agent tool lists. Live
chat uses this v6 registry (owner, "Switch now") and stays refused until the owner approves the new payload."""
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import irrigation_assistant as ia  # noqa: E402
from wp_core import canal_ops_checker as C  # noqa: E402
from wp_core import canal_ops_policy as P1  # noqa: E402
from wp_core import canal_ops_policy_v6 as P6  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402
from wp_core import canal_ops_tools_v6 as V  # noqa: E402
from wp_core import read_guard as rg  # noqa: E402

NEEDS = [V.NV2 / "nodes.parquet", V.NV2 / "parcel_node.parquet", V.MAP / "layers.json", V.DEMAND, V.LAND_TYPES, V.CODES,
         V.REFERENCE]
pytestmark = pytest.mark.skipif(not all(p.exists() for p in NEEDS), reason="network v2 / map / demand not built")
AS_OF = "2025-07-D3"
# ids of the network in use (v2 or the terrain-routed v3), never one network's fixed ids
EX = V.example_ids(pd.read_parquet(V.NV2 / "nodes.parquet")) if (V.NV2 / "nodes.parquet").exists() else {}
G1, G2, BIG, DIRECT = EX.get("gate"), EX.get("gate2"), EX.get("big"), EX.get("direct")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return V.v6_world(tmp_path_factory.mktemp("l6") / "ledger.sqlite", AS_OF)


def size(r):
    return len(json.dumps(r["data"], ensure_ascii=False, default=str).encode("utf-8"))


def lists_in(d):
    """Every page (dict that sits next to a cursor) in a result."""
    if isinstance(d, dict):
        if "cursor" in d:
            yield from (v for k, v in d.items() if k != "cursor" and isinstance(v, dict) and k in PAGED_KEYS)
        for v in d.values():
            yield from lists_in(v)


PAGED_KEYS = {"nodes", "children", "parcels", "offtakes", "matches"}


# ---------------------------------------------------------------- live chat uses v6 and waits for the owner
def test_live_path_uses_v6_and_is_refused_until_the_owner_approves(monkeypatch, tmp_path):
    stale = tmp_path / "approval.json"                                  # an approval for another schema
    stale.write_text(json.dumps({"schema_hash": "0" * 64}), encoding="utf-8")
    monkeypatch.setattr(ia, "APPROVAL", stale)
    monkeypatch.setenv("LIVE_ENABLED", "1")
    with pytest.raises(PermissionError, match="approval"):
        ia.guard_live("live")
    defs = ia.agent_definitions()
    for agent, d in defs.items():
        assert d["tools"] == sorted(f"mcp__canal__{t}" for t in P6.AGENT_TOOLS_V6[agent])
        assert ("Map knowledge" in d["prompt"]) == bool(P6.AGENT_TOOLS_V6[agent])
    assert "submit_planner" in defs["planner"]["prompt"] and "submit_reviewer" in defs["reviewer"]["prompt"]
    assert "Finish by calling your submit tool" not in json.dumps(defs)
    schema = ia.payload_schema()
    assert {t: set(f) for t, f in schema.items() if t in V.ALLOWED_FIELDS_V6} == V.ALLOWED_FIELDS_V6


def test_live_adapter_serves_exactly_the_v6_tools(tmp_path, monkeypatch):
    live = pytest.importorskip("canal_ops_live")
    assert live.V is V and live.pre_tool_use_v6 is P6.pre_tool_use_v6
    seen, real = [], live.tool
    monkeypatch.setattr(live, "tool", lambda name, *a, **k: (seen.append(name), real(name, *a, **k))[1])
    srv = live.build_server(V.v6_world(tmp_path / "l.sqlite", AS_OF))
    assert srv["name"] == "canal" and sorted(seen) == sorted(V.TOOLS)
    toy = ia.toy_world(tmp_path / "toy.sqlite")
    with pytest.raises(PermissionError, match="tree"):
        live.live_answer(toy, "hi", "en", ia.agent_definitions())


def test_new_payload_sample_matches_the_current_schema_and_is_clean():
    out = ROOT / "data/analysis/canal_ops_sim_v1/payload_review_v6/summary.json"
    if not out.exists():
        pytest.skip("payload sample not made")
    s = json.loads(out.read_text(encoding="utf-8"))
    assert s["schema_hash"] == ia.payload_schema_hash()
    assert not any(s["restricted_hits"].values())
    assert s["internal_only"]["codes_outside_get_parcel"] == 0
    assert set(s["fields"]) == set(V.ALLOWED_FIELDS_V6)


def test_v1_registry_is_unchanged():
    assert set(T.ALLOWED_FIELDS) == {"get_network", "deliverable", "compare_rules", "simulate_plan", "get_history",
                                     "get_constraints", "tail_risk", "save_proposal"}
    assert not (V.READ_TOOLS & set(T.ALLOWED_FIELDS)) and not any(V.READ_TOOLS & t for t in P1.AGENT_TOOLS.values())


# ---------------------------------------------------------------- field allowlists
def test_every_tool_returns_only_allowlisted_fields(world):
    sim = V.simulate_plan(world, "2025-07-D2")["data"]["sim_run_id"]
    calls = {"get_network": (), "deliverable": ("2025-07-D2",), "compare_rules": ("2025-07-D2",),
             "simulate_plan": ("2025-07-D2",), "tail_risk": ("2025-07-D2", sim), "get_history": (),
             "get_constraints": (), "get_tree": (G1,), "get_gate": (G1,), "get_parcel": ("f00003",),
             "get_community": ("Արագած",), "find": ("vineyard",), "submit_planner": ([sim],),
             "submit_reviewer": ([{"node": G1, "metric": "tail pct", "sim_run_id": sim}],), "save_proposal": (sim,),
             "schedule": ("2025-07-D2",), "submit_reply": ("outside_role", "", "weekly data")}
    assert set(calls) == set(V.TOOLS)
    for name, args in calls.items():
        r = V.TOOLS[name](world, *args)
        assert set(r["data"]) <= V.ALLOWED_FIELDS_V6[name], name
        assert world.results[r["rid"]] is r["data"]


def test_store_refuses_extra_fields_and_oversized_results(world):
    with pytest.raises(T.ToolRefused, match="allowlist"):
        world._store("find", {"query": "x", "matches": {}, "cursor": {}, "lon": 44.3})
    with pytest.raises(T.ToolRefused, match="too large"):
        world._store("find", {"query": "x" * (V.MAX_RESULT_BYTES + 1), "matches": {}, "cursor": {}})


def test_no_coordinates_internal_ids_or_paths_in_any_result(world):
    for r in world.results.values():
        text = json.dumps(r, ensure_ascii=False, default=str)
        ok, hits = P6.post_tool_use_v6(text, "internal")
        assert ok, hits
        assert not re.search(r'"(lon|lat|x|y|elev|internal_parcel_id|geometry)"', text)


# ---------------------------------------------------------------- read tools
def test_get_tree_parent_children_and_position(world):
    d = V.get_tree(world, f"{G1}-S1")["data"]
    assert d["parent"] == G1 and d["node"]["kind"] == "secondary_canal"
    assert all(k.startswith(f"{G1}-S1-") for k in d["children"])
    assert d["below"].get("tail_escape") == 1
    g = V.get_tree(world, G1)["data"]
    assert 0 < g["position"]["tail_share"] <= 1 and g["position"]["canal_km"] > 0
    e = V.get_tree(world, f"{G1}-S1-E")["data"]
    assert e["position"]["tail_share"] == 1.0
    for bad in ("I-B12", "X-G04", "I-G99", "", None, f"{G1}; DROP"):
        with pytest.raises(T.ToolRefused):
            V.get_tree(world, bad)


def test_get_gate_reconciles_with_the_tree(world):
    for gate in (G1, G2, DIRECT):
        d = V.get_gate(world, gate, cursor=0)["data"]
        node = world.maps.nodes.loc[gate]
        below = V._parcels_below(world, gate)
        assert d["cursor"]["total"] == len(below) == node.n_parcels
        assert round(below.area_ha.sum(), 2) == pytest.approx(node.ha, abs=0.05)
        assert sum(d["communities"].values()) <= node.ha + 0.05
        assert d["need"]["period"] == AS_OF and "v1.4" in d["need"]["basis"]
    with pytest.raises(T.ToolRefused, match="not a gate"):
        V.get_gate(world, f"{G1}-S1")


def test_get_parcel_by_id_and_by_code(world):
    a = V.get_parcel(world, "f00003")["data"]
    assert C.RESTRICTED["cadastral_code"].fullmatch(a["cadastral_code"])
    b = V.get_parcel(world, a["cadastral_code"])["data"]
    assert b == a
    assert a["served_by"]["outlet"].startswith(a["served_by"]["main"] + "-")
    assert a["area_ha"] == round(a["area_ha"], 2) and a["land_type_group"] == "field_land"
    for bad in ("f99999", "parcel_0001d14c3d965abafe567424", "01-007-9999-9999", "x"):
        with pytest.raises(T.ToolRefused):
            V.get_parcel(world, bad)


def test_get_community_totals_reconcile_to_the_zone(world):
    names = world.maps.parcels.community.dropna().unique()
    total = sum(V.get_community(world, n)["data"]["ha"] for n in names)
    assert total == pytest.approx(10339.3, abs=0.5)
    d = V.get_community(world, "  արագած ")["data"]                 # trimmed, case-insensitive
    assert d["community"] == "Արագած" and d["farm_ha"] <= d["ha"]
    with pytest.raises(T.ToolRefused):
        V.get_community(world, "Nowhere")


def test_find_by_id_name_and_land_type(world):
    assert G1 in V.find(world, G1.lower())["data"]["matches"]
    assert V.find(world, "Արագ")["data"]["matches"]["Արագած"]["kind"] == "community"
    vin = V.find(world, "Vineyard")["data"]["matches"]
    has = [v["type_ha"] for v in vin.values()]
    assert has == sorted(has, reverse=True) and all(h > 0 for h in has)
    assert V.find(world, "nothing like this")["data"]["matches"] == {}
    with pytest.raises(T.ToolRefused):
        V.find(world, "x" * 101)


# ---------------------------------------------------------------- water sharing on the tree, breakdowns per node
def test_allocation_tools_return_tree_nodes_with_breakdowns(world):
    r = V.compare_rules(world, "2025-07-D2", "short40")["data"]
    assert {"A", "B", "C"} == set(r["rules"]) and r["rules"]["A"]["balance_closes"]          # totals first
    assert set(r["nodes"]) <= set(world.maps.offtakes) and r["node"] == "offtakes"
    row = next(iter(r["nodes"].values()))
    assert {"need_m3", "A_pct", "B_pct", "ha_field_land", "ha_perennial", "ha_greenhouse", "community", "communities"} <= set(row)
    worst = [min(x for x in (v["A_pct"], v["B_pct"]) if x is not None) for v in r["nodes"].values()]
    assert worst == sorted(worst)                                                             # worst-off first
    s = V.simulate_plan(world, "2025-07-D2", "short40", node=f"{BIG}-S1")["data"]
    assert all(k.startswith(f"{BIG}-S1-") for k in s["nodes"]) and s["balance"]["closes"]
    one = next(iter(s["nodes"].values()))
    assert {"need_m3", "in_m3", "pct", "queue_pos", "ha_perennial", "community"} <= set(one)
    assert s["rounds"]["section_order"] == ["stage II", "lower stage I", "upper stage I"]
    t = V.tail_risk(world, "2025-07-D2", s["sim_run_id"])["data"]
    assert t["totals"]["nodes"] == t["cursor"]["total"] == len(world.maps.offtakes)


def test_node_breakdown_matches_the_tree(world):
    s = V.simulate_plan(world, "2025-07-D2", "normal")["data"]
    nd = world.maps.nodes
    for n, row in s["nodes"].items():
        assert row["ha_perennial"] == pytest.approx(nd.at[n, "ha_ORCHARD"] + nd.at[n, "ha_VINEYARD"] + nd.at[n, "ha_PERENNIAL"], abs=0.01)
        assert row["community"] == nd.at[n, "community"]


def test_node_floors_and_bad_nodes(world):
    with pytest.raises(T.ToolRefused):
        V.simulate_plan(world, "2025-07-D2", "short40", node="I-B12")
    tail = max(world.maps.offtakes, key=lambda n: world.maps.nodes.at[n, "tail_share"])
    base = V.simulate_plan(world, "2025-07-D2", "short40")["data"]["sim_run_id"]
    floored = V.simulate_plan(world, "2025-07-D2", "short40", node_floors={tail: 0.6, "nonsense": 1})["data"]["sim_run_id"]
    pct = lambda sid: world.sims[sid]["nodes"].at[tail, "pct"]  # noqa: E731
    assert pct(floored) >= max(pct(base), 59.9)


def test_submit_tools_take_ids_only(world):
    sid = V.simulate_plan(world, "2025-07-D2", "normal")["data"]["sim_run_id"]
    with pytest.raises(T.ToolRefused):
        V.submit_planner(world, ["S000"])
    with pytest.raises(T.ToolRefused):
        V.submit_reviewer(world, [{"node": G1, "metric": "made up", "sim_run_id": sid}])
    with pytest.raises(T.ToolRefused):
        V.submit_reviewer(world, [{"node": "X-1", "metric": "tail pct", "sim_run_id": sid}])


# ---------------------------------------------------------------- payload limits (plan 2c)
def test_every_result_fits_the_size_cap_and_every_list_the_page_cap(world):
    world.results.clear()
    nd = world.maps.nodes
    for n, k in zip(nd.node, nd.kind):
        V.get_tree(world, n)
        if k in V.GATE_KINDS:
            V.get_gate(world, n)
    for c in world.maps.parcels.community.dropna().unique():
        V.get_community(world, c)
    for cur in (0, 20):
        V.compare_rules(world, "2025-07-D2", "short40", cursor=cur)
        V.get_network(world, cursor=cur) if cur < len(world.maps.offtakes) else None
    world.budget.clear()
    sid = V.simulate_plan(world, "2025-07-D2", "short40")["data"]["sim_run_id"]
    for n in nd.node[nd.kind.isin(["main_canal", "main_gate", "main_pump", "secondary_canal", "secondary_gate",
                                   "secondary_pump", "tertiary_canal"])]:
        kids = [k for k in world.maps.children.get(n, []) if nd.at[k, "kind"] != "tail_escape"]
        for cur in range(0, max(len(kids), 1), V.ALLOC_PAGE):
            V.compare_rules(world, "2025-07-D2", "short40", node=n, cursor=cur)
            V.tail_risk(world, "2025-07-D2", sid, node=n, cursor=cur)
    for r in world.results.values():
        assert len(json.dumps(r, ensure_ascii=False, default=str).encode("utf-8")) <= V.MAX_RESULT_BYTES
        for lst in lists_in(r):
            assert len(lst) <= V.PAGE_MAX
            if "nodes" in r and lst is r["nodes"]:
                assert len(lst) <= V.ALLOC_PAGE
    world.results.clear()


def test_cursor_walks_every_item_once(world):
    seen, cur = [], 0
    while cur is not None:
        d = V.get_tree(world, "I", cursor=cur)["data"]
        seen += list(d["children"])
        cur = d["cursor"]["next"]
    assert len(seen) == len(set(seen)) == len(world.maps.children["I"])
    for bad in (-1, 10_000, "two"):
        with pytest.raises(T.ToolRefused):
            V.get_tree(world, "I", cursor=bad)


def test_shared_map_context_is_small_and_figure_free():
    text = V.map_context()
    assert V.estimate_tokens(text) <= V.MAP_CONTEXT_MAX_TOKENS
    assert "Lower Hrazdan observed zone, stages I and II" in text and not re.search(r"command area", text.replace("Never call it a command area", ""))
    assert not re.search(r"\d[\d,.]*\s*(ha|m3|%)", text)              # data figures come from tools only
    assert "How this canal works" in text                              # the rotation rules (owner-approved text)


# ---------------------------------------------------------------- no hindsight
def test_no_dekad_after_the_planning_date(world):
    for call in (lambda: V.get_gate(world, G1, "2025-08-D1"), lambda: V.get_parcel(world, "f00003", "2025-08-D1"),
                 lambda: V.compare_rules(world, "2025-08-D1")):
        with pytest.raises(T.ToolRefused, match="hindsight"):
            call()


def test_parcel_history_stops_before_the_planning_year(world):
    src = json.loads(world.maps.parcels.loc["f00003", "green_days"])
    assert "2025" in src                                         # the source has the planning year ...
    h = V.get_parcel(world, "f00003")["data"]["history"]
    assert h and all(int(y) < 2025 for y in h)                   # ... the tool never shows it
    assert h["2024"]["green_days"] == src["2024"]


def test_parcel_need_uses_plan_time_values_only(world):
    d = pd.read_parquet(V.DEMAND, columns=["pid", "dekad_id", "net_m3_full_exp", "net_m3_full_act"])
    pid = world.maps.parcels.loc["f00003", "internal_parcel_id"]
    row = d[(d.pid == pid) & (d.dekad_id == "2025-07-D2")].iloc[0]
    got = V.get_parcel(world, "f00003", "2025-07-D2")["data"]["need"]["need_m3"]
    assert got == round(row.net_m3_full_exp)
    rows = world.demand["2025-07-D2"]
    served = world.maps.parcels.loc[world.maps.parcels.canal_demand == "canal demand", "internal_parcel_id"]
    exp = d[(d.dekad_id == "2025-07-D2") & d.pid.isin(served)]
    assert rows.net_m3.sum() == pytest.approx(exp.net_m3_full_exp.sum(), rel=1e-9)   # the tree uses plan-time values
    assert rows.net_m3.sum() != pytest.approx(exp.net_m3_full_act.sum(), rel=1e-6)


def test_normal_supply_uses_years_before_the_planning_year_only(world):
    years = json.loads(V.SUPPLY_CFG.read_text(encoding="utf-8"))["years"]
    assert max(years) < world.planning_year()
    assert world.head_capacity_m3["2025-03-D1"] == 0.0                     # March shutdown
    assert world.head_capacity_m3["2025-07-D2"] > 0


# ---------------------------------------------------------------- no SQL archive, nothing from the parent project
def test_map_data_read_under_the_guard_from_product_files_only(world):
    assert world.maps.violations == [] and len(world.maps.files_read) >= 6
    for f in world.maps.files_read:
        assert rg.problem(ROOT / f) is None and not re.search(r"archive|sql|\.bak$", f, re.I), f


def test_new_v6_sources_fall_under_the_archive_scan():
    import test_plan_v6_owner_decisions as owner
    names = {p.name for p in owner.SOURCES}
    assert {"canal_ops_tools_v6.py", "canal_ops_policy_v6.py", "canal_ops_checker.py"} <= names


# ---------------------------------------------------------------- per-agent tools
def test_tool_lists_follow_the_plan_table():
    t = P6.AGENT_TOOLS_V6
    assert t["public_qa"] == set()
    for agent in set(t) - {"public_qa"}:
        assert P6.MAP_READ <= t[agent], agent
        assert P1.AGENT_TOOLS[agent] <= t[agent], agent               # nothing taken away
    assert {a for a, s in t.items() if "get_parcel" in s} == {"main", "agronomy", "auditor", "staff_qa"}
    from wp_core.canal_ops_router import ROUTABLE
    assert {a for a, s in t.items() if s & {"submit_planner", "submit_reviewer"}} == {"planner", "reviewer"}
    assert {a for a, s in t.items() if "submit_reply" in s} == set(ROUTABLE)   # chat router: every agent it asks replies
    assert "submit_planner" in t["planner"] and "submit_reviewer" in t["reviewer"]


def test_pre_tool_use_v6_decisions():
    ok = lambda a, tool, args=None: P6.pre_tool_use_v6(a, f"mcp__canal__{tool}", args or {}, AS_OF)[0]  # noqa: E731
    assert ok(None, "get_parcel", {"parcel": "f00003"}) and ok("auditor", "get_parcel")
    assert not ok("public_qa", "find") and not ok("public_qa", "get_tree")
    assert not ok("planner", "get_parcel") and not ok("intake", "get_parcel")
    assert not ok("main", "get_gate", {"gate": G1, "period": "2025-08-D1"})
    assert not ok("main", "get_tree", {"node": "I", "cursor": -5}) and not ok("main", "find", {"query": "x" * 300})
    assert not ok("main", "Read") and not ok("planner", "Bash")


def test_post_tool_use_profiles():
    code = '{"cadastral_code": "04-006-9243-0002"}'
    assert P6.post_tool_use_v6(code, "internal")[0]
    assert not P6.post_tool_use_v6(code, "public")[0]
    for bad in ('{"p": "parcel_0001d14c3d965abafe567424"}', '{"lat": 40.17894}', '{"f": "C:\\\\x\\\\y.parquet"}'):
        assert not P6.post_tool_use_v6(bad, "internal")[0], bad


# ---------------------------------------------------------------- Checker: internal profile
def test_internal_checker_accepts_returned_ids_and_codes(world):
    world.results.clear()
    p = V.get_parcel(world, "f00003")
    g = V.get_gate(world, p["data"]["served_by"]["outlet"])
    code, outlet = p["data"]["cadastral_code"], p["data"]["served_by"]["outlet"]
    text = (f"Parcel f00003 (code {code}) is served by outlet {outlet}; it has {{{{{p['rid']}.area_ha}}}} ha and the "
            f"outlet serves {{{{{g['rid']}.gate.ha}}}} ha.")
    v = C.check_internal(text, world.results)
    assert v.ok, v.reasons
    assert re.search(r"has \d+\.\d{2} ha", v.text)                    # hectares with 2 decimals


def test_internal_checker_rejects_made_up_ids_and_codes(world):
    world.results.clear()
    V.get_parcel(world, "f00003")
    for fake in ("f12345", "01-007-9145-0001", "I-G09-S1-G3-T1-G2", "II-P04-S1-D7"):
        v = C.check_internal(f"Look at {fake} first.", world.results)
        assert not v.ok and any(fake in r for r in v.reasons), fake
    v = C.check_internal("Look at f12345 first.", world.results, echo="what about f12345?")    # the user typed it
    assert v.ok, v.reasons
    assert not C.check_internal("It needs 120 m3.", world.results).ok                        # numbers still traced
    assert not C.check_internal("Lat 40.17894 is near.", world.results).ok


def test_public_profile_still_blocks_codes_and_parcel_detail(world):
    world.results.clear()
    p = V.get_parcel(world, "f00003")["data"]
    for text in (f"Parcel {p['cadastral_code']} needs water.", "Parcel f00003 needs water.", "At 40.17894, 44.27929."):
        assert not C.check(text + " Illustrative network.", world.results, profile="public").ok, text
    assert C.check("The illustrative network covers stages I and II. Illustrative network.", {}, profile="public").ok


def test_internal_checker_g10_fixes():
    """G10 run 1 (2026-11-02): names a tool returned, years, version names and list numbers are not figures; a
    placeholder may skip one wrapper key only when the match is unique; bare figures and short ids stay withheld."""
    results = {"R1": {"gate": {"ha": 12.5, "ha_by_type": {"ORCHARD": 3.0}}, "label": "v1.5 land types"},
               "R2": {"scenario": "short40", "sim_run_id": "S3efe831b05",
                      "rounds": {"2025-07-D3A": {"days": 5}}, "turns": {"I-G08-S1-G18_3": {"m3": 10}},
                      "rules": {"A": {"supply_m3": 100, "pct": 50}, "B": {"supply_m3": 100, "pct": 60}}}}
    ok = ("Land types v1.5 (from 2021 to 2024 imagery), scenario short40, simulation S3efe831b05, round D3A:\n"
          "1. Gate area {{R1.ha}} ha, orchard {{R1.data.ha_by_type.ORCHARD}} ha.\n"
          "2. Supply {{R2.supply_m3}} m³.")
    v = C.check_internal(ok, results)
    assert v.ok, v.reasons
    assert "12.50" in v.text and "3.00" in v.text and "100" in v.text
    assert not C.check_internal("Rule share {{R2.pct}} %.", results).ok        # two different values: ambiguous
    assert not C.check_internal("Turn at G18 first.", results).ok               # short form of a tree id
    assert not C.check_internal("It serves 44 communities.", results).ok        # a figure of its own
    assert not C.check_internal("Need is met at 100%.", results).ok


def test_internal_checker_g10_run2_fixes():
    """G10 run 2: fixed scenario names (any case) and their share beside them, clock times a tool gave, "version 1.5",
    the +-3 m terrain band and the rule label in plural are accepted; other figures stay withheld."""
    results = {"R1": {"rules": {"change_times": ["07:00", "19:00"]}, "x": {"v": 5}}}
    ok = ("Short20 and short40 (a 20% shortage in short20). Flows change at 07:00 or 19:00. Land types, версии 1.5. "
          "Terrain uncertain within ±3 m. These are illustrative sharing rules, not legal priority. {{R1.v}}")
    v = C.check_internal(ok, results)
    assert v.ok, v.reasons
    assert not C.check_internal("Flows change at 08:00.", results).ok        # a time no tool gave
    assert not C.check_internal("A 30% shortage.", results).ok                # no scenario of that share
    assert not C.check_internal("This rule has legal priority.", results).ok


# ---------------------------------------------------------------- agentic AI review fixes (owner: "do", 2026-11-04)
def test_checker_reasons_name_the_untraced_figures_and_the_existing_keys():
    results = {"R12": {"nodes": {"II-P02": {"need_m3": 900, "in_m3": 450, "pct": 50.0}}}}
    v = C.check_internal("Gate II-P02 got 45 of its need: {{R12.data.offtakes.II-P02.pct}}.", results)
    assert not v.ok
    assert any(r.startswith("untraced number in text: 45") for r in v.reasons), v.reasons
    miss = next(r for r in v.reasons if r.startswith("placeholder without a result"))
    assert "existing keys: nodes.II-P02.pct" in miss
    assert "is not a result of this question" in C.key_hint(results, "R99", "x")


def test_simulate_plan_returns_the_inputs_it_applied(world):
    d = V.simulate_plan(world, "2025-07-D2", "short40", group_weights={"perennial": 9, "nonsense": 1},
                        all_floor=0.9)["data"]
    assert d["applied"]["all_floor"] == 0.5 and "nonsense" not in d["applied"]["group_weights"]
    assert "perennial" not in d["applied"]["group_weights"]          # 9 is outside 0-5, so it was not applied
    assert C.check_internal("Every outlet keeps {{R1.applied.all_floor}}.", {"R1": d}).ok


def test_prompts_and_tool_texts_carry_no_example_ids_and_are_hashed(monkeypatch):
    lit = re.compile(r"\b(?:I|II)-[GP]\d|\bf\d{5}\b")
    assert not lit.search(V.MAP_CONTEXT) and not any(lit.search(t) for t in V.TOOL_TEXT.values())
    before = ia.payload_schema_hash()
    monkeypatch.setitem(V.TOOL_TEXT, "find", V.TOOL_TEXT["find"] + " changed")
    assert ia.payload_schema_hash() != before                         # a tool description change needs re-approval


def test_one_repair_turn_after_a_failed_check(world, monkeypatch):
    import canal_ops_live as L
    calls = []

    class Fake(L.Session):
        async def ask(self, prompt, resume=None, budget=L.BUDGET_USD):
            calls.append((prompt, resume, budget))
            self.session_id = "s1"
            if len(calls) == 1:
                return "Gate got 45.", 0.10, None
            r = V.get_network(world)["rid"]
            return f"The tree in totals: {{{{{r}.data.totals.offtakes}}}}." if "offtakes" in str(
                world.results[r].get("totals", {})) else "Not available.", 0.15, None   # cumulative, like the SDK
    monkeypatch.setattr(L, "Session", Fake)
    out = L.live_answer(world, "How is the tree?", "en", ia.agent_definitions())
    assert len(calls) == 2 and calls[1][1] == "s1" and calls[1][2] == L.BUDGET_USD
    assert "untraced number in text: 45" in calls[1][0]
    assert out["kind"] == "answer" and out["cost_usd"] == 0.15                  # not 0.25: the first turn counted once
    assert out["what_i_did"][-1] == "Rewrote the answer once after the checks."
    calls.clear()

    class Bad(Fake):
        async def ask(self, prompt, resume=None, budget=L.BUDGET_USD):
            calls.append(prompt)
            self.session_id = "s1"
            return "Gate got 45.", 0.10, None
    monkeypatch.setattr(L, "Session", Bad)
    out = L.live_answer(world, "How is the tree?", "en", ia.agent_definitions())
    assert len(calls) == 2 and out["kind"] == "withheld"              # one repair only, then withheld
