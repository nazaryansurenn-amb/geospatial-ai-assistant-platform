"""Irrigation Assistant (canal_ops_sim_v1): tools, hook policy, scrubber, fixture runner and live-mode guard."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import irrigation_assistant as ia  # noqa: E402
from wp_core import canal_ops_tools as T  # noqa: E402
from wp_core.canal_ops_checker import check  # noqa: E402
from wp_core.canal_ops_policy import fixed_refusal, post_tool_use, pre_tool_use, scrub  # noqa: E402


@pytest.fixture
def world(tmp_path):
    return ia.toy_world(tmp_path / "ledger.sqlite", as_of="2025-07-D2")


# ---------------------------------------------------------------- tools
def test_no_hindsight_in_tools(world):
    with pytest.raises(T.ToolRefused):
        T.compare_rules(world, "2025-07-D3")
    with pytest.raises(T.ToolRefused):
        T.simulate_plan(world, "2025-07-D3")


def test_allowlist_refuses_extra_fields(world):
    with pytest.raises(T.ToolRefused):
        world._store("get_history", {"periods": [], "cadastre_code": "x"})


def test_simulation_budget(world):
    for _ in range(8):
        T.simulate_plan(world, "2025-07-D2", agent="planner")
    with pytest.raises(T.ToolRefused):
        T.simulate_plan(world, "2025-07-D2", agent="planner")


def test_save_proposal_needs_sim_id(world):
    with pytest.raises(T.ToolRefused):
        T.save_proposal(world, "S-made-up")


def test_standing_constraint_injected(world):
    world.ledger.add_constraint({"key": "B6_floor", "kind": "branch_floor", "branch": "B6", "value": 0.7,
                                 "valid_from": "2025-07"}, "owner")
    r = T.simulate_plan(world, "2025-07-D2", "short40", agent="planner")
    assert r["data"]["branches"]["B6"]["pct"] >= 69.9


def test_bad_parameters_are_dropped(world):
    r = T.simulate_plan(world, "2025-07-D2", group_weights={"other_green_field": 99, "made_up": 1},
                        branch_floors={"B99": 0.9}, agent="planner")
    assert r["data"]["metrics"]["need_met_pct"] > 0


def test_results_have_ids_for_placeholders(world):
    r = T.compare_rules(world, "2025-07-D2", "short20")
    v = check(f"Rule A {{{{{r['rid']}.rules.A.metrics.need_met_pct}}}} %.", world.results)
    assert v.ok


# ---------------------------------------------------------------- policy
@pytest.mark.parametrize("agent,tool,ok", [
    (None, "mcp__canal__compare_rules", True), (None, "mcp__canal__simulate_plan", False),
    ("planner", "mcp__canal__simulate_plan", True), ("reviewer", "mcp__canal__save_proposal", False),
    ("planner", "Bash", False), ("report_writer", "Read", False), (None, "Agent", True), ("planner", "Agent", False),
    ("made_up", "mcp__canal__get_network", False)])
def test_pre_tool_use(agent, tool, ok):
    assert pre_tool_use(agent, tool, {}, "2025-07-D2")[0] is ok


def test_pre_tool_use_hindsight_and_long_args():
    assert not pre_tool_use("planner", "mcp__canal__simulate_plan", {"period": "2025-08-D1"}, "2025-07-D2")[0]
    assert not pre_tool_use("planner", "mcp__canal__simulate_plan", {"x": "a" * 300}, "2025-07-D2")[0]


def test_post_tool_use_backstop():
    assert post_tool_use('{"pct": 50}')[0]
    assert not post_tool_use('{"code": "04-002-9199-0012"}')[0]


@pytest.mark.parametrize("raw,leak", [
    ("Arshak Petrosyan from 04-002-9199-0012 called +374 91 234567", ["Petrosyan", "Arshak", "0199", "234567"]),
    ("Գյուղի պարոն Արամ Գևորգյանը խնդրում է, հեռ. 093 12 34 56", ["Գևորգյան", "Արամ", "093"]),
    ("Звонил Карен Саркисян, aram@mail.am", ["Саркисян", "Карен", "aram@"])])
def test_scrub_removes_identifiers(raw, leak):
    out = scrub(raw)
    assert not any(x in out for x in leak)


def test_scrub_keeps_operational_numbers():
    assert scrub("section 3 needs 12 ha in dekad 2, B7 short 40 percent") == \
        "section 3 needs 12 ha in dekad 2, B7 short 40 percent"


@pytest.mark.parametrize("q,kind", [("please open gate 3", "order"), ("send this to the farmers", "farmers"),
                                    ("is this real data?", "real_data"), ("what head pressure is needed", "hydraulic"),
                                    ("բացեք փականը", "order"), ("откройте затвор", "order")])
def test_fixed_refusals(q, kind):
    r = fixed_refusal(q)
    assert r and r[0] == kind and r[2]


# ---------------------------------------------------------------- runner
def test_agent_definitions():
    defs = ia.agent_definitions()
    assert len(defs) == 12
    assert all(d["model_id"].startswith("claude-") for d in defs.values())
    assert all(t.startswith("mcp__canal__") for d in defs.values() for t in d["tools"])
    assert "simulate_plan" not in " ".join(defs["main"]["tools"])


def test_run_dekad_fixture_passes_checker(tmp_path):
    w = ia.toy_world(tmp_path / "l.sqlite", as_of="2025-07-D3")
    for p, s in (("2025-07-D1", "normal"), ("2025-07-D3", "short40")):
        out = ia.run_dekad(w, p, s)
        assert out["checker"] == [] and out["proposal"].startswith("P")
        assert "Illustrative sharing rule" in out["answer"]
        assert "{{" not in out["answer"]                                            # every placeholder was filled
    assert w.ledger.verify()
    assert len(w.ledger.events("proposal")) == 2 and not w.ledger.events("decision")   # nothing approved by the agent


def test_chat_answers(world):
    assert ia.answer(world, "Please open gate 3")["kind"] == "refusal"
    r = ia.answer(world, "compare the rules if supply drops 40%")
    assert r["kind"] == "answer" and r["checker"] == [] and "{{" not in r["answer"]
    assert ia.answer(world, "what is the weather in Paris")["kind"] == "not_enabled"


def test_live_mode_refused(world, monkeypatch):
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    with pytest.raises(PermissionError):
        ia.run_dekad(world, "2025-07-D2", mode="live")
    monkeypatch.setenv("LIVE_ENABLED", "1")
    monkeypatch.setattr(ia, "APPROVAL", Path(world.ledger.path).with_name("no_approval.json"))
    with pytest.raises(PermissionError):                  # no approved payload schema
        ia.run_dekad(world, "2025-07-D2", mode="live")


def test_payload_schema_hash_changes_with_allowlist(monkeypatch):
    h = ia.payload_schema_hash()
    monkeypatch.setitem(ia.V.ALLOWED_FIELDS_V6, "get_history", {"periods", "extra"})   # live uses the v6 fields
    assert ia.payload_schema_hash() != h
