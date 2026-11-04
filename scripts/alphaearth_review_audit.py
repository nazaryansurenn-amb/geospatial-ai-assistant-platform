"""Blind watchdog audit of a reviewed queue (approved plan: the watchdog photo-rechecks a small random share).

  alphaearth_review_audit.py sample QUEUE N SEED       print N random answered parcels: image, number on it, signals
                                                       (never the reviewers' answers); remembers the sample
  alphaearth_review_audit.py score QUEUE 'ID TYPE ...' compare the auditor's blind types with the stored final types

Audit labels are stored only in reports/<QUEUE>_audit.json, never as review answers.
"""
from __future__ import annotations
import collections
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_review_cli import compact_line  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
RENDER = Path(tempfile.gettempdir()) / "alphaearth_review_shots" / "render"
CODES = {"VIN", "ORC", "ALF", "ANN", "GRH", "NCU", "GRS", "BLT", "WAT", "NAT", "MIX", "PART", "DEFER"}


def stored(queue):
    out = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            final = entry.get("final")
            if final:
                out[item_id] = "DEFER" if final.get("defer") else final["decision"]["type"]
    return out


def sample(queue, n, seed):
    data = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    items = {i["id"]: i for i in data["items"]}
    position = {i: (b["id"], k + 1) for b in data.get("blocks", []) for k, i in enumerate(b["items"])}
    answered = sorted(i for i, t in stored(queue).items() if t != "DEFER")
    chosen = sorted(random.Random(seed).sample(answered, min(n, len(answered))))
    folder = OUT / "reports"
    folder.mkdir(exist_ok=True)
    (folder / f"{queue}_audit_sample.json").write_text(json.dumps({"seed": seed, "ids": chosen}), encoding="utf-8")
    for item_id in chosen:
        it = items[item_id]
        unit, number = position.get(item_id, (item_id, 1))
        signals = {k: v for k, v in it["hints"].items() if k in ("ndvi", "cycles", "ndmi_2025", "bsi_2025")}
        print(f"{item_id} image {RENDER / queue / (unit + '.jpg')} number {number} | {it['mode']} {it['area_ha']}ha "
              f"{it['mean_width_m']}m photo {it['photo']['date']}{'' if it['photo']['current'] else ' OLD'} | {compact_line(signals)}")


def score(queue, text):
    chosen = json.loads((OUT / "reports" / f"{queue}_audit_sample.json").read_text(encoding="utf-8"))["ids"]
    audit = {}
    for line in filter(None, (x.strip() for x in text.replace(";", "\n").splitlines())):
        item_id, kind = line.split()[:2]
        if kind not in CODES:
            raise SystemExit(f"unknown type {kind} for {item_id}; use {sorted(CODES)}")
        audit[item_id] = kind
    if set(audit) != set(chosen):
        raise SystemExit(f"audit must cover exactly the sample; missing {sorted(set(chosen) - set(audit))[:10]}")
    answers = stored(queue)
    rated = [i for i in chosen if audit[i] != "DEFER"]
    agree = [i for i in rated if audit[i] == answers[i]]
    report = {"queue": queue, "sample": len(chosen), "auditor_unsure": len(chosen) - len(rated), "agree": len(agree),
              "agreement": round(len(agree) / max(len(rated), 1), 3),
              "differences": collections.Counter(f"{answers[i]}->{audit[i]}" for i in rated if audit[i] != answers[i]).most_common(),
              "per_parcel": {i: {"reviewer": answers[i], "auditor": audit[i]} for i in chosen}}
    (OUT / "reports" / f"{queue}_audit.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_parcel"}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["sample"] and len(sys.argv) == 5:
        sample(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    elif sys.argv[1:2] == ["score"] and len(sys.argv) == 4:
        score(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)
