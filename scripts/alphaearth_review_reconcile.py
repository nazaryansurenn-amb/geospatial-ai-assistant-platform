"""End-of-run reconciliation for a land-type review queue (orchestrator tool, no AI).

  alphaearth_review_reconcile.py QUEUE

Every parcel of the queue ends in exactly one state: classified, deferred (owner queue), fixed NCI (not
classifiable from imagery, no review) or open (no final answer yet). Checks that the states add up to the queue's
parcel count and lists the open parcels with any logged problems. Writes reports/<QUEUE>_reconcile.json.
"""
from __future__ import annotations
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"


def reconcile(queue):
    data = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    key = json.loads((OUT / "queues" / f"{queue}.key.json").read_text(encoding="utf-8"))
    total = len(key["items"] if isinstance(key, dict) else key)
    fixed = {f["id"] for f in data.get("fixed_status") or []}
    state, types, reviewers = {}, collections.Counter(), collections.Counter()
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        reviewer = path.stem.split("__", 1)[1]
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            final = entry.get("final")
            if not final or item_id in state:
                continue
            kind = "DEFER" if final.get("defer") else final["decision"]["type"]
            state[item_id] = "deferred" if kind == "DEFER" else "classified"
            types[kind] += 1
            reviewers[reviewer] += 1
    for item_id in fixed:
        state[item_id] = "fixed_nci"
    open_ids = sorted(i["id"] for i in data["items"] if i["id"] not in state)
    log = OUT / "logs" / "problems.jsonl"
    problems = [p for p in (json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip())
                if p.get("queue") == queue] if log.exists() else []
    unit_of = {i: b["id"] for b in data.get("blocks", []) for i in b["items"]}
    counts = collections.Counter(state.values())
    counts["open"] = len(open_ids)
    report = {"queue": queue, "at": datetime.now(timezone.utc).isoformat(), "parcels": total, "states": dict(counts),
              "adds_up": sum(counts.values()) == total, "final_types": dict(types.most_common()),
              "answers_per_reviewer": dict(reviewers),
              "open": [{"id": i, "unit": unit_of.get(i, i),
                        "problems": [p["kind"] for p in problems if p.get("unit") in (i, unit_of.get(i)) or i in p.get("error", "")]}
                       for i in open_ids],
              "tool_problems_by_kind": dict(collections.Counter(p["kind"] for p in problems))}
    folder = OUT / "reports"
    folder.mkdir(exist_ok=True)
    (folder / f"{queue}_reconcile.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    r = reconcile(sys.argv[1])
    print(json.dumps({k: v for k, v in r.items() if k != "open"}, indent=1, ensure_ascii=False))
    print(f"open parcels: {len(r['open'])}" + ("" if not r["open"] else " -> " + ", ".join(o["id"] for o in r["open"][:30])))
