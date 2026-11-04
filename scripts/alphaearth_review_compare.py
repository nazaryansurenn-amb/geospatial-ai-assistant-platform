"""Compare reviewer answers between test queues over the same parcels (orchestrator tool, no AI).

  alphaearth_review_compare.py QUEUE_A QUEUE_B [QUEUE_C ...] [--recompute]

Pairwise agreement on parcels both queues classified, deferrals, type counts and the differing parcels. With
--recompute, every final answer's type is re-derived from its stored shares under the current rules (so answers
saved under older rules versions are compared on equal terms).
"""
from __future__ import annotations
import collections
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wp_core.land_types import decide, weak_spring_peak  # noqa: E402

OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
CONFIG = json.loads((ROOT / "config/alphaearth_types_20261030_v2_1.json").read_text(encoding="utf-8"))


def finals(queue, hints, recompute):
    """parcel -> (type, reviewer); latest answer wins; photo-stage defers count as DEFER."""
    res = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        reviewer = path.stem.split("__", 1)[1]
        for pid, entry in json.loads(path.read_text(encoding="utf-8")).items():
            record = entry.get("final") or (entry.get("photo") if (entry.get("photo") or {}).get("defer") else None)
            if record is None:
                continue
            kind = "DEFER" if record.get("defer") else record["decision"]["type"]
            if recompute and not record.get("defer") and record.get("shares"):
                kind = decide(record["shares"], CONFIG)["type"]
                weak = CONFIG["rules"]["weak_spring_peak"]
                if kind in weak["applies_to"] and weak_spring_peak(hints.get(pid) or {}, weak):
                    kind = weak["result"]
            if pid not in res or record.get("at", "") > res[pid][2]:
                res[pid] = (kind, reviewer, record.get("at", ""))
    return res


def main():
    queues = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(queues) < 2:
        raise SystemExit(__doc__)
    first = json.loads((OUT / "queues" / f"{queues[0]}.json").read_text(encoding="utf-8"))
    hints = {i["id"]: i.get("hints") for i in first["items"]}
    fixed = {f["id"] for f in first.get("fixed_status") or []}
    ids = [i["id"] for i in first["items"] if i["id"] not in fixed]
    answers = {q: finals(q, hints, "--recompute" in sys.argv) for q in queues}
    for q in queues:
        done = [i for i in ids if i in answers[q]]
        print(f"{q}: {len(done)} of {len(ids)} done; " + str(collections.Counter(answers[q][i][0] for i in done).most_common()))
    for a, b in itertools.combinations(queues, 2):
        both = [i for i in ids if i in answers[a] and i in answers[b]]
        cls = [i for i in both if "DEFER" not in (answers[a][i][0], answers[b][i][0])]
        agree = [i for i in cls if answers[a][i][0] == answers[b][i][0]]
        print(f"\n{a} vs {b}: both classified {len(cls)}, agree {len(agree)} = {len(agree) / max(len(cls), 1):.0%}; "
              f"defers {sum(answers[a][i][0] == 'DEFER' for i in both)} / {sum(answers[b][i][0] == 'DEFER' for i in both)}")
        diff = collections.Counter((answers[a][i][0], answers[b][i][0]) for i in cls if answers[a][i][0] != answers[b][i][0])
        print("  differences:", diff.most_common())


if __name__ == "__main__":
    main()
