"""Land types v1.2: candidate overlay and the combined owner-first check page (run plan steps 4-5). Never edits v1.1.

  alphaearth_landtypes_v12_check.py overlay   -> v12/overlay.parquet: per parcel the v1.2 candidate type and its group
  alphaearth_landtypes_v12_check.py page      -> verify/v12_combined.json (+ key): the 146 v11_final parcels plus a
                                                random 20 per group (15 if the group has fewer than 60), shuffled
  alphaearth_landtypes_v12_check.py score     -> per-group results: first pick / corrected / merged (Addenda 6-7)
  alphaearth_landtypes_v12_check.py freeze    -> releases/landtypes_v1_2 (owner 2026-11-01), never overwritten

Rules: docs/ALPHAEARTH_V12_PREREGISTRATION_2026_10_31_EN.md (Addenda 1-4). Owner labels win; a broader class counts as
right when the owner picks one of its sub-types; nothing here changes v1.1.
"""
from __future__ import annotations
import glob
import json
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import OUT  # noqa: E402
from alphaearth_landtypes_v11_merge import type6_of, wilson  # noqa: E402

RELEASE = OUT / "releases/landtypes_v1_1"
WORK = OUT / "v12"
NAME = "v12_combined"
GROUPS = ("C", "D", "A1", "A2", "P", "K", "B", "W", "S", "B-fix")
SEEDS = {"C": 20261116, "D": 20261117, "A1": 20261119, "A2": 20261120, "P": 20261121, "K": 20261122,
         "B": 20261115, "W": 20261123, "S": 20261124, "B-fix": 20261125}
ACCEPT = {"PERENNIAL": ["VINEYARD", "ORCHARD"], "CROPLAND_UNCERTAIN": ["FIELD_CROP", "NO_CROP"]}
LABELS = {"FIELD_CROP": "Field crops (annual or alfalfa)", "VINEYARD": "Vineyard", "ORCHARD": "Orchard / fruit trees",
          "NON_FARM": "Not farmland (buildings, roads, water, tree belts)", "GREENHOUSE": "Greenhouses",
          "NO_CROP": "No crop in 2025 (fallow, grass, natural ground)"}
EXTRA_LABELS = {"PERENNIAL": "Perennial planting (vineyard or orchard, type not specified)",
                "CROPLAND_UNCERTAIN": "Field land, 2025 use not specified (field crops or no crop)"}
NOTES = {"S": "Strip rule: continues the neighbouring field", "B-fix": "Re-reviewed: field crops next to vineyards",
         "P": "Broader class", "K": "Broader class"}


def answers(queue):
    out = {}
    for p in glob.glob(str(OUT / "answers" / f"{queue}__claude*.json")):
        for k, e in json.loads(Path(p).read_text(encoding="utf-8")).items():
            if e.get("final") and not e["final"].get("defer"):
                out[k] = e["final"]
    return out


def overlay():
    d = pd.read_parquet(RELEASE / "parcels.parquet").set_index("item_id")
    straight = pd.read_parquet(OUT / "releases/landtypes_v1_1_draft/straightness.parquet").set_index("internal_parcel_id").straight
    rows = []
    cand = pd.read_parquet(WORK / "candidates.parquet")
    for r in cand.itertuples():
        rows.append({"item_id": r.item_id, "group": r.group, "v12_type": r.v12_candidate, "confidence": None,
                     "source": f"rule {r.group}"})
    t6 = lambda f, i: type6_of(f, d.loc[i], straight.get(d.at[i, "internal_parcel_id"]))
    key = json.loads((OUT / "queues/v12_blocks.key.json").read_text(encoding="utf-8"))
    role = {i: g for b in key["blocks"] for i, g in b["roles"].items()}
    for i, f in answers("v12_blocks").items():
        g, conf = role.get(i), f.get("confidence")
        if g in ("B", "W") and d.at[i, "type6"] == "UNCLASSIFIED" and conf in ("medium", "high"):
            rows.append({"item_id": i, "group": g, "v12_type": t6(f, i), "confidence": conf, "source": "block review"})
        elif g == "B-fix" and conf == "high" and t6(f, i) not in (None, d.at[i, "type6"]):
            rows.append({"item_id": i, "group": g, "v12_type": t6(f, i), "confidence": conf, "source": "block review"})
    # group S only after the pilot's go (the full queue exists); the 2026-10-31 pilot was NO-GO (33% clear, a control
    # pulled), so no strip is typed by the continuity rule
    sa = {}
    if (OUT / "queues/v12_strips.json").exists():
        strips = set(json.loads((OUT / "queues/v12_strips_pilot.key.json").read_text(encoding="utf-8"))["strips"])
        sa = {k: v for k, v in answers("v12_strips_pilot").items() if k in strips}
        sa.update(answers("v12_strips"))
    for i, f in sa.items():
        if f.get("confidence") in ("medium", "high") and (f.get("reason") or "").strip().lower().startswith("continues"):
            rows.append({"item_id": i, "group": "S", "v12_type": t6(f, i), "confidence": f["confidence"], "source": "strip continuity"})
    o = pd.DataFrame(rows)
    o = o[o.v12_type.notna() & ~o.item_id.map(lambda i: isinstance(d.at[i, "owner_label"], str))]
    o = o.drop_duplicates("item_id", keep="first")
    o["internal_parcel_id"] = o.item_id.map(d.internal_parcel_id)
    o["v11_type"] = o.item_id.map(d.type6)
    o["area_ha"] = o.item_id.map(d.area_ha)
    o["version"] = "v1.2 candidate"
    o.to_parquet(WORK / "overlay.parquet", index=False)
    summary = {g: {"parcels": int((o.group == g).sum()), "ha": round(o[o.group == g].area_ha.sum(), 1),
                   "types": o[o.group == g].v12_type.value_counts().to_dict()} for g in GROUPS}
    (WORK / "overlay_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary))


def page():
    target = OUT / "verify" / f"{NAME}.json"
    if target.exists():
        raise SystemExit("Verify set already exists; it is never redrawn.")
    o = pd.read_parquet(WORK / "overlay.parquet")
    base = json.loads((OUT / "verify/v11_final.json").read_text(encoding="utf-8"))
    bkey = json.loads((OUT / "verify/v11_final.key.json").read_text(encoding="utf-8"))
    q = {i["id"]: i for i in json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))["items"]}
    taken = {bkey["items"][it["vid"]]["item"] for it in base["items"]}
    shown, key = [], {}
    for it in base["items"]:
        vid = f"c{len(shown) + 1:03d}"
        shown.append({**it, "vid": vid})
        key[vid] = {**bkey["items"][it["vid"]], "group": "v1.1", "v11_vid": it["vid"]}
    sampled = {}
    for g in GROUPS:
        pool = sorted(set(o[o.group == g].item_id) - taken)
        n = min(len(pool), 20 if len(pool) >= 60 else 15)
        pick = sorted(random.Random(SEEDS[g]).sample(pool, n)) if n else []
        sampled[g] = {"group_size": int((o.group == g).sum()), "sampled": n}
        types = o.set_index("item_id").v12_type
        for i in pick:
            it, t = q[i], types[i]
            vid = f"c{len(shown) + 1:03d}"
            shown.append({"vid": vid, "lat": it["lat"], "lon": it["lon"], "outline": it["outline"], "photo": it["photo"],
                          "area_ha": it["area_ha"], "community": it["community"], "shown_type": t, "shown_components": [],
                          "shown_note": NOTES.get(g, ""), **({"accept": ACCEPT[t]} if t in ACCEPT else {})})
            key[vid] = {"item": i, "group": g, "shown": t}
            taken.add(i)
    rng = random.Random(20261126)
    rng.shuffle(shown)
    target.write_text(json.dumps({"queue": "prod_full_v24", "items": shown, "choices": list(LABELS),
                                  "labels": {**LABELS, **EXTRA_LABELS}, "traps": False, "owner_first": True},
                                 ensure_ascii=False), encoding="utf-8")
    (OUT / "verify" / f"{NAME}.key.json").write_text(json.dumps(
        {"groups": sampled, "v11_strata_sizes": bkey["strata_sizes"], "items": key}, indent=1), encoding="utf-8")
    print(json.dumps({"total": len(shown), "v1.1": len(base["items"]), "groups": sampled}))


# Addenda 6-7 (owner, after answering): comments saying the AI was right count as agreement, unsure comments as can't
# tell; MERGED also counts NON_FARM and NO_CROP as one class. All three scores are always reported.
AI_RIGHT = r"i (?:was|am) wrong|ai (?:is|si)\s+right|ai (?:isnt|isn't|is not|not|is nto) wrong"
UNSURE = r"maybe|not sure|mixed"
MODES = ("first_pick", "corrected", "merged")


def verdict(a, mode):
    """The owner's verdict on one parcel under a scoring mode."""
    import re
    v, note = a["verdict"], a.get("note") or ""
    if mode == "first_pick" or v != "disagree":
        return v
    if re.search(UNSURE, note, re.I):
        return "cannot_tell"
    if re.search(AI_RIGHT, note, re.I):
        return "agree"
    if mode == "merged":
        merge = lambda t: "NOT_IN_CROP_USE" if t in ("NON_FARM", "NO_CROP") else t
        ok = {merge(t) for t in ACCEPT.get(a["shown"], [a["shown"]])}
        return "agree" if merge(a["owner_type"]) in ok else "disagree"
    return v


def decide(right, decided, sampled):
    """Pre-registered bars (Addenda 3 and 5)."""
    if sampled >= 20:
        return "adopt" if right >= 17 else "more" if right == 16 else "reject"
    if sampled >= 15:
        return "adopt" if right >= 13 else "more" if right == 12 else "reject"
    return "adopt" if decided and right / decided >= .85 and decided - right <= 1 else "reject"


def scores():
    key = json.loads((OUT / "verify" / f"{NAME}.key.json").read_text(encoding="utf-8"))
    ans = json.loads((OUT / "answers" / f"verify_{NAME}__owner.json").read_text(encoding="utf-8"))
    out = {}
    for mode in MODES:
        res = {}
        for g in ("v1.1",) + GROUPS:
            vs = [verdict(ans[v], mode) for v, k in key["items"].items() if k["group"] == g and v in ans]
            dec = [v for v in vs if v != "cannot_tell"]
            right = sum(v == "agree" for v in dec)
            res[g] = {"right": right, "decided": len(dec), "range_95": wilson(right, len(dec))}
            if g != "v1.1" and key["groups"][g]["sampled"]:
                res[g]["decision"] = decide(right, len(dec), key["groups"][g]["sampled"])
        sizes = key["v11_strata_sizes"]
        tot, est, var, cov = sum(sizes.values()), 0.0, 0.0, 0.0
        for st, n in sizes.items():
            dec = [verdict(ans[v], mode) for v, k in key["items"].items() if k["group"] == "v1.1" and k.get("stratum") == st and v in ans]
            dec = [v for v in dec if v != "cannot_tell"]
            if dec:
                pr, w = sum(v == "agree" for v in dec) / len(dec), n / tot
                est, var, cov = est + w * pr, var + w * w * pr * (1 - pr) / max(len(dec) - 1, 1), cov + w
        acc, se = est / cov, var ** .5 / cov
        res["v1.1 weighted"] = {"accuracy": round(acc, 3), "range_95": [round(max(0, acc - 1.96 * se), 3), round(min(1, acc + 1.96 * se), 3)]}
        out[mode] = res
    return out


def score():
    print(json.dumps(scores(), indent=1))


MAIN = {"ORCHARD": "Perennial plantings", "VINEYARD": "Perennial plantings", "PERENNIAL": "Perennial plantings",
        "FIELD_CROP": "Field land", "NO_CROP": "Field land", "CROPLAND_UNCERTAIN": "Field land"}


def freeze():
    """Owner 2026-11-01 'ok freeze it': v1.2 = v1.1 + the groups adopted on the MERGED score (owner instruction).
    Written once to releases/landtypes_v1_2/ (read-only, manifest, frozen inputs); v1.1 is never edited."""
    import hashlib
    import shutil
    from datetime import datetime, timezone
    target = OUT / "releases" / "landtypes_v1_2"
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten.")
    sc = scores()
    adopted = [g for g in GROUPS if sc["merged"].get(g, {}).get("decision") == "adopt"]
    only_corrected = [g for g in adopted if sc["first_pick"][g].get("decision") != "adopt"]
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    o = pd.read_parquet(WORK / "overlay.parquet")
    o = o[o.group.isin(adopted)].set_index("item_id")
    d["type6_v11"], d["source_v11_frozen"] = d.type6, d.source_v11
    d["group_v12"] = d.item_id.map(o.group)
    new = d.item_id.map(o.v12_type)
    take = new.notna() & d.owner_label.isna() & (d.type6 == "UNCLASSIFIED")
    d.loc[take, "type6"] = new[take]
    d.loc[take, "source_v11"] = "v1.2 rule " + d.loc[take, "group_v12"]
    d.loc[~take, "group_v12"] = None
    d["main_class"] = d.type6.map(MAIN).fillna(d.type6)
    target.mkdir(parents=True)
    d.to_parquet(target / "parcels.parquet", index=False)
    d.to_csv(target / "parcels.csv", index=False, encoding="utf-8-sig")
    counts = lambda col: {str(k): {"parcels": int(len(g)), "ha": round(g.area_ha.sum(), 1)} for k, g in d.groupby(d[col].fillna("not classifiable"))}
    summary = {"version": "v1.2", "adopted_groups": adopted, "adopted_only_with_owner_corrections": only_corrected,
               "not_adopted": {g: sc["merged"][g].get("decision") for g in GROUPS if g not in adopted and sc["merged"][g].get("decision")},
               "parcels_changed_from_v11": int(take.sum()), "types": counts("type6"), "main_classes": counts("main_class"),
               "owner_check": sc, "scoring_note": "first_pick = the owner's locked first answer (lower bound); corrected = "
               "with the owner's comments after seeing the map (anchored, upper bound); merged = corrected with NON_FARM and "
               "NO_CROP as one class (owner instruction, Addendum 7). Groups adopted on merged as instructed."}
    (target / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    inputs = [RELEASE / "parcels.parquet", RELEASE / "manifest.json", WORK / "overlay.parquet", WORK / "candidates.parquet",
              WORK / "targets.json", WORK / "ai_targets.json", OUT / "verify" / f"{NAME}.json", OUT / "verify" / f"{NAME}.key.json",
              OUT / "answers" / f"verify_{NAME}__owner.json", OUT / "queues/v12_blocks.json", OUT / "queues/v12_blocks.key.json",
              OUT / "queues/v12_strips_pilot.key.json"]
    inputs += sorted((OUT / "answers").glob("v12_*__claude*.json")) + sorted((OUT / "prompts").glob("v12_*"))
    root = OUT.parents[2]
    inputs += [root / f for f in ("docs/ALPHAEARTH_V12_PREREGISTRATION_2026_10_31_EN.md", "scripts/alphaearth_landtypes_v12.py",
                                  "scripts/alphaearth_landtypes_v12_check.py", "scripts/alphaearth_photo_review.py",
                                  "scripts/alphaearth_landtypes_v11_merge.py", "wp_core/land_types.py")]
    sha = lambda f: hashlib.sha256(Path(f).read_bytes()).hexdigest()
    for f in inputs:
        rel = Path(f).relative_to(root)
        (target / "frozen" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target / "frozen" / rel)
    (target / "manifest.json").write_text(json.dumps({
        "version": "v1.2", "created": datetime.now(timezone.utc).isoformat(),
        "label": "AI-interpreted from imagery; owner labels applied; owner-checked sample; not field-verified",
        "inputs": {str(Path(f).relative_to(root)): sha(f) for f in inputs}, "parcels_sha256": sha(target / "parcels.parquet")},
        indent=1), encoding="utf-8")
    (target / "README.md").write_text(README_V12.format(created=datetime.now(timezone.utc).date(), n=len(d),
                                                       changed=int(take.sum()), adopted=", ".join(adopted)), encoding="utf-8")
    for f in target.rglob("*"):
        if f.is_file():
            f.chmod(0o444)
    print(json.dumps({k: summary[k] for k in ("adopted_groups", "adopted_only_with_owner_corrections", "not_adopted",
                                               "parcels_changed_from_v11", "main_classes")}, indent=1, default=str))


README_V12 = """# Land types v1.2 (frozen {created})

{n} parcels: v1.1 plus {changed} formerly unclassified parcels typed by the v1.2 groups that passed the owner's check
({adopted}). v1.1 (releases/landtypes_v1_1) is unchanged. AI-interpreted from imagery, owner labels applied, owner-checked
on a sample, not field-verified. Do not edit: a new version goes to a new folder; manifest.json holds the sha256 of
every input, frozen/ holds copies.

Columns: type6 (published v1.2 type, incl. PERENNIAL = perennial planting type not specified and CROPLAND_UNCERTAIN =
field land, 2025 use not specified), main_class (Perennial plantings / Field land / other), type6_v11, group_v12 (the
v1.2 rule that typed the parcel), owner_label, hint, source_v11, attrs_v11.

Accuracy (summary.json "owner_check"): three scores are always reported together - first pick (lower bound), corrected
by the owner's comments (anchored, upper bound) and merged (NON_FARM = NO_CROP). Rules and every post-hoc change:
docs/ALPHAEARTH_V12_PREREGISTRATION_2026_10_31_EN.md.
"""


if __name__ == "__main__":
    {"overlay": overlay, "page": page, "score": score, "freeze": freeze}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
