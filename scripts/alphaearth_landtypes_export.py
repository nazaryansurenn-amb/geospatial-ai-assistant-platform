"""Freeze a land-type classification version: per-parcel dataset + manifest of every input (orchestrator tool, no AI).

  alphaearth_landtypes_export.py v1 QUEUE

Writes releases/landtypes_<VERSION>/ under the review folder:
  parcels.csv / parcels.parquet  one row per parcel of QUEUE (reviewed + fixed NCI) with the stored answer
  manifest.json                  sha256 of the answer files, queue, key, rules config, reviewer instructions and code
Never overwrites an existing version. Cadastral codes are not included (the internal parcel id links to them locally).
"""
from __future__ import annotations
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
CODE = ["scripts/alphaearth_photo_review.py", "scripts/alphaearth_review_cli.py", "scripts/alphaearth_review_status.py",
        "scripts/alphaearth_review_reconcile.py", "scripts/alphaearth_review_tokens.py", "wp_core/land_types.py",
        "../.claude/agents/land-reviewer.md"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(queue):
    data = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    raw = json.loads((OUT / "queues" / f"{queue}.key.json").read_text(encoding="utf-8"))
    key = {k["id"]: k["internal_parcel_id"] for k in (raw["items"] if isinstance(raw, dict) else raw)}
    answers = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        reviewer = path.stem.split("__", 1)[1]
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            if entry.get("final"):
                answers[item_id] = (reviewer, entry)
    out = []
    for it in data["items"]:
        reviewer, entry = answers.get(it["id"], (None, {}))
        f, p = entry.get("final") or {}, entry.get("photo") or {}
        h = it["hints"]
        deferred = bool(f.get("defer")) or (f.get("decision") or {}).get("type") == "DEFER"
        out.append({
            "item_id": it["id"], "internal_parcel_id": key[it["id"]], "community": it["community"],
            "area_ha": it["area_ha"], "mean_width_m": it["mean_width_m"], "mode": it["mode"],
            "lon": it["lon"], "lat": it["lat"], "photo_date": it["photo"]["date"], "photo_current": it["photo"]["current"],
            "status": "open" if not f else "deferred" if deferred else "classified",
            "type": None if not f else "DEFER" if deferred else f["decision"]["type"],
            "shares": json.dumps(f.get("shares") or {}), "photo_structure": json.dumps(p.get("structure") or {}),
            "confidence": f.get("confidence"), "reason": f.get("reason") or p.get("reason"),
            "attribute": (f.get("decision") or {}).get("attribute"),
            "converted_from": (f.get("decision") or {}).get("converted_from"),
            "contradictions": json.dumps(f.get("contradictions") or []), "rules": f.get("rules"), "reviewer": reviewer,
            "deferred_at": "photo" if p.get("defer") else "final" if deferred else None,
            "cuts": json.dumps(h.get("cuts") or {}), "green_days": json.dumps(h.get("green_days") or {}),
            "cycles": json.dumps(h.get("cycles") or {})})
    for fx in data.get("fixed_status") or []:
        out.append({"item_id": fx["id"], "internal_parcel_id": key[fx["id"]], "area_ha": fx["area_ha"],
                    "mean_width_m": fx["mean_width_m"], "status": "fixed_nci", "type": fx["type"]})
    return out


def main():
    version, queue = sys.argv[1], sys.argv[2]
    target = OUT / "releases" / f"landtypes_{version}"
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten.")
    import pandas as pd
    table = pd.DataFrame(rows(queue))
    target.mkdir(parents=True)
    table.to_parquet(target / "parcels.parquet", index=False)
    table.to_csv(target / "parcels.csv", index=False, encoding="utf-8-sig")
    rules = sorted({r for r in table.rules.dropna()})
    files = ([str(p.relative_to(ROOT)) for p in sorted((OUT / "answers").glob(f"{queue}__claude*.json"))]
             + [str((OUT / "queues" / f"{queue}.json").relative_to(ROOT)), str((OUT / "queues" / f"{queue}.key.json").relative_to(ROOT))]
             + [f"config/{r}.json" for r in rules] + CODE)
    manifest = {"version": version, "queue": queue, "created": datetime.now(timezone.utc).isoformat(),
                "parcels": len(table), "status_counts": table.status.value_counts().to_dict(),
                "type_counts": table.type.value_counts().to_dict(), "rules_versions": rules,
                "label": "AI-interpreted from imagery; not field-verified; unaccepted candidate",
                "files": {f: sha(ROOT / f) for f in files}}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    (target / "parcels.parquet.sha256").write_text(sha(target / "parcels.parquet"), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("version", "parcels", "status_counts", "rules_versions")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
