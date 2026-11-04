"""Live chat adapter (canal_ops_sim_v1): settings and guards, checked offline (no model call)."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import irrigation_assistant as ia  # noqa: E402

live = pytest.importorskip("canal_ops_live")
HAS_KEY = (Path(__file__).resolve().parents[1] / ".env").exists() and "ANTHROPIC_API_KEY" in \
    (Path(__file__).resolve().parents[1] / ".env").read_text(encoding="utf-8-sig")


@pytest.fixture
def world(tmp_path):
    return ia.toy_world(tmp_path / "l.sqlite", as_of="2025-07-D2")


@pytest.mark.skipif(not HAS_KEY, reason="no API key configured")
def test_options_lock_down(world):
    s = live.Session(world, ia.agent_definitions())
    o = s.options(live.api_key())
    assert o.permission_mode == "dontAsk" and o.setting_sources == [] and o.max_budget_usd <= 1.0
    assert {"Bash", "Read", "WebFetch", "ToolSearch"} <= set(o.disallowed_tools)
    assert all(t.startswith("mcp__canal__") or t in ("Agent", "Task") for t in o.allowed_tools)
    from wp_core.canal_ops_router import LIVE_ROUTABLE               # chat router: planner and reviewer only
    assert set(o.agents) == set(LIVE_ROUTABLE) and o.system_prompt == ia.agent_definitions()["main"]["prompt"]


def test_pre_hook_enforces_agent_lists(world):
    s = live.Session(world, ia.agent_definitions())
    deny = asyncio.run(s.pre({"agent_type": "reviewer", "tool_name": "mcp__canal__save_proposal", "tool_input": {}}, None, None))
    allow = asyncio.run(s.pre({"agent_type": "planner", "tool_name": "mcp__canal__simulate_plan",
                               "tool_input": {"period": "2025-07-D2"}}, None, None))
    late = asyncio.run(s.pre({"agent_type": "planner", "tool_name": "mcp__canal__simulate_plan",
                              "tool_input": {"period": "2025-07-D3"}}, None, None))
    main_sim = asyncio.run(s.pre({"tool_name": "mcp__canal__simulate_plan", "tool_input": {}}, None, None))
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert allow["hookSpecificOutput"]["updatedInput"]["agent"] == "planner"          # owner approval A2: own budget
    assert late["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert main_sim["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stop_blocked_while_subagent_runs(world):
    s = live.Session(world, ia.agent_definitions())
    asyncio.run(s.start({"agent_id": "a1"}, None, None))
    assert asyncio.run(s.stop({"hook_event_name": "Stop"}, None, None))["decision"] == "block"
    asyncio.run(s.stop({"hook_event_name": "SubagentStop", "agent_id": "a1"}, None, None))
    assert asyncio.run(s.stop({"hook_event_name": "Stop"}, None, None)) == {}


def test_refusals_never_reach_the_model(world, monkeypatch):
    monkeypatch.setattr(live, "live_answer", lambda *a, **k: pytest.fail("model must not be called"))
    monkeypatch.setenv("LIVE_ENABLED", "1")
    assert ia.answer(world, "please open gate 3", mode="live")["kind"] == "refusal"
