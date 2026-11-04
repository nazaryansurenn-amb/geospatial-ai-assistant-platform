"""Irrigation Assistant runner (canal_ops_sim_v1): agent definitions, the dekad pipeline and chat answers.

Modes
  fixture  (default) scripted planner/reviewer/main logic, but every step goes through the real tools
           (wp_core/canal_ops_tools.py), the ledger and the fixed-code Checker. No model call.
  recorded replays a saved run; refused if any hash (prompt, tools, data, model id) changed.
  live     Claude Agent SDK; refused unless LIVE_ENABLED=1, the SDK is installed and the owner-approved payload
           schema hash in config/canal_ops_payload_approval.json matches (scripts/canal_ops_live.py).

Plan v6 (owner, 2026-11-02, "Switch now"): the agents use the v6 registry (wp_core/canal_ops_tools_v6.py: map read
tools, water sharing on the tree, submit_planner / submit_reviewer) and v6 tool lists (wp_core/canal_ops_policy_v6.py);
their instructions carry the shared map context. That changes the payload schema hash, so live answers stay off until
the owner approves the new payload sample. The toy network keeps the v1 tools for the fixture tests.

  irrigation_assistant.py demo        -> fixture run of three July 2025 dekads on the toy network
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core import canal_allocation as ca  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402
from wp_core import canal_ops_router as RO  # noqa: E402
from wp_core import canal_ops_tools_v6 as V  # noqa: E402
from wp_core.canal_ops_checker import check, check_internal  # noqa: E402
from wp_core.canal_ops_ledger import Ledger  # noqa: E402
from wp_core.canal_ops_policy import fixed_refusal, scrub  # noqa: E402
from wp_core.canal_ops_policy_v6 import AGENT_TOOLS_V6  # noqa: E402
from wp_core.canal_toy_network import toy_network  # noqa: E402

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
APPROVAL = ROOT / "config/canal_ops_payload_approval.json"
RULES_TEXT = ("Every number comes from a tool result: write numbers only as {{R<n>.<key>}} placeholders, with the full "
              "key path as the result shows it (R2.gate.ha, not R2.ha). Never write a figure of your own (counts, shares, "
              "percentages, days, list positions in prose); if no tool returns it, say it is not available. Write "
              "network ids in full exactly as a tool returned them (I-G08-S1-G18, never G18). Quote labels word for word. "
              "Advice only: never orders, gate commands or messages to farmers. Say 'Lower Hrazdan observed zone', never "
              "'command area'. Label sharing as 'illustrative sharing rule, not legal priority'. No head, pumping energy "
              "or design. Never name data sources, people or coordinates. Network ids, parcel ids and cadastral codes only "
              "as a tool returned them; codes stay on this internal page. Use the map knowledge in your own job: tree "
              "position, land types and communities; protect perennial-heavy and tail-end canals.")
REPLY = ("Then call submit_reply once: status answered (your answer), partly (what you answered, what is missing, "
         "and the agent that could help) or outside_role (what is missing). Never call another agent.")
FINISH = {"planner": "Finish by calling submit_planner with your best one to three simulation ids, best first. " + REPLY,
          "reviewer": "Finish by calling submit_reviewer with your objections (tree node, metric, simulation id). " + REPLY,
          "main": "Finish with your answer to the owner. " + RO.LIVE_ROUTING_GUIDE}
FINISH.update({a: "Finish by calling submit_reply once: status answered (your answer), partly (what you answered, what "
                  "is missing, and the agent that could help) or outside_role (what is missing). Never call another agent."
               for a in RO.ROUTABLE if a not in FINISH})
MAP_USE = {
    "main": "You are the router: the only agent that talks to the owner and the only one that asks other agents.",
    "planner": "Use tree position and the v1.5 land types per gate when you set node floors and group weights.",
    "reviewer": "Use tree position and the v1.5 land types per gate: object when tail-end or perennial-heavy canals lose out.",
    "season_monitor": "Report stress and coverage per canal and community.",
    "agronomy": "Use the perennial and sensitive areas by gate and community.",
    "auditor": "Compare parcel labels with their neighbours on the same canal.",
    "season_prep": "Write community briefs by land type.",
    "report_writer": "Use names, communities and gates from the tools.",
    "staff_qa": "Use names, communities and gates from the tools.",
    "intake": "Use names, communities and gates from the tools.",
    "public_qa": "Published summaries only; you have no map tools.",
    "watchdog": "Flag drift per canal and community; you cannot block or approve.",
}

AGENTS = {
    "main": {"model": "sonnet", "description": "Irrigation Assistant router: answers directly or asks the one best agent, combines, narrates"},
    "planner": {"model": "sonnet", "description": "Canal planner: proposes up to 3 candidate plans that beat rules A/B/C or explain why not"},
    "reviewer": {"model": "sonnet", "description": "Equity and crop reviewer: objections tied to a metric and a simulation id"},
    "season_monitor": {"model": "sonnet", "description": "Weekly picture; stress, change and forecast kept apart"},
    "agronomy": {"model": "sonnet", "description": "Crop stage, demand bases, soil-water buffer, proposed floors"},
    "auditor": {"model": "sonnet", "description": "Data-mistake detectors, evidence, fix proposals as questions"},
    "season_prep": {"model": "sonnet", "description": "Nov-Mar community briefs and questions for the WUA"},
    "report_writer": {"model": "sonnet", "description": "Armenian/English sheets, briefs, Excel from placeholders"},
    "staff_qa": {"model": "sonnet", "description": "Short answers in Armenian, English, Russian"},
    "intake": {"model": "haiku", "description": "Scrubbed staff requests -> section totals"},
    "public_qa": {"model": "sonnet", "description": "Separate process; published summaries only"},
    "watchdog": {"model": "haiku", "description": "Advisory drift flags; cannot block or approve"},
}


def agent_definitions():
    """The 12 agents with full model ids, v6 tool lists (mcp__canal__*), the shared rules text, the shared map context
    for every agent with map tools, and "how this canal works" (canal rotation logic, section 11) for all 12."""
    out = {}
    for name, a in AGENTS.items():
        ctx = f" {V.map_context()}" if AGENT_TOOLS_V6[name] else f" {V.rotation_text()}"   # every agent: canal rules
        out[name] = {**a, "model_id": MODELS[a["model"]], "tools": sorted(f"mcp__canal__{t}" for t in AGENT_TOOLS_V6[name]),
                     "prompt": f"You are the {a['description']}. {MAP_USE[name]} {RULES_TEXT} "
                               f"{FINISH.get(name, 'Finish with your answer.')}{ctx}"}
    return out


# ---------------------------------------------------------------- world
def toy_world(ledger_path, as_of="2025-07-D3"):
    b, g, d = toy_network()
    periods = {"2025-07-D1": (1.00, 10), "2025-07-D2": (1.08, 10), "2025-07-D3": (1.15, 11)}
    demand = {p: d.assign(net_m3=d.net_m3 * f) for p, (f, _) in periods.items()}
    days = {p: n for p, (_, n) in periods.items()}
    eff = d.ltype.map(ca.CFG["water_chain"]["field_efficiency"]).fillna(0.6)
    head = {p: float((demand[p].net_m3 / eff).sum()) * 1.05 for p in periods}   # assumed head capacity ~ peak need
    return T.World(b, g, demand, days, head, Ledger(ledger_path), as_of)


# ---------------------------------------------------------------- fixture pipeline
def _objective(m):
    return (m["floors_met"], m["worst_gate_pct"] or 0, m["equity_min_over_mean"] or 0, m["need_met_pct"])


def _objective6(m):
    return (m["floors_met"], m["worst_outlet_pct"] or 0, m["equity_min_over_mean"] or 0, m["need_met_pct"])


def run_dekad_tree(world: V.World6, period: str, scenario: str = "normal", lang: str = "en"):
    """One dekad on the tree: code facts -> planner candidates -> reviewer objections -> revision -> proposal -> Checker
    (internal profile). Every step goes through the v6 tools."""
    world.budget = {k: v for k, v in world.budget.items() if k[1] != period}
    facts = V.deliverable(world, period, scenario, agent="main")
    rules = V.compare_rules(world, period, scenario, agent="main")
    steps = ["Checked supply and need for the dekad", "Compared sharing rules A and B down the tree"]
    base = max((rules["data"]["rules"][k]["metrics"] for k in ("A", "B")), key=_objective6)
    nd = world.maps.nodes
    tail = sorted(world.maps.offtakes, key=lambda n: -nd.at[n, "tail_share"])[:3]
    trials = [{}, {"group_weights": {"other_green_field": 0.6}}, {"node_floors": {n: 0.5 for n in tail}},
              {"node_floors": {n: 0.35 for n in world.maps.offtakes}}, {"all_floor": 0.3},
              {"all_floor": 0.4, "group_weights": {"other_green_field": 1.2}}]
    cands = [V.simulate_plan(world, period, scenario, agent="planner", **t) for t in trials]
    best = max(cands, key=lambda r: _objective6(r["data"]["metrics"]))
    V.submit_planner(world, [best["data"]["sim_run_id"]], agent="planner")
    steps.append(f"Planner tried {len(cands)} plans")
    risk = V.tail_risk(world, period, best["data"]["sim_run_id"], agent="reviewer")
    flagged = [n for n, r in risk["data"]["nodes"].items() if r["flag"]]
    objections, chosen = [], best
    if flagged:
        counter = V.simulate_plan(world, period, scenario, agent="reviewer", node_floors={n: 0.55 for n in flagged})
        objections.append({"branches": flagged, "metric": "tail pct", "sim_run_id": counter["data"]["sim_run_id"]})
        V.submit_reviewer(world, [{"node": n, "metric": "tail pct", "sim_run_id": counter["data"]["sim_run_id"]}
                                  for n in flagged[:5]], agent="reviewer")
        if _objective6(counter["data"]["metrics"]) >= _objective6(best["data"]["metrics"]):
            chosen = counter                                    # accepted only if it is not worse for the worst-off
    steps.append("Reviewer checked tail-end canals" + (" and objected" if objections else ""))
    beats = _objective6(chosen["data"]["metrics"]) >= _objective6(base)
    prop = V.save_proposal(world, chosen["data"]["sim_run_id"], author="main", agent="main")
    steps.append("Saved the plan for your review (not approved)")
    c, r, f = chosen["rid"], rules["rid"], facts["rid"]
    text = (f"Dekad {{{{{f}.period}}}}, scenario {{{{{f}.scenario}}}}. The proposed plan meets {{{{{c}.metrics.need_met_pct}}}} % "
            f"of the estimated need; the lowest outlet gets {{{{{c}.metrics.worst_outlet_pct}}}} %. For comparison, rule A "
            f"meets {{{{{r}.rules.A.metrics.need_met_pct}}}} % and rule B {{{{{r}.rules.B.metrics.need_met_pct}}}} %. "
            + ("It does at least as well as the best rule on the worst-off outlet. " if beats else
               f"Rule B gives the worst-off outlet more ({{{{{r}.rules.B.metrics.worst_outlet_pct}}}} %); this plan protects "
               "sensitive fields and perennials instead. Both are shown for your choice. ")
            + "Simulation. Illustrative sharing rule, not legal priority.")
    v = check_internal(text, world.results, lang=lang, profile="plan")
    if not v.ok:
        text = "Simulation. Illustrative sharing rule, not legal priority. The agent did not finish; showing the rule plan."
        v = check_internal(text, world.results, profile="plan")
    return {"period": period, "scenario": scenario, "proposal": prop["data"]["proposal"], "sim_run_id":
            chosen["data"]["sim_run_id"], "beats_rules": beats, "objections": objections, "what_i_did": steps,
            "answer": v.text, "checker": v.reasons, "metrics": chosen["data"]["metrics"]}


def run_dekad(world: T.World, period: str, scenario: str = "normal", mode: str = "fixture", lang: str = "en"):
    """One dekad: code facts -> planner candidates -> reviewer objections -> revision -> proposal -> Checker."""
    if mode != "fixture":
        guard_live(mode)
    if isinstance(world, V.World6):
        return run_dekad_tree(world, period, scenario, lang)
    world.budget = {k: v for k, v in world.budget.items() if k[1] != period}   # each owner-pressed run is a new decision
    steps = []
    facts = T.deliverable(world, period, scenario, agent="main")
    rules = T.compare_rules(world, period, scenario, agent="main")
    steps += ["Checked supply and need for the dekad", "Compared sharing rules A and B"]
    base = max((rules["data"]["rules"][k]["metrics"] for k in ("A", "B")), key=_objective)
    # planner (scripted): a few parameter sets inside its decision space
    tail = world.branches.assign(pos=world.branches.chainage_m + world.branches.upstream_m).nlargest(2, "pos").branch
    every = {b: 0.35 for b in world.branches.branch}           # a minimum share for every branch
    trials = [{}, {"group_weights": {"other_green_field": 0.6}},
              {"branch_floors": {b: 0.5 for b in tail}},
              {"branch_floors": every},
              {"branch_floors": every, "group_weights": {"other_green_field": 1.5}},
              {"all_floor": 0.3}, {"all_floor": 0.4, "group_weights": {"other_green_field": 1.2}}]
    cands = [T.simulate_plan(world, period, scenario, agent="planner", **t) for t in trials]
    best = max(cands, key=lambda r: _objective(r["data"]["metrics"]))
    steps.append(f"Planner tried {len(cands)} plans")
    # reviewer (scripted): tail-end and starved-group objections with a counter-run
    risk = T.tail_risk(world, period, best["data"]["sim_run_id"], agent="reviewer")
    flagged = [b for b, r in risk["data"]["branches"].items() if r["flag"]]
    objections, chosen = [], best
    if flagged:
        counter = T.simulate_plan(world, period, scenario, agent="reviewer", branch_floors={b: 0.55 for b in flagged})
        objections.append({"branches": flagged, "metric": "tail pct", "sim_run_id": counter["data"]["sim_run_id"]})
        if _objective(counter["data"]["metrics"]) >= _objective(best["data"]["metrics"]):
            chosen = counter                                    # accepted only if it is not worse for the worst-off
    steps.append("Reviewer checked tail-end branches" + (" and objected" if objections else ""))
    beats = _objective(chosen["data"]["metrics"]) >= _objective(base)
    prop = T.save_proposal(world, chosen["data"]["sim_run_id"], author="main", agent="main")
    steps.append("Saved the plan for your review (not approved)")
    c, r, f = chosen["rid"], rules["rid"], facts["rid"]
    text = (f"Dekad {{{{{f}.period}}}}, scenario {{{{{f}.scenario}}}}. The proposed plan meets {{{{{c}.metrics.need_met_pct}}}} % "
            f"of the estimated need; the lowest gate gets {{{{{c}.metrics.worst_gate_pct}}}} %. For comparison, rule A meets "
            f"{{{{{r}.rules.A.metrics.need_met_pct}}}} % and rule B {{{{{r}.rules.B.metrics.need_met_pct}}}} %. "
            + ("It does at least as well as the best rule on the worst-off gate. " if beats else
               f"Rule B gives the worst-off gate more ({{{{{r}.rules.B.metrics.worst_gate_pct}}}} %); this plan protects "
               "sensitive fields and perennials instead. Both are shown for your choice. ")
            + "Simulation. Illustrative sharing rule, not legal priority.")
    v = check(text, world.results, profile="plan", lang=lang)
    if not v.ok:
        text = "Simulation. Illustrative sharing rule, not legal priority. The agent did not finish; showing the rule plan."
        v = check(text, world.results, profile="plan")
    return {"period": period, "scenario": scenario, "proposal": prop["data"]["proposal"], "sim_run_id":
            chosen["data"]["sim_run_id"], "beats_rules": beats, "objections": objections, "what_i_did": steps,
            "answer": v.text, "checker": v.reasons, "metrics": chosen["data"]["metrics"]}


def answer(world: T.World, question: str, lang: str = "en", mode: str = "fixture"):
    """Chat answer. Fixed refusals first; fixture mode understands a few demo intents only."""
    q = scrub(question)
    ref = fixed_refusal(q, lang)
    if ref:
        return {"kind": "refusal", "answer": ref[1], "alternative": ref[2]}
    if mode != "fixture":
        guard_live(mode)
        if mode == "live":                                   # live chat: approved agents on the Agent SDK
            import canal_ops_live
            return canal_ops_live.live_answer(world, q, lang, agent_definitions())
    low = q.lower()
    scen = "short40" if "40" in low else "short20" if "20" in low else "normal"
    if isinstance(world, V.World6):
        return _answer_tree(world, low, scen, lang, q)
    if any(w in low for w in ("compare", "rule", "shortage", "cut", "less", "համեմատ", "կրճատ", "сравн", "сокращ")):
        r = T.compare_rules(world, world.as_of, scen, agent="main")["rid"]
        text = (f"Rule A meets {{{{{r}.rules.A.metrics.need_met_pct}}}} % of need and rule B {{{{{r}.rules.B.metrics.need_met_pct}}}} %; "
                f"the lowest gate gets {{{{{r}.rules.A.metrics.worst_gate_pct}}}} % under A and "
                f"{{{{{r}.rules.B.metrics.worst_gate_pct}}}} % under B. Illustrative sharing rule, not legal priority.")
    elif any(w in low for w in ("network", "branch", "ճյուղ", "ветк")):
        r = T.get_network(world, agent="main")["rid"]
        text = f"The illustrative network has {{{{{r}.gates_count}}}} gates on the branches shown on the map."
    else:
        return {"kind": "not_enabled", "answer": "Live answers are not enabled yet. Try one of the suggested questions.",
                "suggestions": ["Compare the sharing rules this dekad", "What if supply drops 40%?", "Show the network"]}
    v = check(text, world.results, profile="answer", lang=lang)
    return {"kind": "answer", "answer": v.text if v.ok else "The answer did not pass the checks.", "checker": v.reasons}


def _answer_tree(world: V.World6, low: str, scen: str, lang: str, question: str = ""):
    """Fixture chat on the tree with the router's escalation ladder (no model call): the router answers simple
    questions with its own tools, otherwise asks the one best agent; scripted agents reply through submit_reply and
    every delegation passes the same RouterGuard limits as live chat."""
    g = RO.RouterGuard()
    steps = []
    text, kind = None, "answer"
    pid = re.search(r"\bf\d{5}\b", low)
    com = _community_in(world, question or low)
    if pid and not any(w in low for w in AUDIT_WORDS):                     # 1. direct: which gate serves a parcel
        r = V.get_parcel(world, pid.group(0), agent="main")["rid"]
        text = (f"Parcel {{{{{r}.parcel}}}} ({{{{{r}.area_ha}}}} ha) is served by outlet {{{{{r}.served_by.outlet}}}} on "
                f"{{{{{r}.served_by.unit}}}}, secondary {{{{{r}.served_by.secondary}}}}, main-canal offtake "
                f"{{{{{r}.served_by.main}}}}.")
        steps.append("Read a parcel")
    elif com and any(w in low for w in HA_WORDS) and not any(w in low for w in CROP_WORDS + BRIEF_WORDS):
        r = V.get_community(world, com, agent="main")["rid"]                # 1. direct: hectares in a community
        top = sorted(world.results[r]["ha_by_type"].items(), key=lambda kv: -kv[1])[:3]
        parts = ", ".join(f"{{{{{r}.ha_by_type.{k}}}}} ha {k.lower().replace('_', ' ')}" for k, _ in top)
        text = f"{{{{{r}.community}}}}: {{{{{r}.ha}}}} ha in the zone, {{{{{r}.farm_ha}}}} ha farm land; largest: {parts}."
        steps.append("Read a community")
    elif any(w in low for w in PLAN_WORDS):                                   # 2. a plan: planner, then the reviewer
        for a in ("planner", "reviewer"):
            g.delegate("main", a)
        out = run_dekad_tree(world, world.as_of, scen, lang)
        p = V.submit_reply(world, "answered", "Candidate plans simulated; the best is saved for your review.",
                           agent="planner")
        g.reply("planner", world.results[p["rid"]])
        g.reply("reviewer", world.results[V.submit_reply(world, "answered", "Objections recorded with the plan.",
                                                          agent="reviewer")["rid"]])
        steps += out["what_i_did"]
        return {"kind": "answer", "answer": out["answer"], "checker": out["checker"], "route": g.asked,
                "what_i_did": [g.path(lang)] + RO.steps_in(steps, lang)}
    elif any(w in low for w in ("compare", "rule", "shortage", "cut", "less", "համեմատ", "կրճատ", "сравн", "сокращ")):
        r = V.compare_rules(world, world.as_of, scen, agent="main")["rid"]
        text = (f"Rule A meets {{{{{r}.rules.A.metrics.need_met_pct}}}} % of need and rule B {{{{{r}.rules.B.metrics.need_met_pct}}}} %; "
                f"the lowest outlet gets {{{{{r}.rules.A.metrics.worst_outlet_pct}}}} % under A and "
                f"{{{{{r}.rules.B.metrics.worst_outlet_pct}}}} % under B. Illustrative sharing rule, not legal priority.")
        steps.append("Compared sharing rules")
    elif any(w in low for w in ("network", "branch", "canal", "ճյուղ", "ցանց", "ветк", "сеть")):
        r = V.get_network(world, agent="main")["rid"]
        text = (f"The synthetic tree has {{{{{r}.totals.main_gate}}}} gates and {{{{{r}.totals.main_pump}}}} pump stations "
                f"on the main canal, {{{{{r}.totals.secondary_canal}}}} secondary canals and {{{{{r}.totals.tertiary_canal}}}} "
                "tertiary canals, shown on the map.")
        steps.append("Read the network")
    else:                                                                     # 2-4. ask the one best agent
        first = next((a for a, words in ROUTE_WORDS if any(w in low for w in words)), None)
        if first is None:
            return {"kind": "not_enabled", "answer": "Live answers are not enabled yet. Try one of the suggested questions.",
                    "suggestions": ["Compare the sharing rules this dekad", "What if supply drops 40%?", "Show the network"]}
        agent, answers = first, []
        while agent:
            ok, why = g.delegate("main", agent)
            if not ok:
                break
            rep = g.reply(agent, world.results[FIXTURE_AGENTS[agent](world, com)["rid"]])
            if rep["answer"]:
                answers.append(rep["answer"])
            agent = rep["suggest"] if rep["status"] != "answered" else None
        if g.nobody_answered() or not answers:
            text, kind = RO.cannot_answer(g, lang), "cannot_answer"
        else:
            miss = g.missing()
            text = " ".join(answers) + (f" Not covered: {'; '.join(miss)}." if miss else "")
    v = check_internal(text, world.results, lang=lang, echo=question)
    out = {"kind": kind if v.ok else "answer", "answer": v.text if v.ok else RO.text("failed_checks", lang),
           "checker": v.reasons, "route": g.asked, "what_i_did": [g.path(lang)] + RO.steps_in(steps, lang)}
    return out


def _community_in(world, text):
    t = (text or "").lower()
    names = sorted(world.maps.parcels.community.dropna().unique(), key=len, reverse=True)
    return next((n for n in names if n.lower() in t), None)


# fixture agents (scripted): each reads with its own tools and replies through submit_reply
def _fx_agronomy(world, com):
    if not com:
        return V.submit_reply(world, "partly", "Crop needs follow the land types of each canal.",
                              "a community or canal to look at", None, agent="agronomy")
    r = V.get_community(world, com, agent="agronomy")["rid"]
    return V.submit_reply(world, "partly", f"{{{{{r}.community}}}} has {{{{{r}.ha_by_type.ORCHARD}}}} ha orchards and "
                          f"{{{{{r}.ha_by_type.VINEYARD}}}} ha vineyards to protect in short supply.",
                          "observed crop stage and stress this week", "season_monitor", agent="agronomy")


def _fx_season_monitor(world, com):
    return V.submit_reply(world, "outside_role", "", "weekly field observations (not in this simulation's tools)", None,
                          agent="season_monitor")


def _fx_auditor(world, com):
    return V.submit_reply(world, "outside_role", "", "the data-mistake detectors are not available in chat", None,
                          agent="auditor")


def _fx_report_writer(world, com):
    r = V.get_network(world, agent="report_writer")["rid"]
    return V.submit_reply(world, "answered", f"Summary: {{{{{r}.totals.main_gate}}}} gates and {{{{{r}.totals.main_pump}}}} "
                          f"pump stations on the main canal feed {{{{{r}.totals.secondary_canal}}}} secondary canals.",
                          agent="report_writer")


def _fx_season_prep(world, com):
    if not com:
        return V.submit_reply(world, "outside_role", "", "which community the brief is for", None, agent="season_prep")
    r = V.get_community(world, com, agent="season_prep")["rid"]
    return V.submit_reply(world, "answered", f"Brief for {{{{{r}.community}}}}: {{{{{r}.farm_ha}}}} ha farm land, "
                          f"{{{{{r}.ha_by_type.FIELD_CROP}}}} ha field crops.", agent="season_prep")


FIXTURE_AGENTS = {"agronomy": _fx_agronomy, "season_monitor": _fx_season_monitor, "auditor": _fx_auditor,
                  "report_writer": _fx_report_writer, "season_prep": _fx_season_prep}
PLAN_WORDS = ("plan", "պլան", "план")
HA_WORDS = ("hectare", " ha", "how much land", "land type", "հեկտար", "гектар")
CROP_WORDS = ("crop", "stress", "perennial", "orchard", "vineyard", "մշակաբույս", "սթրես", "культур", "стресс")
AUDIT_WORDS = ("mistake", "wrong", "error", "doubt", "սխալ", "ошибк")
BRIEF_WORDS = ("brief", "pre-season", "preseason", "նախասեզոն", "бриф")
ROUTE_WORDS = [("auditor", AUDIT_WORDS), ("agronomy", CROP_WORDS),
               ("season_monitor", ("trend", "weekly", "over the weeks", "միտում", "тренд")),
               ("report_writer", ("summary", "report", "ամփոփ", "отчёт", "сводк")), ("season_prep", BRIEF_WORDS)]


# ---------------------------------------------------------------- modes
def payload_schema() -> dict:
    """What the owner approves: the v6 tool fields, the payload limits and the agent definitions (prompts, tools)."""
    schema = {t: sorted(V.ALLOWED_FIELDS_V6[t]) for t in V.ALLOWED_FIELDS_V6}
    schema["limits"] = {"page_max": V.PAGE_MAX, "alloc_page": V.ALLOC_PAGE, "max_result_bytes": V.MAX_RESULT_BYTES}
    schema["prompts"] = hashlib.sha256(json.dumps(agent_definitions(), sort_keys=True).encode()).hexdigest()
    schema["tool_text"] = hashlib.sha256(json.dumps({"tools": V.TOOL_TEXT, "repair": V.REPAIR_TEXT},
                                                    sort_keys=True).encode()).hexdigest()   # what the model reads too
    return schema


def payload_schema_hash():
    return hashlib.sha256(json.dumps(payload_schema(), sort_keys=True).encode()).hexdigest()


def guard_live(mode):
    if mode == "recorded":
        raise NotImplementedError("recorded mode needs a recorded live run (after payload approval)")
    if os.environ.get("LIVE_ENABLED") != "1":
        raise PermissionError("live mode is off (LIVE_ENABLED is not 1)")
    if not APPROVAL.exists() or json.loads(APPROVAL.read_text()).get("schema_hash") != payload_schema_hash():
        raise PermissionError("no owner approval for this exact payload schema")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as e:
        raise PermissionError("Claude Agent SDK is not installed") from e
    return True


def demo():
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "ledger.sqlite"
    w = toy_world(tmp)
    for p, s in (("2025-07-D1", "normal"), ("2025-07-D2", "short20"), ("2025-07-D3", "short40")):
        out = run_dekad(w, p, s)
        print(json.dumps({k: out[k] for k in ("period", "scenario", "beats_rules", "objections", "what_i_did", "answer")},
                         indent=1, ensure_ascii=False))
    print(json.dumps(answer(w, "Please open gate 3"), ensure_ascii=False))
    print(json.dumps(answer(w, "What if supply drops 40%? compare rules"), ensure_ascii=False))
    print("ledger chain ok:", w.ledger.verify())


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        sys.exit(__doc__)
