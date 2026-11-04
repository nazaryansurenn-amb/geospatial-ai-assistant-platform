"""Payload review and owner approval for live AI calls (canal_ops_sim_v1; plan v6 registry since 2026-11-02).

  canal_ops_payload_review.py sample                   -> data/analysis/canal_ops_sim_v1/payload_review_v6/
        runs every v6 agent tool on the REAL tree in fixture mode (no model call) and saves exactly what the model
        would receive: tool outputs, the agent prompts (with the shared map context), and the field list per tool;
        scans it for restricted content (internal profile: parcel ids and cadastral codes are listed and counted,
        coordinates, internal parcel ids, paths and archive names must be 0); writes
        docs/CANAL_OPS_PAYLOAD_REVIEW_V6_2026_11_02_EN.md for the owner. The earlier v1 sample in payload_review/
        and its approval record are kept as they were.
  canal_ops_payload_review.py approve --i-am-the-owner -> config/canal_ops_payload_approval.json
        owner only, typed confirmation at a terminal; binds the approval to the schema hash and the sample hash.
        Any later change to a tool's fields or an agent prompt changes the hash and live mode refuses to start.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import irrigation_assistant as ia  # noqa: E402
from wp_core import canal_ops_tools_v6 as V  # noqa: E402
from wp_core.canal_ops_checker import INTERNAL_RESTRICTED, PARCEL_ITEM, RESTRICTED  # noqa: E402

OUT = ROOT / "data/analysis/canal_ops_sim_v1/payload_review_v6"
DOC = ROOT / "docs/CANAL_OPS_PAYLOAD_REVIEW_V6_2026_11_02_EN.md"
APPROVAL = ia.APPROVAL
CONFIRM = "APPROVE PAYLOAD"
EXCLUDED = ["internal parcel ids (parcel_...)", "parcel geometry or coordinates", "owner or user names",
            "SQL archive data (not read at all)", "file paths", "thresholds of other modules", "API keys",
            "anything for the public view: public_qa has no map tools and the public Checker blocks codes"]
INTERNAL_ONLY = ["parcel ids (f00001) in get_gate and get_parcel", "cadastral codes in get_parcel only",
                 "community names and network ids of the synthetic tree"]


def sample():
    w = V.v6_world(Path(tempfile.mkdtemp()) / "l.sqlite", as_of="2025-07-D2")
    per, scen = "2025-07-D2", "short40"
    ex = V.example_ids(w.maps.nodes)                     # ids of the network in use
    rows = []

    def add(name, r):
        rows.append({"tool": name, "payload": r})
        return r

    add("get_network", V.get_network(w))
    add("deliverable", V.deliverable(w, per, scen))
    add("compare_rules", V.compare_rules(w, per, scen))
    sim = add("simulate_plan", V.simulate_plan(w, per, scen, all_floor=0.3, agent="planner"))
    sid = sim["data"]["sim_run_id"]
    add("simulate_plan (drill-down)", V.simulate_plan(w, per, scen, all_floor=0.3, node=ex["big_s1"], agent="planner"))
    add("tail_risk", V.tail_risk(w, per, sid))
    add("get_history", V.get_history(w))
    add("get_constraints", V.get_constraints(w))
    add("get_tree", V.get_tree(w, ex["big_s1"]))
    gate = add("get_gate", V.get_gate(w, ex["direct"]))
    add("get_parcel", V.get_parcel(w, next(iter(gate["data"]["parcels"]))))
    com = add("get_community", V.get_community(w, w.maps.nodes.at[ex["big"], "community"]))
    add("submit_reply (router: an agent's reply)", V.submit_reply(            # chat router: the fixed reply shape
        w, "partly", f"{{{{{com['rid']}.ha_by_type.ORCHARD}}}} ha orchards to protect.", "observed crop stage",
        "season_monitor", agent="agronomy"))
    add("find", V.find(w, "vineyard"))
    add("submit_planner", V.submit_planner(w, [sid]))
    add("submit_reviewer", V.submit_reviewer(w, [{"node": ex["big"], "metric": "tail pct", "sim_run_id": sid}]))
    add("schedule", V.schedule(w, per, scenario=scen, sim_run_id=sid))
    add("schedule (week, secondary)", V.schedule(w, scope="week", date="2025-07-11", node=ex["big_s1"], scenario=scen))
    add("save_proposal", V.save_proposal(w, sid))
    prompts = {k: v["prompt"] for k, v in ia.agent_definitions().items()}
    text = json.dumps(rows, ensure_ascii=False, default=str) + "\n".join(prompts.values())   # prompts unescaped
    hits = {name: len(rx.findall(text)) for name, rx in INTERNAL_RESTRICTED.items()}
    internal = {"cadastral_codes": len(RESTRICTED["cadastral_code"].findall(text)), "parcel_ids": len(PARCEL_ITEM.findall(text)),
                "codes_outside_get_parcel": sum(len(RESTRICTED["cadastral_code"].findall(json.dumps(r, ensure_ascii=False, default=str)))
                                                for r in rows if r["tool"] != "get_parcel")}
    communities = sorted(set(w.maps.parcels.community.dropna()))
    names_in_payload = [c for c in communities if c in json.dumps(rows, ensure_ascii=False)]
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "sample.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    (OUT / "prompts.json").write_text(json.dumps(prompts, indent=1, ensure_ascii=False), encoding="utf-8")
    sample_hash = hashlib.sha256((OUT / "sample.jsonl").read_bytes()).hexdigest()
    summary = {"schema_hash": ia.payload_schema_hash(), "sample_hash": sample_hash,
               "network": "real canal, synthetic tree (plan v6)", "restricted_hits": hits, "internal_only": internal,
               "community_names_in_payload": names_in_payload,
               "fields": {t: sorted(f) for t, f in V.ALLOWED_FIELDS_V6.items()},
               "agents": {k: v["tools"] for k, v in ia.agent_definitions().items()},
               "limits": ia.payload_schema()["limits"],
               "map_context_tokens": V.estimate_tokens(V.map_context()),
               "bytes_per_tool": {r["tool"]: len(json.dumps(r["payload"]["data"], ensure_ascii=False, default=str).encode())
                                  for r in rows}}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    write_doc(summary, rows, prompts)
    print(json.dumps({k: summary[k] for k in ("restricted_hits", "internal_only", "bytes_per_tool")}, indent=1,
                     ensure_ascii=False))
    return summary


def write_doc(summary, rows, prompts):
    ex = next(r for r in rows if r["tool"] == "simulate_plan")["payload"]
    ex_short = {"rid": ex["rid"], "data": {**{k: v for k, v in ex["data"].items() if k != "nodes"},
                                           "nodes": dict(list(ex["data"]["nodes"].items())[:3])}}
    par = next(r for r in rows if r["tool"] == "get_parcel")["payload"]
    lines = [
        "# Payload review v6: what the Irrigation Assistant would send to the AI model (2026-11-02)",
        "",
        "You chose \"Switch now\": live chat moves to the plan v6 tools and instructions. Live AI answers stay OFF until",
        "you approve this payload. The approval binds to the exact tool fields, limits and agent instructions below; any",
        "later change to them changes the hash and live mode refuses to start until you approve again.",
        "",
        "## What goes to the model",
        "- **Tool results** (numbers computed by fixed code), only the fields listed per tool; totals first, lists of at",
        f"  most {summary['limits']['page_max']} items ({summary['limits']['alloc_page']} in allocation results) with a cursor, "
        f"each result at most {summary['limits']['max_result_bytes']} bytes:",
        "",
        "| Tool | Fields | Size in this sample |",
        "|---|---|---|",
    ]
    for t, f in summary["fields"].items():
        lines.append(f"| {t} | {', '.join(f)} | {summary['bytes_per_tool'].get(t, '-')} bytes |")
    lines += ["",
              "- **New in v6, internal page only:**"]
    lines += [f"  - {e};" for e in INTERNAL_ONLY]
    lines += [f"  - in this sample: {summary['internal_only']['cadastral_codes']} cadastral code(s), all in get_parcel "
              f"({summary['internal_only']['codes_outside_get_parcel']} elsewhere), {summary['internal_only']['parcel_ids']} parcel id(s).",
              "- **Identifiers:** tree ids (`I-G04`, `I-G04-S1-G2-T1-G3`, `...-D5`, `...-E`), result ids (`R1`), simulation ids.",
              f"- **Community names in the payload:** {', '.join(summary['community_names_in_payload']) or 'none'}.",
              "- **Your questions in chat**, after code removes names, phone numbers, e-mails and cadastral codes you type.",
              f"- **Agent instructions** for all 12 agents with the shared map context (about {summary['map_context_tokens']} "
              "tokens; saved in `payload_review_v6/prompts.json`). The planner ends with submit_planner, the reviewer with",
              "  submit_reviewer (simulation ids and node ids only); the main agent ends with its answer.",
              "- **Tool lists per agent:** public_qa has no tools; get_parcel (codes) only for main, agronomy, data auditor",
              "  and staff Q&A.",
              "", "## What never goes to the model", ""]
    lines += [f"- {e}" for e in EXCLUDED]
    lines += ["", "## Automatic scan of this sample (must all be 0)", "",
              "| Pattern | Hits |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["restricted_hits"].items()]
    lines += ["", "## Example: one simulation result as the model would receive it (3 of the tree nodes shown)", "",
              "```json", json.dumps(ex_short, indent=1, ensure_ascii=False), "```", "",
              "## Example: one parcel (internal page only)", "",
              "```json", json.dumps(par, indent=1, ensure_ascii=False), "```", "",
              "## Things to check on your side before approving",
              "- In the Claude Console (platform.claude.com): your organisation's data-retention setting and a monthly",
              "  spend limit for this API key. Cadastral codes go to the model only through get_parcel on this page.",
              "- Live use is internal (port 8554 on this PC); the public view never calls the model.", "",
              "## How to approve",
              "Run in a terminal from `WORKING_PRODUCT` and type the phrase it asks for:", "",
              "```", ".venv/Scripts/python.exe -B scripts/canal_ops_payload_review.py approve --i-am-the-owner", "```", "",
              "After approving, restart 8554 in live mode (launch config irrigation-assistant-live).", "",
              f"Schema hash: `{summary['schema_hash'][:16]}…` · sample hash: `{summary['sample_hash'][:16]}…`"]
    DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _interactive() -> bool:
    """A real terminal with a person at it. On Windows the NUL device claims isatty(), so ask the console API."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))


def approve():
    if "--i-am-the-owner" not in sys.argv:
        raise SystemExit("approve is for the owner only: run it yourself with --i-am-the-owner")
    if not _interactive():
        raise SystemExit("approve needs the owner at a terminal (typed confirmation)")
    s = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    if s["schema_hash"] != ia.payload_schema_hash():
        raise SystemExit("the tool fields or prompts changed since the sample; run `sample` again and review it")
    if any(s["restricted_hits"].values()):
        raise SystemExit("the sample contains restricted content; it cannot be approved")
    print(f"Review: {DOC}")
    if input(f"Type '{CONFIRM}' to allow live AI calls with exactly this payload schema: ").strip() != CONFIRM:
        raise SystemExit("not confirmed; nothing written")
    APPROVAL.write_text(json.dumps({"schema_hash": s["schema_hash"], "sample_hash": s["sample_hash"],
                                    "approved_by": "owner (typed confirmation)",
                                    "approved_at": datetime.now(timezone.utc).isoformat(),
                                    "scope": "internal page 8554 only; public view never calls the model"}, indent=1),
                        encoding="utf-8")
    print(f"approved: {APPROVAL.name}")


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["sample"]:
        sample()
    elif cmd == ["approve"]:
        approve()
    else:
        sys.exit(__doc__)
