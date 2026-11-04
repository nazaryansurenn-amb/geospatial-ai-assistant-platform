"""Owner check of the final land types v1.1 map. Owner decision 2026-10-31 after the safety review: the owner picks the
type FIRST and sees the map's answer only afterwards (first pick locked, comment always possible), on a stratified
random sample. No trap answers. This gives the accuracy of typed parcels with 95% ranges, weighted to the map.

  alphaearth_landtypes_v11_verify.py make [SIZE]   -> verify/v11_final.json (+ key); page /verify?set=v11_final
  alphaearth_landtypes_v11_verify.py score         -> accuracy per stratum and weighted overall (95% ranges), comments

Sample: AI-typed parcels of the frozen v1.1 release that the owner has not labelled, stratified by published type
(at least 15 each, the rest proportional), with perennials seen only on 2021-22 photos and road-shape parcels as
their own strata. UNCLASSIFIED parcels are not shown (they make no claim; coverage is reported beside the accuracy).
"Can't tell" answers are reported and left out of the accuracy.
"""
from __future__ import annotations
import json
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import OUT  # noqa: E402
from alphaearth_landtypes_v11_merge import OLD_PHOTO, wilson  # noqa: E402

NAME = "v11_final"
RELEASE = OUT / "releases/landtypes_v1_1"
LABELS = {"FIELD_CROP": "Field crops (annual or alfalfa)", "VINEYARD": "Vineyard", "ORCHARD": "Orchard / fruit trees",
          "NON_FARM": "Not farmland (buildings, roads, water, tree belts)", "GREENHOUSE": "Greenhouses",
          "NO_CROP": "No crop in 2025 (fallow, grass, natural ground)"}


def stratum(attrs, source, type6):
    if OLD_PHOTO in (attrs or ""):
        return "old-photo perennial"
    if str(source).startswith("rule road"):
        return "road shape"
    return type6


def allocate(sizes, total, floor=15):
    """At least `floor` per stratum (or all of it), the rest proportional to the remaining size."""
    per = {s: min(n, floor) for s, n in sizes.items()}
    spare = max(0, total - sum(per.values()))
    rest = {s: n - per[s] for s, n in sizes.items()}
    pool = sum(rest.values())
    for s in rest:
        per[s] += min(rest[s], int(spare * rest[s] / pool)) if pool else 0
    return per


def make(size=150, seed=20261112):
    target = OUT / "verify" / f"{NAME}.json"
    if target.exists():
        raise SystemExit("Verify set already exists; it is never redrawn.")
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    d = d[(d.status != "fixed_nci") & d.owner_label.isna() & (d.type6_model != "UNCLASSIFIED")].copy()
    d["stratum"] = [stratum(r.attrs_v11, r.source_v11, r.type6_model) for r in d.itertuples()]
    sizes = d.stratum.value_counts().to_dict()
    per = allocate(sizes, size)
    rng = random.Random(seed)
    items = {i["id"]: i for i in json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))["items"]}
    rows = d.set_index("item_id")
    shown, key = [], {}
    for s in sorted(per):
        for item_id in sorted(rng.sample(sorted(rows[rows.stratum == s].index), per[s])):
            r, it = rows.loc[item_id], items[item_id]
            vid = f"v{len(shown) + 1:03d}"
            note = ("The map's photo is from 2021-22: is it still there?" if s == "old-photo perennial"
                    else "Shape rule: likely road / track / canal" if s == "road shape" else "")
            shown.append({"vid": vid, "lat": it["lat"], "lon": it["lon"], "outline": it["outline"], "photo": it["photo"],
                          "area_ha": it["area_ha"], "community": it["community"], "shown_type": r.type6_model,
                          "shown_components": [], "shown_note": note})
            key[vid] = {"item": item_id, "stratum": s, "shown": r.type6_model, "confidence": r.confidence}
    rng.shuffle(shown)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"queue": "prod_full_v24", "items": shown, "choices": list(LABELS), "labels": LABELS,
                                  "traps": False, "owner_first": True}, ensure_ascii=False), encoding="utf-8")
    (OUT / "verify" / f"{NAME}.key.json").write_text(json.dumps(
        {"release": str(RELEASE.relative_to(OUT)), "strata_sizes": sizes, "sampled": per, "items": key}, indent=1),
        encoding="utf-8")
    print(f"verify set {NAME}: {len(shown)} parcels; strata (map size, sampled): {({s: (sizes[s], per[s]) for s in per})}")


def summarise(key, ans):
    """Accuracy per stratum (can't-tell left out and reported), weighted to the map by stratum size, with 95% ranges
    (Wilson per stratum; stratified normal range overall); every comment listed."""
    t = pd.DataFrame([{**k, "vid": v, **ans[v]} for v, k in key["items"].items() if v in ans])
    out = {"note": "owner picked the type before seeing the map's answer; stratified random sample of TYPED parcels "
                   "(unclassified parcels are reported as coverage, not scored); can't-tell answers are left out",
           "answered": len(t), "of": len(key["items"]), "by_stratum": {}}
    total = sum(key["strata_sizes"].values())
    est = var = covered = 0.0
    for s, g in (t.groupby("stratum") if len(t) else []):
        decided = g[g.verdict != "cannot_tell"]
        n, right = len(decided), int((decided.verdict == "agree").sum())
        out["by_stratum"][s] = {"answered": len(g), "right": right, "wrong": n - right,
                                "cannot_tell": len(g) - n, "accuracy": round(right / n, 3) if n else None,
                                "accuracy_95": wilson(right, n),
                                "owner_says": g[g.verdict == "disagree"].correct.value_counts().to_dict()}
        if n:
            w, p = key["strata_sizes"][s] / total, right / n
            est += w * p
            var += w * w * p * (1 - p) / max(n - 1, 1)
            covered += w
    if covered:
        acc, se = est / covered, var ** .5 / covered
        out["weighted_accuracy"] = round(acc, 3)
        out["weighted_accuracy_95"] = [round(max(0, acc - 1.96 * se), 3), round(min(1, acc + 1.96 * se), 3)]
    else:
        out["weighted_accuracy"] = out["weighted_accuracy_95"] = None
    out["comments"] = [{"vid": r.vid, "shown": r.shown, "verdict": r.verdict, "note": r.note}
                       for r in t.itertuples() if getattr(r, "note", "")] if len(t) else []
    return out


def score():
    key = json.loads((OUT / "verify" / f"{NAME}.key.json").read_text(encoding="utf-8"))
    ans = json.loads((OUT / "answers" / f"verify_{NAME}__owner.json").read_text(encoding="utf-8"))
    print(json.dumps(summarise(key, ans), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "make":
        make(int(sys.argv[2]) if len(sys.argv) > 2 else 150)
    elif cmd == "score":
        score()
    else:
        sys.exit(__doc__)
