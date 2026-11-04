"""Live chat adapter for the Irrigation Assistant (canal_ops_sim_v1) on the Claude Agent SDK.

Called only through irrigation_assistant.answer(..., mode="live"), after guard_live() has checked the owner switch,
the owner-approved payload schema and the installed SDK. Uses exactly the agent instructions and tool lists of
irrigation_assistant.agent_definitions(); tools are the plan v6 fixed-code functions in wp_core/canal_ops_tools_v6.py
(map read tools, water sharing on the tree, submit_planner / submit_reviewer), served in-process on a World6.
Control layer (fixed code, tested in scripts/canal_ops_sdk_spike.py):
  - dontAsk + explicit allowed tools; every built-in tool disallowed; no project settings loaded; spend cap per answer;
  - PreToolUse: v6 per-agent tool lists (missing agent_type = main agent), no dekads after the planning date,
    whole-number cursors;
  - PostToolUse: restricted-pattern backstop scan (internal profile: codes and parcel ids allowed, coordinates,
    internal parcel ids, paths and archive names not);
  - Stop is blocked once while a subagent is still running;
  - the final text goes through the Checker's internal profile (ids and codes only as a tool returned them).
Chat router (owner 2026-11-02): the main agent is the router and the only agent that delegates; it may ask the agents
of wp_core/canal_ops_router.ROUTABLE (public_qa is never registered). RouterGuard enforces in the hooks: at most 3
agents per question, none twice, agents never call agents, a plan always ends with the reviewer (Stop blocked, and the
answer withheld if not); every asked agent replies once through submit_reply (answered / partly / outside_role). When
nobody could answer, the fixed "cannot answer" text names what is missing.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from pathlib import Path

from claude_agent_sdk import (AgentDefinition, AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server, query, tool)

from wp_core import canal_ops_router as RO
from wp_core import canal_ops_tools as T
from wp_core import canal_ops_tools_v6 as V
from wp_core.canal_ops_checker import check_internal
from wp_core.canal_ops_policy_v6 import post_tool_use_v6, pre_tool_use_v6

ROOT = Path(__file__).resolve().parents[1]
BUDGET_USD = 0.60
LOG = ROOT / "data/analysis/canal_ops_sim_v1/live_withheld.jsonl"
MAX_TURNS = 14
REPAIR_MIN_USD = 0.08                      # a repair turn starts only when at least this much budget is left
BUILTIN = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit", "TodoWrite",
           "ToolSearch"]
LABELS = {"get_network": "Read the network", "deliverable": "Checked supply and need",
          "compare_rules": "Compared sharing rules", "simulate_plan": "Simulated a plan",
          "tail_risk": "Checked tail-end branches", "get_history": "Read past dekads",
          "get_constraints": "Read your standing rules", "save_proposal": "Saved a plan for your review (not approved)",
          "get_tree": "Read the canal tree", "get_gate": "Read a gate", "get_parcel": "Read a parcel",
          "get_community": "Read a community", "find": "Searched the map", "submit_planner": "The planner chose its plans",
          "submit_reviewer": "The reviewer gave its objections", "schedule": "Read the round schedule",
          "submit_reply": "An agent replied to the router",
          "Agent": "Asked a specialist agent", "Task": "Asked a specialist agent"}


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        if line.split("=", 1)[0].strip() == "ANTHROPIC_API_KEY":
            return line.split("=", 1)[1].strip()
    raise PermissionError("ANTHROPIC_API_KEY is not set in WORKING_PRODUCT/.env")


def _text(r):
    return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False, default=str)}]}


def _err(e):
    return {"content": [{"type": "text", "text": f"refused: {e}"}], "is_error": True}


def build_server(world: V.World6):
    """The v6 tools, wrapping the fixed-code functions. Arguments are validated by those functions."""
    def wrap(fn):
        async def call(args):
            try:
                return _text(fn(args))
            except (T.ToolRefused, KeyError, ValueError, TypeError) as e:
                return _err(e)
        return call

    def cur(a):
        return a.get("cursor", 0)

    tools = [
        tool("get_network", V.TOOL_TEXT["get_network"], {"cursor": int})(
            wrap(lambda a: V.get_network(world, cur(a)))),
        tool("deliverable", V.TOOL_TEXT["deliverable"],
             {"period": str, "scenario": str})(
            wrap(lambda a: V.deliverable(world, a["period"], a.get("scenario", "normal")))),
        tool("compare_rules", V.TOOL_TEXT["compare_rules"], {"period": str, "scenario": str, "node": str, "cursor": int})(
            wrap(lambda a: V.compare_rules(world, a["period"], a.get("scenario", "normal"), a.get("node"), cur(a)))),
        tool("simulate_plan", V.TOOL_TEXT["simulate_plan"],
             {"period": str, "scenario": str, "group_weights": dict, "node_floors": dict, "all_floor": float,
              "node": str, "cursor": int})(
            wrap(lambda a: V.simulate_plan(world, a["period"], a.get("scenario", "normal"), a.get("group_weights"),
                                           a.get("node_floors"), a.get("all_floor", 0.0), a.get("node"), cur(a),
                                           agent=a.get("agent") or "planner"))),   # the caller, set by the hook
        tool("tail_risk", V.TOOL_TEXT["tail_risk"],
             {"period": str, "sim_run_id": str, "node": str, "cursor": int})(
            wrap(lambda a: V.tail_risk(world, a["period"], a["sim_run_id"], a.get("node"), cur(a)))),
        tool("get_history", V.TOOL_TEXT["get_history"], {})(wrap(lambda a: V.get_history(world))),
        tool("get_constraints", V.TOOL_TEXT["get_constraints"], {})(wrap(lambda a: V.get_constraints(world))),
        tool("save_proposal", V.TOOL_TEXT["save_proposal"],
             {"sim_run_id": str})(wrap(lambda a: V.save_proposal(world, a["sim_run_id"]))),
        tool("get_tree", V.TOOL_TEXT["get_tree"],
             {"node": str, "cursor": int})(wrap(lambda a: V.get_tree(world, a["node"], cur(a)))),
        tool("get_gate", V.TOOL_TEXT["get_gate"],
             {"gate": str, "period": str, "cursor": int})(
            wrap(lambda a: V.get_gate(world, a["gate"], a.get("period"), cur(a)))),
        tool("get_parcel", V.TOOL_TEXT["get_parcel"],
             {"parcel": str, "period": str})(wrap(lambda a: V.get_parcel(world, a["parcel"], a.get("period")))),
        tool("get_community", V.TOOL_TEXT["get_community"],
             {"name": str, "cursor": int})(wrap(lambda a: V.get_community(world, a["name"], cur(a)))),
        tool("find", V.TOOL_TEXT["find"], {"query": str, "cursor": int})(
            wrap(lambda a: V.find(world, a["query"], cur(a)))),
        tool("schedule", V.TOOL_TEXT["schedule"],
             {"period": str, "scope": str, "date": str, "node": str, "scenario": str, "sim_run_id": str, "cursor": int})(
            wrap(lambda a: V.schedule(world, a.get("period"), a.get("scope", "dekad"), a.get("date"), a.get("node"),
                                      a.get("scenario", "normal"), a.get("sim_run_id"), cur(a)))),
        tool("submit_planner", V.TOOL_TEXT["submit_planner"], {"sim_run_ids": list})(
            wrap(lambda a: V.submit_planner(world, a["sim_run_ids"]))),
        tool("submit_reviewer", V.TOOL_TEXT["submit_reviewer"], {"objections": list})(
            wrap(lambda a: V.submit_reviewer(world, a["objections"]))),
        tool("submit_reply", V.TOOL_TEXT["submit_reply"],
             {"status": str, "answer": str, "missing": str, "suggest": str})(
            wrap(lambda a: V.submit_reply(world, a["status"], a.get("answer", ""), a.get("missing", ""),
                                          a.get("suggest") or None, agent=a.get("agent") or "unknown"))),
    ]
    return create_sdk_mcp_server("canal", tools=tools)


class Session:
    def __init__(self, world: T.World, defs: dict):
        self.world, self.defs = world, defs
        self.running, self.steps, self.blocked = set(), [], []
        self.guard = RO.RouterGuard(RO.LIVE_ROUTABLE)
        self.pending = set()
        self.session_id, self.repaired = None, False

    def _deny(self, why):
        self.blocked.append(why)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}

    async def pre(self, inp, tid, ctx):
        agent, args = inp.get("agent_type"), inp.get("tool_input") or {}
        name = inp.get("tool_name", "").split("__")[-1]
        if name in ("Agent", "Task"):                            # delegation: only the router, within the limits
            ok, why = self.guard.delegate(agent, RO.delegation_target(args))
            return {} if ok else self._deny(why)
        ok, why = pre_tool_use_v6(agent, inp.get("tool_name", ""), args, self.world.as_of)
        if not ok:
            return self._deny(why)
        if name == "submit_reply":                               # the fixed reply shape, from an agent the router asked
            if agent in (None, "main") or agent not in self.guard.asked or agent in self.guard.replies \
                    or agent in self.pending:
                return self._deny("only an agent the router asked replies, once")
            try:
                RO.parse_reply(args)
            except RO.RouteRefused as e:
                return self._deny(str(e))
            self.pending.add(agent)                             # audit I6: reserved before the tool runs
            self.steps.append(LABELS[name])
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "updatedInput": {**args, "agent": agent}}}
        if name == "simulate_plan":                              # each agent's simulations count to its own budget
            self.steps.append(LABELS[name])
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "updatedInput": {**args, "agent": agent or "main"}}}
        self.steps.append(LABELS.get(name, "Used a tool"))
        return {}

    async def post(self, inp, tid, ctx):
        ok, hits = post_tool_use_v6(json.dumps(inp.get("tool_response"), default=str), "internal")
        if not ok:
            self.blocked.append("restricted content in a tool result: " + ", ".join(hits))
            return {"decision": "block", "reason": "Restricted content; stop and say the answer cannot be given."}
        name = inp.get("tool_name", "").split("__")[-1]
        resp = inp.get("tool_response")
        failed = isinstance(resp, dict) and resp.get("is_error")
        if name == "submit_reply" and not failed:                # recorded once the reply passed the tool's checks
            try:
                self.guard.reply(inp.get("agent_type"), inp.get("tool_input") or {})
            except RO.RouteRefused as e:
                self.blocked.append(str(e))
        return {}

    async def start(self, inp, tid, ctx):
        self.running.add(inp.get("agent_id"))
        return {}

    async def stop(self, inp, tid, ctx):
        if inp.get("hook_event_name") == "SubagentStop":
            self.running.discard(inp.get("agent_id"))
        elif self.running and not inp.get("stop_hook_active"):
            return {"decision": "block", "reason": "Wait for the specialist agent's result before answering."}
        elif not self.guard.plan_reviewed() and not inp.get("stop_hook_active"):
            return {"decision": "block", "reason": "A plan must go to the reviewer before you answer."}
        return {}

    def options(self, key: str, resume: str | None = None, budget: float = BUDGET_USD) -> ClaudeAgentOptions:
        d = self.defs
        subs = {n: AgentDefinition(description=d[n]["description"], prompt=d[n]["prompt"], tools=d[n]["tools"],
                                   model=d[n]["model"]) for n in RO.LIVE_ROUTABLE}       # planner and reviewer only
        allowed = sorted(set(d["main"]["tools"]).union(*(d[n]["tools"] for n in RO.LIVE_ROUTABLE)) | {"Agent", "Task"})
        return ClaudeAgentOptions(
            model=d["main"]["model_id"], system_prompt=d["main"]["prompt"], permission_mode="dontAsk",
            setting_sources=[], cwd=tempfile.mkdtemp(), mcp_servers={"canal": build_server(self.world)},
            allowed_tools=allowed, disallowed_tools=BUILTIN, agents=subs, max_turns=MAX_TURNS, max_budget_usd=budget, resume=resume,
            hooks={"PreToolUse": [HookMatcher(hooks=[self.pre])], "PostToolUse": [HookMatcher(hooks=[self.post])],
                   "SubagentStart": [HookMatcher(hooks=[self.start])], "SubagentStop": [HookMatcher(hooks=[self.stop])],
                   "Stop": [HookMatcher(hooks=[self.stop])]},
            env={"ANTHROPIC_API_KEY": api_key(), "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1"})

    async def ask(self, prompt: str, resume: str | None = None, budget: float = BUDGET_USD):
        last, cost, err = "", 0.0, None
        try:
            async for m in query(prompt=prompt, options=self.options(api_key(), resume, budget)):
                if isinstance(m, AssistantMessage):
                    txt = " ".join(b.text for b in m.content if isinstance(b, TextBlock)).strip()
                    if txt and not any(isinstance(b, ToolUseBlock) for b in m.content):
                        last = txt
                elif isinstance(m, ResultMessage):
                    cost = float(m.total_cost_usd or 0)
                    self.session_id = getattr(m, "session_id", None) or self.session_id
                    if m.result:
                        last = m.result.strip()
                    if getattr(m, "is_error", False):
                        err = getattr(m, "subtype", "error")
        except Exception as e:                                 # recorded; the Checker still decides what is shown
            err = f"{type(e).__name__}"
        return last, cost, err


LIVE_LOCK = threading.Lock()
LANG = {"hy": "Armenian", "en": "English", "ru": "Russian"}


def live_answer(world: V.World6, question: str, lang: str, defs: dict) -> dict:
    if not isinstance(world, V.World6):
        raise PermissionError("live chat runs on the plan v6 tree world only")
    with LIVE_LOCK:                                             # one live question at a time: result ids never collide
        world.budget = {}                                       # the simulation budget is per question, not per server run
        before = set(world.results)
        world.question_before = before                          # audit I3: agents' replies see this question only
        s = Session(world, defs)
        prompt = (f"{question}\n\n(Planning dekad: {world.as_of}. Reply in {LANG.get(lang, 'English')}. Write every "
                  f"number only as a {{{{R<n>.<key>}}}} placeholder from a tool result.)")
        text, cost, err = asyncio.run(s.ask(prompt))
        mine = {k: v for k, v in world.results.items() if k not in before}   # the Checker sees this question's results only
        reasons = repair_reasons(world, s, question, text, err, mine)
        if reasons and s.session_id and cost < BUDGET_USD - REPAIR_MIN_USD:   # one repair turn, same session, in budget
            s.repaired = True
            # a resumed session reports its whole cost so far (first turn included), and the SDK budget cap applies to
            # that total: so the cap stays BUDGET_USD and the reported cost replaces the first one (measured 2026-11-04)
            text2, cost2, err2 = asyncio.run(s.ask(V.repair_prompt(reasons), s.session_id, BUDGET_USD))
            cost = max(cost, cost2)
            if text2:
                text, err = text2, err2
            mine = {k: v for k, v in world.results.items() if k not in before}
        return finish(world, s, question, lang, text, cost, err, results=mine)


def repair_reasons(world, s, question, text, err, results) -> list:
    """The Checker's reasons when a finished, reviewed answer failed only the internal checks; else [] (no repair:
    an early stop, an unreviewed plan or nobody answering keep their fixed outcomes)."""
    if err or not text or not s.guard.plan_reviewed() or s.guard.nobody_answered():
        return []
    v = check_internal(text, results, echo=f"{question} {world.as_of}")
    return [] if v.ok else v.reasons


def finish(world, s, question, lang, text, cost, err, results=None) -> dict:
    """What the owner sees: the router's path, then the Checker's verdict; an unreviewed plan is withheld, and when no
    asked agent could answer, the fixed text says so and names what is missing."""
    steps = [s.guard.path(lang)] + RO.steps_in(dict.fromkeys(s.steps), lang)
    if getattr(s, "repaired", False):
        steps.append(RO.text("repaired", lang))
    if not s.guard.plan_reviewed():
        return {"kind": "withheld", "answer": RO.text("not_reviewed", lang), "what_i_did": steps,
                "checker": ["plan without the reviewer"], "route": s.guard.asked, "cost_usd": round(cost, 4), "live": True}
    if s.guard.nobody_answered():
        return {"kind": "cannot_answer", "answer": RO.cannot_answer(s.guard, lang), "what_i_did": steps,
                "route": s.guard.asked, "cost_usd": round(cost, 4), "live": True}
    v = check_internal(text, world.results if results is None else results, lang=lang, echo=f"{question} {world.as_of}")
    if not (v.ok and text):                                     # internal log for the owner; never shown on the page
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"as_of": world.as_of, "reasons": v.reasons, "error": err, "raw": text[:4000]},
                               ensure_ascii=False) + "\n")
    if err and v.ok and text:                                   # audit I5: never a half-finished answer
        v.ok = False
        v.reasons = [f"the run ended early: {err}"]
    if v.ok and text:
        return {"kind": "answer", "answer": v.text, "what_i_did": steps, "route": s.guard.asked,
                "cost_usd": round(cost, 4), "live": True}
    return {"kind": "withheld", "answer": RO.text("failed_checks", lang),
            "what_i_did": steps, "checker": v.reasons or ([err] if err else ["empty answer"]), "route": s.guard.asked,
            "cost_usd": round(cost, 4), "live": True}
