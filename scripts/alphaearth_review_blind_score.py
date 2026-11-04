"""Score the AI against the owner's BLIND labels (orchestrator tool, no AI).

  alphaearth_review_blind_score.py NAME      NAME of a blind test built by make-blind-test

The owner labelled the parcels without seeing the AI answer. Per AI type: how often the owner gave the same type.
Overall: weighted by stratum size (rare types were oversampled), with a normal-approximation 95% interval.
Go/no-go (final report, gate 1): overall >= 90% with lower bound >= 85%; any AI type under 80% goes to the owner queue.
"""
from __future__ import annotations
import collections
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
PERENNIAL = {"VIN", "ORC", "ALF"}


def score(name):
    design = json.loads((OUT / "queues" / f"blind_{name}.key.json").read_text(encoding="utf-8"))["design"]
    path = OUT / "answers" / f"blind_{name}__owner.json"
    owner = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    truth = {}
    for item_id, entry in owner.items():
        final = entry.get("final")
        if final and not final.get("defer"):
            truth[item_id] = final["decision"]["type"]
    by_stratum = collections.defaultdict(lambda: [0, 0])
    by_type = collections.defaultdict(lambda: [0, 0])
    confusion = collections.Counter()
    for item_id, ai_type in design["ai"].items():
        if item_id not in truth:
            continue
        ok = truth[item_id] == ai_type
        for table, k in ((by_stratum, design["stratum"][item_id]), (by_type, ai_type)):
            table[k][0] += ok
            table[k][1] += 1
        if not ok:
            confusion[f"AI {ai_type} -> owner {truth[item_id]}"] += 1
    sizes = design["strata_sizes"]
    total = sum(sizes[s] for s in by_stratum)
    acc = sum(sizes[s] * c / n for s, (c, n) in by_stratum.items()) / total if total else None
    var = sum((sizes[s] / total) ** 2 * (c / n) * (1 - c / n) / n for s, (c, n) in by_stratum.items() if n) if total else 0
    low = None if acc is None else acc - 1.96 * math.sqrt(var)
    missed_perennials = sum(1 for i, t in truth.items() if t in PERENNIAL and design["ai"].get(i) not in PERENNIAL)
    report = {"name": name, "labelled": len(truth), "of": len(design["ai"]), "owner_unsure": len(owner) - len(truth),
              "accuracy_weighted": None if acc is None else round(acc, 3), "lower_95": None if low is None else round(low, 3),
              "go": acc is not None and acc >= .90 and low >= .85,
              "by_ai_type": {t: {"correct": c, "checked": n, "rate": round(c / n, 2)} for t, (c, n) in sorted(by_type.items())},
              "types_under_80_percent": sorted(t for t, (c, n) in by_type.items() if n >= 5 and c / n < .8),
              "perennials_missed_by_ai": missed_perennials, "differences": confusion.most_common()}
    (OUT / "reports").mkdir(exist_ok=True)
    (OUT / "reports" / f"blind_{name}_score.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(json.dumps(score(sys.argv[1]), indent=1))
