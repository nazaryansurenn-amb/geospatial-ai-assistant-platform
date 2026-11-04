"""Plan v6 control layer (part 3): per-agent tool lists widened by the map read tools (plan section 3 table), and the
hook decisions for the v6 registry. Fixed code, never a model.

Live chat uses these lists (owner decision 2026-11-02, "Switch now"); wp_core/canal_ops_policy.py keeps the v1 lists
of the earlier approval. The planner and reviewer subagents end by calling submit_planner / submit_reviewer (ids only).

Map knowledge by agent (plan section 3): planner and reviewer use tree position and v1.5 types per gate; agronomy the
perennial and sensitive areas by gate and community; the season monitor stress and coverage per branch and community;
the data auditor parcel labels against their neighbours; season prep community briefs by land type; report writer,
Q&A and intake names, communities and gates. Parcel detail (get_parcel: parcel id or cadastral code) goes only to the
agents whose job needs single parcels. public_qa gets no map tools at all (plan 2c).
"""
from __future__ import annotations

from wp_core.canal_ops_checker import INTERNAL_RESTRICTED, RESTRICTED
from wp_core.canal_ops_policy import AGENT_TOOLS, BUILTIN, tool_name
from wp_core.canal_ops_router import ROUTABLE
from wp_core.canal_ops_tools_v6 import ALLOWED_FIELDS_V6

MAP_READ = {"get_tree", "get_gate", "get_community", "find"}
PARCEL_DETAIL = {"get_parcel"}
_WIDEN = {
    "main": MAP_READ | PARCEL_DETAIL,
    "planner": MAP_READ | {"submit_planner"},
    "reviewer": MAP_READ | {"submit_reviewer"},
    "season_monitor": MAP_READ,
    "agronomy": MAP_READ | PARCEL_DETAIL,
    "auditor": MAP_READ | PARCEL_DETAIL,
    "season_prep": MAP_READ,
    "report_writer": MAP_READ,
    "staff_qa": MAP_READ | PARCEL_DETAIL,
    "intake": MAP_READ,
    "watchdog": MAP_READ,
    "public_qa": set(),
}
AGENT_TOOLS_V6 = {a: set(AGENT_TOOLS[a]) | _WIDEN[a] | ({"schedule"} if _WIDEN[a] else set())
                  | ({"submit_reply"} if a in ROUTABLE else set()) for a in AGENT_TOOLS}   # chat router replies
assert all(t <= set(ALLOWED_FIELDS_V6) for t in AGENT_TOOLS_V6.values())
assert not AGENT_TOOLS_V6["public_qa"]


def pre_tool_use_v6(agent_type: str | None, tool: str, args: dict, as_of: str) -> tuple[bool, str]:
    """Allow or deny a v6 tool call. Missing agent_type means the main agent (SDK convention)."""
    agent = agent_type or "main"
    name = tool_name(tool)
    if name in BUILTIN and not (agent == "main" and name in ("Agent", "Task")):
        return False, "built-in tools are not available"
    if name in ("Agent", "Task"):
        return True, ""
    if name not in AGENT_TOOLS_V6.get(agent, set()):
        return False, f"{name} is not allowed for {agent}"
    period = args.get("period")
    if period and str(period) > as_of:
        return False, "no hindsight: period after the planning date"
    cursor = args.get("cursor", 0)
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        return False, "cursor must be a whole number"
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            return False, f"argument {k} too long"
    return True, ""


def post_tool_use_v6(result_text: str, profile: str = "internal") -> tuple[bool, list]:
    """Backstop scan of a tool result. Internal: codes and parcel ids are allowed (tools return them on purpose);
    coordinates, internal parcel ids, paths and archive names are not. Public: everything restricted is blocked."""
    rules = INTERNAL_RESTRICTED if profile == "internal" else RESTRICTED
    hits = [name for name, rx in rules.items() if rx.search(result_text or "")]
    return (not hits), hits
