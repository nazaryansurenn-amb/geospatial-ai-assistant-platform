"""Score reviewer versions against the owner's verify answers (orchestrator tool, no AI).

  alphaearth_review_verify_score.py NAME [QUEUE ...]   NAME = a verify set built by make-verify-compare; extra
                                                       QUEUEs (copies of a version's queue, same parcel ids, e.g. a
                                                       run with another model) are scored on the same truths

Owner truth per parcel: the shown type when the owner agreed, the corrected type when the owner disagreed;
"cannot tell" and the disclosed check answers are excluded. Each version's accuracy counts its typed answers that
match the truth (its deferrals are reported separately). Estimates are weighted by stratum size, because the
sample over-represents parcels where the versions disagree.
"""
from __future__ import annotations
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1/verify"
ANSWERS = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1/answers"


def finals(queue):
    out = {}
    for path in sorted(ANSWERS.glob(f"{queue}__claude*.json")):
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            final = entry.get("final")
            if final:
                out[item_id] = "DEFER" if final.get("defer") else final["decision"]["type"]
    return out


def score(name, extra=()):
    key = json.loads((VERIFY / f"{name}.key.json").read_text(encoding="utf-8"))
    for queue in extra:  # later runs on the same parcels join the scoring without changing the sealed key file
        answers = finals(queue)
        for k in key["items"].values():
            if k["item"] in answers:
                k["answers"][queue] = answers[k["item"]]
        key["versions"].append(queue)
    shown = {i["vid"]: i for i in json.loads((VERIFY / f"{name}.json").read_text(encoding="utf-8"))["items"]}
    owner_path = ANSWERS / f"verify_{name}__owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8")) if owner_path.exists() else {}
    traps = {"caught": 0, "missed": 0}
    truth = {}
    for vid, k in key["items"].items():
        o = owner.get(vid)
        if not o or o["verdict"] == "cannot_tell":
            continue
        if k["trap"]:
            traps["caught" if o["verdict"] == "disagree" else "missed"] += 1
            continue
        truth[vid] = shown[vid]["shown_type"] if o["verdict"] == "agree" else o["correct"]
    sizes = key["strata_sizes"]
    report = {"name": name, "owner_checked": len(owner), "truths": len(truth), "check_answers": traps, "versions": {}}
    for version in key["versions"]:
        per_stratum = collections.defaultdict(lambda: [0, 0, 0])  # correct, typed, deferred
        for vid, t in truth.items():
            k = key["items"][vid]
            answer = k["answers"].get(version)
            if answer is None:
                continue
            cell = per_stratum[k["stratum"]]
            if answer == "DEFER":
                cell[2] += 1
            else:
                cell[1] += 1
                cell[0] += answer == t
        weight = {s: sizes[s] for s in per_stratum if per_stratum[s][1]}
        weighted = sum(weight[s] * per_stratum[s][0] / per_stratum[s][1] for s in weight) / max(sum(weight.values()), 1)
        report["versions"][version] = {
            "checked": sum(c[1] + c[2] for c in per_stratum.values()), "typed": sum(c[1] for c in per_stratum.values()),
            "correct": sum(c[0] for c in per_stratum.values()), "deferred": sum(c[2] for c in per_stratum.values()),
            "accuracy_weighted": round(weighted, 3),
            "by_stratum": {s: {"correct": c[0], "typed": c[1], "deferred": c[2]} for s, c in sorted(per_stratum.items())}}
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(json.dumps(score(sys.argv[1], sys.argv[2:]), indent=1))
