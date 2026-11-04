"""Land types v1.1 final merge, with the fixes from the independent audit (2026-10-31) and the owner's decisions.

  alphaearth_landtypes_v11_merge.py aux     -> writes the draft's helper files reproducibly (signal_disputed, straightness)
  alphaearth_landtypes_v11_merge.py merge   -> releases/landtypes_v1_1/ (parcels, summary, manifest)

Owner decisions: better UNCLASSIFIED than inaccurate (low-confidence answers and most-likely guesses become
UNCLASSIFIED, the guess kept as a hint); owner labels always win and are never touched by rules; road rules need
straightness; no further blind check: the owner's final check is an agree/disagree/comment page (anchored).
Audit fixes: candidate groups come from saved candidate reasons (controls are never adopted); a group's second-pass
answers are adopted only by a paired test (expert review: >= 5 wins, <= 1 loss where the answers differ); only medium/high answers without warnings can replace anything; tree belts need belt words or a strict
shape (width < 8 m and straightness < 0.6); scoring buckets by v1 status.
"""
from __future__ import annotations
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import (BELT, OUT, ROOT, TYPE6, owner6, owner_labels, perennial_pattern,  # noqa: E402
                                      signal_type)

DRAFT = OUT / "releases/landtypes_v1_1_draft"
SECOND = ("v11_second_pass", "v11_second_pass_b")
OLD_PHOTO = "structure seen 2021-22, current use not confirmed"
THIN_M = 15          # narrower strips have no clean 10 m Sentinel pixel (expert review)


def paired(pairs):
    """Expert review: (new, old, truth) per confident control; count only pairs where the answers differ.
    A group is adopted with >= 5 wins and <= 1 loss."""
    wins = sum(n != o and n == t for n, o, t in pairs)
    losses = sum(n != o and n != t for n, o, t in pairs)
    return wins, losses, wins >= 5 and losses <= 1


def thin_strip(width, source, converted_from, type6):
    """Expert review: signal-driven decisions are not used on strips narrower than THIN_M.
    Returns (new type6 or None, check name or None)."""
    if not width or width >= THIN_M or type6 == "UNCLASSIFIED":
        return None, None
    if source == "rule weak-spring":
        return "UNCLASSIFIED", "thin strip: weak-spring decision dropped"
    if converted_from == "NAT" and type6 != "NO_CROP":
        return "NO_CROP", "thin strip: crop-history conversion dropped"
    return None, None


def aux():
    """The helper files the merge needs, rebuilt from code (audit fix 7); existing files are compared, not overwritten."""
    import geopandas as gpd
    d = pd.read_parquet(DRAFT / "parcels.parquet")
    q = {i["id"]: i for i in json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))["items"]}
    dis = []
    for r in d[(d.status == "classified") & d.type.isin(["ANN", "ALF", "NCU", "GRS"]) & (d.source == "v1")].itertuples():
        h = q[r.item_id]["hints"]
        peak = max([v for v in (h["ndvi"].get("2025") or []) if v is not None], default=None)
        if signal_type(h.get("cuts", {}), h.get("green_days", {}), h.get("cycles", {}), peak)[0] != r.type6:
            dis.append(r.item_id)
    spec = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    live = d[d.status != "fixed_nci"].internal_parcel_id
    g = gpd.read_parquet(ROOT / spec).set_index("internal_parcel_id").loc[live].to_crs(32638).geometry
    straight = pd.DataFrame({"internal_parcel_id": live.values, "straight": (g.area / g.minimum_rotated_rectangle().area).values})
    for name, new in (("signal_disputed.json", json.dumps({"items": dis})), ("straightness.parquet", straight)):
        path = DRAFT / name
        if name.endswith(".json"):
            same = path.exists() and json.loads(path.read_text(encoding="utf-8"))["items"] == dis
            if not path.exists():
                path.write_text(new, encoding="utf-8")
        else:
            same = path.exists() and pd.read_parquet(path).set_index("internal_parcel_id").straight.round(6).equals(
                straight.set_index("internal_parcel_id").straight.round(6))
            if not path.exists():
                straight.to_parquet(path, index=False)
        print(name, "reproduced identically" if same else "written or DIFFERENT - check")


def is_belt(reason, width, straight):
    return bool(BELT.search(reason or "")) or ((width or 99) < 8 and (straight if straight is not None else 1) < .6)


def type6_of(final, row, straight):
    t = final["decision"]["type"]
    if t == "TRE":
        return "NON_FARM" if is_belt(final.get("reason"), row.mean_width_m, straight) else "ORCHARD"
    if final["decision"].get("attribute") == "uncertain_2025_activity":
        return "NO_CROP"
    return TYPE6.get(t)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def merge():
    target = (Path(sys.argv[3]) if "--dry" in sys.argv and len(sys.argv) > 3 else OUT / "releases" / "landtypes_v1_1")
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten.")
    d = pd.read_parquet(DRAFT / "parcels.parquet")
    straight = pd.read_parquet(DRAFT / "straightness.parquet").set_index("internal_parcel_id").straight
    signal = set(json.loads((DRAFT / "signal_disputed.json").read_text(encoding="utf-8"))["items"])
    q = {i["id"]: i for i in json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))["items"]}
    second, files = {}, []
    for queue in SECOND:
        for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
            files.append(path)
            for item_id, e in json.loads(path.read_text(encoding="utf-8")).items():
                if e.get("final") and not e["final"].get("defer"):
                    second.setdefault(item_id, e["final"])
    labels = owner_labels()

    # 1. candidate reasons from the draft (audit fix 1): controls that are not candidates are never adopted
    def reason(r):
        if r.status == "deferred":
            return "deferred"
        if r.source == "rule R1":
            return "R1 tree plot"
        if r.item_id in signal:
            return "signal-disputed"
        if r.confidence == "medium" and r.alphaearth_agrees is False:
            return "AlphaEarth-disputed medium"
        sh, h = json.loads(r.shares or "{}"), q[r.item_id]["hints"]
        if r.type == "ALF" and perennial_pattern(h.get("green_days", {}), h.get("cycles", {})) and sh.get("ORC", 0) + sh.get("TRE", 0) > 0:
            return "R4"
        return None
    d["candidate"] = [reason(r) if r.status != "fixed_nci" else None for r in d.itertuples()]

    # draft type with the stricter belt test (audit fix 3)
    def draft_type(r):
        if r.status == "classified" and r.type == "TRE":
            return "NON_FARM" if is_belt(r.reason, r.mean_width_m, straight.get(r.internal_parcel_id)) else "ORCHARD"
        return r.type6
    d["type6"] = [draft_type(r) for r in d.itertuples()]

    # 2. adoption per group on blind + anchored controls, with confident (m/h, no warning) second-pass answers only
    def usable(i):
        f = second.get(i)
        return f is not None and f.get("confidence") in ("high", "medium") and not f.get("contradictions")
    groups = {}
    for g in ("deferred", "R1 tree plot", "signal-disputed", "AlphaEarth-disputed medium", "R4"):
        rows = d[d.candidate == g]
        ctrl = rows[rows.internal_parcel_id.isin(labels)]
        conf = [r for r in ctrl.itertuples() if usable(r.item_id)]
        truth = lambda r: owner6(labels[r.internal_parcel_id][0])
        new_ok = sum(type6_of(second[r.item_id], r, straight.get(r.internal_parcel_id)) == truth(r) for r in conf)
        old_ok = sum(r.type6 == truth(r) for r in conf)
        # expert review: paired test on controls where the answers differ (the deferred draft is unclassified, so
        # every confident second-pass answer differs): adopt only with >= 5 wins and <= 1 loss
        wins, losses, adopt = paired([(type6_of(second[r.item_id], r, straight.get(r.internal_parcel_id)),
                                       "UNCLASSIFIED" if g == "deferred" else r.type6, truth(r)) for r in conf])
        groups[g] = {"parcels": len(rows), "controls": len(ctrl), "confident_controls": len(conf),
                     "second_pass_right": new_ok, "draft_right": old_ok, "paired_wins": wins, "paired_losses": losses,
                     "adopted": adopt}

    # 3. final type per parcel (model view), then owner rules, then owner labels win
    out = d.copy()
    out["type6_model"], out["hint"], out["source_v11"] = out.type6, None, out.source
    for idx, r in out.iterrows():
        if r.status == "fixed_nci":
            continue
        i, g = r.item_id, r.candidate
        if g and groups.get(g, {}).get("adopted") and usable(i):
            out.at[idx, "type6_model"] = type6_of(second[i], r, straight.get(r.internal_parcel_id))
            out.at[idx, "confidence"] = second[i]["confidence"]
            out.at[idx, "source_v11"] = f"second pass ({g})"
        elif r.confidence == "low":                    # owner: better unclassified than inaccurate
            guess = second.get(i)
            out.at[idx, "hint"] = (type6_of(guess, r, straight.get(r.internal_parcel_id)) if guess else r.type6)
            out.at[idx, "type6_model"] = "UNCLASSIFIED"
            out.at[idx, "source_v11"] = "unclassified (low confidence)"
    # fix 2 (expert review): perennials seen only on 2021-22 photos are marked "current use not confirmed". The
    # satellite signals cannot separate surviving from removed perennials (a green-season test failed 15 of 30 and a
    # crop-cycle test 19 of 30 owner-confirmed perennials), so the type is kept and the owner check samples them.
    # fix 3 (expert review): strips narrower than 15 m have no clean 10 m pixel: signal-driven decisions are not used
    checks = {"old-photo perennial, current use not confirmed": 0, "thin strip: weak-spring decision dropped": 0,
              "thin strip: crop-history conversion dropped": 0}
    for idx, r in out.iterrows():
        if r.status == "fixed_nci" or r.internal_parcel_id in labels:
            continue
        if (r.photo_current is False or r.photo_current == 0) and r.type6_model in ("VINEYARD", "ORCHARD"):
            out.at[idx, "attrs_v11"] = "; ".join(filter(None, [r.attrs_v11, OLD_PHOTO]))
            checks["old-photo perennial, current use not confirmed"] += 1
            continue
        new, check = thin_strip(r.mean_width_m, r.source, r.converted_from, r.type6_model)
        if new:
            if new == "UNCLASSIFIED":
                out.at[idx, "hint"] = r.type6_model
            out.at[idx, "type6_model"] = new
            out.at[idx, "source_v11"] = f"{check} (signal unreliable)"
            checks[check] += 1
        if (r.mean_width_m or 99) < THIN_M and "alfalfa" in (r.attrs_v11 or ""):
            out.at[idx, "attrs_v11"] = r.attrs_v11 + "; alfalfa signal unreliable (thin strip)"
    rules = {"curved long (straightness < 0.4)": 0, "very long (>= 60x, straightness < 0.6)": 0}
    for idx, r in out.iterrows():
        if r.status == "fixed_nci" or not r.mean_width_m or r.internal_parcel_id in labels or r.confidence == "high":
            continue
        ratio, s = r.area_ha * 1e4 / r.mean_width_m / r.mean_width_m, straight.get(r.internal_parcel_id, 1)
        kind = ("curved long (straightness < 0.4)" if ratio >= 10 and r.mean_width_m < 15 and s < .4
                and r.type6_model in ("FIELD_CROP", "ORCHARD", "VINEYARD")
                else "very long (>= 60x, straightness < 0.6)" if ratio >= 60 and r.mean_width_m < 12 and s < .6
                and r.type6_model not in ("NON_FARM",) else None)
        if kind:
            notes = [a for a in (r.attrs_v11 or "").split("; ") if a and a != OLD_PHOTO]
            if OLD_PHOTO in (r.attrs_v11 or ""):
                checks["old-photo perennial, current use not confirmed"] -= 1
            out.at[idx, "type6_model"] = "NON_FARM"
            out.at[idx, "source_v11"] = f"rule road ({kind})"
            out.at[idx, "attrs_v11"] = "; ".join(notes + ["likely road / track / canal (shape)"])
            rules[kind] += 1
    out["owner_label"] = out.internal_parcel_id.map(lambda p: owner6(labels[p][0]) if p in labels else None)
    out["type6"] = [o if o else m for o, m in zip(out.owner_label, out.type6_model)]   # published: owner wins

    # 4. scoring by v1 status (audit fix 8): precision of classified answers and coverage; in-sample
    def score(blind):
        res = {}
        for name, mask in (("v1 classified", out.status == "classified"), ("v1 deferred", out.status == "deferred")):
            sub = out[mask & out.internal_parcel_id.isin([p for p, (_, b) in labels.items() if b == blind])]
            typed = sub[sub.type6_model != "UNCLASSIFIED"]
            ok = int((typed.type6_model == typed.owner_label).sum())
            res[name] = {"labelled": len(sub), "classified": len(typed), "right": ok,
                         "precision": round(ok / len(typed), 3) if len(typed) else None,
                         "precision_95": wilson(ok, len(typed)),
                         "unclassified": int((sub.type6_model == "UNCLASSIFIED").sum())}
        return res
    target.mkdir(parents=True)
    out.to_parquet(target / "parcels.parquet", index=False)
    out.to_csv(target / "parcels.csv", index=False, encoding="utf-8-sig")
    live = out[out.status != "fixed_nci"]
    perennial = out[out.type6.isin(["VINEYARD", "ORCHARD"])]
    summary = {"coverage": {"parcels": len(out), "typed": int(live.type6.ne("UNCLASSIFIED").sum()),
                            "unclassified": int(live.type6.eq("UNCLASSIFIED").sum()),
                            "not_classifiable_from_imagery": int((out.status == "fixed_nci").sum()),
                            "ha": {"typed": round(live[live.type6 != "UNCLASSIFIED"].area_ha.sum(), 1),
                                   "unclassified": round(live[live.type6 == "UNCLASSIFIED"].area_ha.sum(), 1),
                                   "not_classifiable_from_imagery": round(out[out.status == "fixed_nci"].area_ha.sum(), 1)}},
               "perennials_by_photo": {t: {("current photo" if c else "2021-22 photo only"): {
                   "parcels": int(len(g)), "ha": round(g.area_ha.sum(), 1)}
                   for c, g in perennial[perennial.type6 == t].groupby(perennial.photo_current.fillna(False).astype(bool))}
                   for t in ("VINEYARD", "ORCHARD")},
               "groups": groups, "checks": checks, "road_rules": rules,
               "type6_published": out.type6.value_counts(dropna=False).to_dict(),
               "score_in_sample": {"note": "in-sample: these labels also guided the rules; 'blind' = owner labelled without "
                                           "seeing the AI answer but NOT a random sample (rare types oversampled); "
                                           "'anchored' = owner saw the AI answer. Neither is an accuracy of the map.",
                                   "blind_non_random": score(True), "anchored": score(False)}}
    (target / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    inputs = [DRAFT / "parcels.parquet", DRAFT / "signal_disputed.json", DRAFT / "straightness.parquet",
              OUT / "queues/prod_full_v24.json", Path(__file__), Path(__file__).with_name("alphaearth_landtypes_v11.py")] + files
    inputs += frozen_inputs()
    frozen = target / "frozen"
    for path in inputs:                # safety review: frozen copies of everything the release depends on
        rel = path.relative_to(ROOT) if str(path).startswith(str(ROOT)) else Path(path.name)
        (frozen / rel).parent.mkdir(parents=True, exist_ok=True)
        (frozen / rel).write_bytes(path.read_bytes())
    (target / "manifest.json").write_text(json.dumps({
        "version": "v1.1", "created": datetime.now(timezone.utc).isoformat(),
        "label": "AI-interpreted from imagery; owner labels applied; not field-verified; unaccepted candidate",
        "inputs": {str(p.relative_to(ROOT)) if str(p).startswith(str(ROOT)) else str(p): sha(p) for p in inputs},
        "parcels_sha256": sha(target / "parcels.parquet")}, indent=1), encoding="utf-8")
    (target / "README.md").write_text(README.format(created=datetime.now(timezone.utc).date(), n=len(out)), encoding="utf-8")
    for f in target.rglob("*"):         # frozen: read-only on disk (fix 4); versions are never overwritten
        if f.is_file():
            f.chmod(0o444)
    print(json.dumps(summary, indent=1, default=str))


README = """# Land types v1.1 (frozen {created})

{n} parcels. AI-interpreted from imagery, owner labels applied, not field-verified, unaccepted candidate.
Do not edit these files: a new version goes to a new folder. manifest.json holds the sha256 of every input,
parcels_sha256 the hash of parcels.parquet.

Columns: type6 (published: owner label if any, else the model), type6_model, owner_label, hint (the guess kept
when a parcel is UNCLASSIFIED), source_v11 (which rule or answer decided it), attrs_v11 (notes such as
"structure seen 2021-22, current use not confirmed" or "likely road / track / canal (shape)").

Rules: better UNCLASSIFIED than inaccurate; owner labels always win; second-pass groups adopted only with a paired
test (>= 5 wins, <= 1 loss on owner-labelled parcels where the answers differ); strips under 15 m do not use
satellite-signal decisions; road shape rules need straightness. summary.json: the counts of every rule and check.

Accuracy: the figures in summary.json are in-sample (the labels were used for tuning) and never an accuracy of the
map. The owner's final check (scripts/alphaearth_landtypes_v11_verify.py) is a stratified random sample of typed
parcels where the owner picks the type BEFORE seeing the map's answer; it gives the accuracy of typed parcels with
95% ranges. Always report coverage (typed / unclassified / not classifiable) and vineyards by photo age with it.
frozen/ holds copies of every input (owner-label sources, configs, code, reviewer prompts), hashed in manifest.json.
"""


def wilson(k, n, z=1.96):
    """95% Wilson interval for k of n."""
    if not n:
        return None
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** .5 / (1 + z * z / n)
    return [round(c - h, 3), round(c + h, 3)]


def frozen_inputs():
    """Owner-label sources, rules configs, shared code and reviewer instructions (safety review, recommendation 2)."""
    names = ["answers/blind_t300__owner.json", "answers/blind_alf_b1__owner.json", "answers/owner_prod_t100_v22__owner.json",
             "answers/verify_t100_v22__owner.json", "answers/verify_sonnet200__owner.json",
             "verify/t100_v22.json", "verify/t100_v22.key.json", "verify/sonnet200.json", "verify/sonnet200.key.json"]
    names += [f"queues/{q}.key.json" for q in ("blind_t300", "blind_alf_b1", "owner_prod_t100_v22", "prod_t100_v22",
                                               "prod_t1000", "v11_second_pass", "v11_second_pass_b")]
    names += [f"queues/{q}.json" for q in SECOND]
    files = [OUT / n for n in names] + sorted((OUT / "prompts").glob("*")) if (OUT / "prompts").exists() else [OUT / n for n in names]
    files += sorted((ROOT / "config").glob("alphaearth_types_20261030_*.json"))
    files += [ROOT / f for f in ("wp_core/land_types.py", "scripts/alphaearth_photo_review.py", "scripts/alphaearth_review_cli.py",
                                 "scripts/alphaearth_review_status.py", "scripts/alphaearth_landtypes_v11_verify.py")]
    files += [ROOT.parent / ".claude/agents/land-reviewer.md"]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        raise SystemExit(f"cannot freeze, missing inputs: {missing}")
    return files


if __name__ == "__main__":
    {"aux": aux, "merge": merge}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
