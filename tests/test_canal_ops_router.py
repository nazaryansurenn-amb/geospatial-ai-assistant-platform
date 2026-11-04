"""Chat router with escalation (owner 2026-11-02): code-enforced limits, the fixed reply shape, the path text, the
"cannot answer" path and direct answers without any subagent. Fixture mode and the live adapter's hooks; no model call."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import irrigation_assistant as ia  # noqa: E402
from wp_core import canal_ops_router as RO  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402
from wp_core import canal_ops_tools_v6 as V  # noqa: E402
from wp_core.canal_ops_policy_v6 import AGENT_TOOLS_V6  # noqa: E402

NEEDS = [V.NV2 / "nodes.parquet", V.MAP / "layers.json", V.DEMAND, V.REFERENCE]
built = pytest.mark.skipif(not all(p.exists() for p in NEEDS), reason="network / map / demand not built")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return V.v6_world(tmp_path_factory.mktemp("router") / "ledger.sqlite", "2025-07-D3")


def live():
    pytest.importorskip("claude_agent_sdk")
    import canal_ops_live as L
    return L


def session(world, monkeypatch=None):
    L = live()
    return L, L.Session(world, ia.agent_definitions())


def run(coro):
    return asyncio.run(coro)


def pre(s, agent, tool, args=None):
    out = run(s.pre({"agent_type": agent, "tool_name": tool, "tool_input": args or {}}, "t", None))
    return out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


# ---------------------------------------------------------------- limits (RouterGuard)
def test_at_most_three_agents_per_question():
    g = RO.RouterGuard()
    for a in ("agronomy", "season_monitor", "report_writer"):
        assert g.delegate("main", a)[0]
    ok, why = g.delegate("main", "auditor")
    assert not ok and "at most 3" in why and g.asked == ["agronomy", "season_monitor", "report_writer"]


def test_no_agent_asked_twice():
    g = RO.RouterGuard()
    assert g.delegate("main", "agronomy")[0]
    ok, why = g.delegate("main", "agronomy")
    assert not ok and "already asked" in why


def test_agents_never_call_agents():
    g = RO.RouterGuard()
    for caller in ("planner", "agronomy", "reviewer", "public_qa"):
        ok, why = g.delegate(caller, "season_monitor")
        assert not ok and "only the router" in why
    assert g.delegate(None, "season_monitor")[0]                          # no agent type = the router (SDK convention)


def test_public_qa_and_unknown_agents_are_never_called():
    g = RO.RouterGuard()
    for a in ("public_qa", "intake", "watchdog", "staff_qa", "main", "", None):
        assert not g.delegate("main", a)[0]
    assert "public_qa" not in RO.ROUTABLE and "main" not in RO.ROUTABLE


def test_every_plan_ends_with_the_reviewer():
    g = RO.RouterGuard()
    assert g.delegate("main", "agronomy")[0] and g.delegate("main", "season_monitor")[0]
    ok, why = g.delegate("main", "planner")                               # no slot left for the reviewer
    assert not ok and "reviewer" in why
    g = RO.RouterGuard()
    g.delegate("main", "planner")
    assert not g.plan_reviewed()
    g.delegate("main", "reviewer")
    assert g.plan_reviewed()
    g = RO.RouterGuard()                                                  # a reviewer before the planner is not a review
    g.delegate("main", "reviewer")
    assert not g.delegate("main", "planner")[0]                          # audit C1: no review slot after it
    g = RO.RouterGuard()
    g.delegate("main", "reviewer"); g.delegate("main", "agronomy")
    assert not g.delegate("main", "planner")[0] and g.plan_reviewed()


def test_path_says_who_was_asked():
    g = RO.RouterGuard()
    assert g.path() == "Answered with my own tools."
    for a in ("agronomy", "planner", "reviewer"):
        g.delegate("main", a)
    assert g.path() == "Asked agronomy, then the planner; the reviewer checked."


# ---------------------------------------------------------------- the fixed reply shape
@pytest.mark.parametrize("bad", [
    {"status": "maybe"}, {"status": "answered"}, {"status": "partly", "answer": "x"},
    {"status": "outside_role", "answer": "x"}, {"status": "answered", "answer": "x", "suggest": "public_qa"},
    {"status": "answered", "answer": "x" * (RO.ANSWER_MAX + 1)}, "answered"])
def test_reply_shape_refuses_malformed(bad):
    with pytest.raises(RO.RouteRefused):
        RO.parse_reply(bad)


def test_reply_shape_parses_the_three_statuses():
    assert RO.parse_reply({"status": "answered", "answer": "ok"})["status"] == "answered"
    r = RO.parse_reply({"status": "partly", "answer": "half", "missing": "crop stage", "suggest": "season_monitor"})
    assert r["missing"] == "crop stage" and r["suggest"] == "season_monitor"
    assert RO.parse_reply({"status": "outside_role", "missing": "weekly data"})["answer"] == ""


def test_only_asked_agents_reply_once():
    g = RO.RouterGuard()
    with pytest.raises(RO.RouteRefused):
        g.reply("agronomy", {"status": "answered", "answer": "x"})
    g.delegate("main", "agronomy")
    g.reply("agronomy", {"status": "answered", "answer": "x"})
    with pytest.raises(RO.RouteRefused):
        g.reply("agronomy", {"status": "answered", "answer": "y"})


@built
def test_submit_reply_refuses_untraced_numbers_and_stores_the_shape(world):
    with pytest.raises(T.ToolRefused, match="checks"):
        V.submit_reply(world, "answered", "About 120 ha of orchards.", agent="agronomy")
    r = V.get_community(world, world.maps.parcels.community.dropna().iloc[0], agent="agronomy")["rid"]
    out = V.submit_reply(world, "partly", f"{{{{{r}.ha_by_type.ORCHARD}}}} ha orchards.", "crop stage", "season_monitor",
                         agent="agronomy")["data"]
    assert set(out) == V.ALLOWED_FIELDS_V6["submit_reply"] and out["status"] == "partly"


# ---------------------------------------------------------------- per-agent tools under delegation
def test_each_agent_keeps_its_own_tool_list():
    defs = ia.agent_definitions()
    for a in RO.ROUTABLE:
        assert defs[a]["tools"] == sorted(f"mcp__canal__{t}" for t in AGENT_TOOLS_V6[a])
        assert "submit_reply" in AGENT_TOOLS_V6[a]
    codes = {a for a, t in AGENT_TOOLS_V6.items() if "get_parcel" in t}
    assert codes == {"main", "agronomy", "auditor", "staff_qa"}
    assert not AGENT_TOOLS_V6["public_qa"]
    assert RO.LIVE_ROUTING_GUIDE in defs["main"]["prompt"] and "submit_reply" in defs["agronomy"]["prompt"]


@built
def test_live_hooks_keep_tool_lists_and_block_agent_to_agent(world):
    L, s = session(world)
    assert not pre(s, "planner", "mcp__canal__get_parcel", {"parcel": "f00003"})[0]       # no codes for the planner
    assert not pre(s, "report_writer", "mcp__canal__get_parcel", {"parcel": "f00003"})[0]
    assert pre(s, "agronomy", "mcp__canal__get_parcel", {"parcel": "f00003"})[0]
    assert not pre(s, "agronomy", "Agent", {"subagent_type": "season_monitor"})[0]        # agents never call agents
    assert not pre(s, None, "Agent", {"subagent_type": "public_qa"})[0]
    for a in ("agronomy", "season_monitor", "report_writer", "auditor"):                  # folded into the main agent
        ok, why = pre(s, None, "Agent", {"subagent_type": a})
        assert not ok
    assert pre(s, None, "Agent", {"subagent_type": "planner"})[0]
    assert pre(s, None, "Agent", {"subagent_type": "reviewer"})[0]
    assert not pre(s, None, "Agent", {"subagent_type": "planner"})[0]                     # a repeat


@built
def test_live_registers_only_routable_agents(world, monkeypatch):
    L, s = session(world)
    monkeypatch.setattr(L, "api_key", lambda: "test-key-not-used")
    o = s.options("test-key-not-used")
    assert set(o.agents) == set(RO.LIVE_ROUTABLE) == {"planner", "reviewer"} and "public_qa" not in o.agents
    assert "mcp__canal__submit_reply" in o.allowed_tools and not set(L.BUILTIN) & set(o.allowed_tools)


@built
def test_live_reply_shape_checked_in_the_hooks(world):
    L, s = session(world)
    assert not pre(s, "reviewer", "mcp__canal__submit_reply", {"status": "answered", "answer": "x"})[0]   # not asked
    pre(s, None, "Agent", {"subagent_type": "reviewer"})
    assert not pre(s, "reviewer", "mcp__canal__submit_reply", {"status": "maybe"})[0]
    ok, out = pre(s, "reviewer", "mcp__canal__submit_reply", {"status": "outside_role", "missing": "weekly data"})
    assert ok and out["hookSpecificOutput"]["updatedInput"]["agent"] == "reviewer"
    run(s.post({"agent_type": "reviewer", "tool_name": "mcp__canal__submit_reply",
                "tool_input": {"status": "outside_role", "missing": "weekly data"}, "tool_response": {"content": []}}, "t", None))
    assert s.guard.replies["reviewer"]["status"] == "outside_role"
    assert not pre(s, None, "mcp__canal__submit_reply", {"status": "answered", "answer": "x"})[0]  # the router answers itself


@built
def test_live_plan_without_reviewer_is_blocked_and_withheld(world):
    L, s = session(world)
    pre(s, None, "Agent", {"subagent_type": "planner"})
    blocked = run(s.stop({"hook_event_name": "Stop", "stop_hook_active": False}, "t", None))
    assert blocked.get("decision") == "block" and "reviewer" in blocked["reason"]
    out = L.finish(world, s, "Make a plan", "en", "Here is the plan.", 0.0, None)
    assert out["kind"] == "withheld" and "not reviewed" in out["answer"]


@built
def test_live_cannot_answer_names_what_is_missing(world):
    L, s = session(world)
    pre(s, None, "Agent", {"subagent_type": "reviewer"})
    run(s.post({"agent_type": "reviewer", "tool_name": "mcp__canal__submit_reply",
                "tool_input": {"status": "outside_role", "missing": "weekly field observations"}, "tool_response": {}}, "t", None))
    out = L.finish(world, s, "trend?", "en", "I think it is rising.", 0.0, None)            # a guess is never shown
    assert out["kind"] == "cannot_answer" and "weekly field observations" in out["answer"]
    assert out["what_i_did"][0] == s.guard.path("en")


# ---------------------------------------------------------------- fixture router: the ladder end to end
@built
def test_simple_questions_are_answered_directly_without_any_agent(world):
    pid = world.maps.parcels.index[0] if world.maps.parcels.index.name == "item_id" else "f00003"
    r = ia.answer(world, f"Which gate serves parcel {pid}?")
    assert r["kind"] == "answer" and r["route"] == [] and r["what_i_did"][0] == "Answered with my own tools."
    com = world.maps.parcels.community.dropna().iloc[0]
    r = ia.answer(world, f"How many hectares by land type in {com}?")
    assert r["kind"] == "answer" and r["route"] == [] and com in r["answer"]


@built
def test_plan_goes_to_the_planner_then_the_reviewer(world):
    r = ia.answer(world, "Make a plan for this dekad with 40% less water")
    assert r["route"] == ["planner", "reviewer"] and r["what_i_did"][0] == "Asked the planner; the reviewer checked."
    assert r["kind"] == "answer" and "Illustrative sharing rule" in r["answer"]


@built
def test_escalation_follows_the_suggested_agent_and_combines(world):
    com = world.maps.parcels.community.dropna().iloc[0]
    r = ia.answer(world, f"Which crops are under stress in {com}?")
    assert r["route"] == ["agronomy", "season_monitor"] and r["kind"] == "answer"
    assert "Not covered" in r["answer"] and len(r["route"]) <= RO.MAX_AGENTS


@built
def test_cannot_answer_path_says_so_plainly(world):
    r = ia.answer(world, "Show me the trend over the weeks")
    assert r["kind"] == "cannot_answer" and r["route"] == ["season_monitor"]
    assert r["answer"].startswith("I cannot answer") and "Missing:" in r["answer"]


# ---------------------------------------------------------------- the owner's languages (owner 2026-11-02, deviation 4)
def test_fixed_texts_exist_in_every_chat_language_without_banned_words_or_figures():
    import re
    from wp_core.canal_ops_checker import CFG
    g = RO.RouterGuard()
    for a in ("agronomy", "season_monitor"):
        g.delegate("main", a)
    for lang in ("en", "hy", "ru"):
        assert set(RO.TEXTS[lang]) == set(RO.TEXTS["en"])
        texts = [RO.text(k, lang) for k in ("not_reviewed", "failed_checks", "own_tools")]
        texts += [RO.cannot_answer(g, lang), RO.cannot_answer(RO.RouterGuard(), lang), g.path(lang)]
        for t in texts:
            assert not re.search(r"\d", t)
            for lg in ("en", "hy", "ru"):
                for w in CFG["wording_ban"][lg]:
                    assert not re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", t.lower()), (lang, w, t)
    for lang in ("hy", "ru"):                                              # every agent and step label is covered
        assert set(RO.ASKED_NAMES[lang]) == set(RO.ROUTABLE)
    assert RO.text("cannot", "de") == RO.text("cannot", "en")              # other languages fall back to English


def test_path_and_steps_in_armenian_and_russian():
    g = RO.RouterGuard()
    for a in ("agronomy", "planner", "reviewer"):
        g.delegate("main", a)
    assert g.path("hy") == "Հարցրի ագրոնոմին, հետո պլանավորողին, վերանայողը ստուգեց։"
    assert g.path("ru") == "Спросил агронома, затем планировщика; рецензент проверил."
    assert RO.RouterGuard().path("hy") == "Պատասխանեցի իմ գործիքներով։"
    assert RO.steps_in(["Simulated a plan", "something new"], "ru") == ["Смоделировал план", "something new"]


@built
def test_live_withheld_and_cannot_answer_in_the_owners_language(world):
    L, s = session(world)
    pre(s, None, "Agent", {"subagent_type": "planner"})
    out = L.finish(world, s, "Պլան տուր", "hy", "Ահա պլանը։", 0.0, None)
    assert out["answer"] == RO.text("not_reviewed", "hy") and out["what_i_did"][0] == "Հարցրի պլանավորողին։"
    L, s = session(world)
    pre(s, None, "Agent", {"subagent_type": "reviewer"})
    run(s.post({"agent_type": "reviewer", "tool_name": "mcp__canal__submit_reply",
                "tool_input": {"status": "outside_role", "missing": "недельные наблюдения"}, "tool_response": {}}, "t", None))
    out = L.finish(world, s, "тренд?", "ru", "Думаю, растёт.", 0.0, None)
    assert out["kind"] == "cannot_answer" and out["answer"].startswith("Я не могу ответить")
    assert "недельные наблюдения" in out["answer"]
    L, s = session(world)
    out = L.finish(world, s, "?", "hy", "Ջուրը փոխվում է 2 անգամ։", 0.0, None)              # untraced figure
    assert out["kind"] == "withheld" and out["answer"] == RO.text("failed_checks", "hy")


@built
def test_live_simulation_budget_is_per_question(world, monkeypatch):
    L = live()
    world.budget = {("planner", world.as_of): 99}                         # used up by earlier questions
    seen = {}

    async def ask(self, prompt):
        seen["budget"] = dict(self.world.budget)
        return "", 0.0, None
    monkeypatch.setattr(L.Session, "ask", ask)
    L.live_answer(world, "Make a plan", "en", ia.agent_definitions())
    assert seen["budget"] == {}


def test_g10_live_scope_and_scrubbed_dates():
    """G10 follow-up: the Checker sees only this question's tool results, and a date with a time is not a phone."""
    from wp_core.canal_ops_policy import scrub
    assert "2025-07-21" in scrub("Plan from 2025-07-21 07:00 for 48 hours")
    assert "[phone]" in scrub("call +374 91 123456")
    import canal_ops_live as L
    assert hasattr(L, "LIVE_LOCK")
    from wp_core.canal_ops_checker import check_internal
    old = {"R1": {"need_m3": 5}}
    assert check_internal("Need {{R1.need_m3}} m³.", old).ok
    assert not check_internal("Need {{R1.need_m3}} m³.", {}).ok              # an earlier question's result is not enough


@built
def test_live_simulations_count_to_the_calling_agent(world):
    """Owner approval A2 (2026-11-02): the reviewer's runs use the reviewer's budget, not the planner's."""
    L, s = session(world)
    pre(s, None, "Agent", {"subagent_type": "planner"})
    pre(s, None, "Agent", {"subagent_type": "reviewer"})
    for agent in ("planner", "reviewer"):
        ok, out = pre(s, agent, "mcp__canal__simulate_plan", {"period": world.as_of})
        assert ok and out["hookSpecificOutput"]["updatedInput"]["agent"] == agent


def test_cannot_answer_drops_figures_and_codes_from_agents_words():
    """Audit C2: the agents' 'missing' text passes the Checker before the owner sees it."""
    g = RO.RouterGuard()
    g.delegate("main", "season_monitor")
    g.replies["season_monitor"] = {"status": "outside_role", "answer": "", "suggest": None,
                                   "missing": "need 5000 m3 for 02-045-9012-0003"}
    out = RO.cannot_answer(g)
    assert "5000" not in out and "02-045" not in out and RO.text("no_tool") in out


def test_delegation_reads_subagent_type_only():
    assert RO.delegation_target({"agent": "planner"}) is None                 # audit M1
