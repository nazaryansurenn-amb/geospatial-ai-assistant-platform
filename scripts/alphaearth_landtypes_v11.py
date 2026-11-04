"""Land types v1.1, deterministic part (owner request 2026-10-31, strategy steps 1-4a and 6; no AI, no tokens).

  alphaearth_landtypes_v11.py build      -> releases/landtypes_v1_1_draft/ (parcels, second-pass candidates, score)
  alphaearth_landtypes_v11.py score DIR  -> accuracy of a v1.1 table against the owner's labels (6 types)

Input is the frozen version 1 (releases/landtypes_v1/parcels.parquet) plus the queue evidence; v1 is never changed.
Six final types; the old codes stay as attributes. Deferred parcels get a most-likely type marked low confidence.
The AlphaEarth cross-check (nearest look-alikes by AlphaEarth group shares and stability, labelled by confident v1.1
answers) is only an audit flag: it never changes a type.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
V1 = OUT / "releases/landtypes_v1/parcels.parquet"
AE = ROOT / "data/analysis/alphaearth/alphaearth_groups_20261030_v1"
YEARS = ["2021", "2022", "2023", "2024", "2025"]
TYPE6 = {"ANN": "FIELD_CROP", "ALF": "FIELD_CROP", "MIX": "FIELD_CROP", "PART": "FIELD_CROP", "VIN": "VINEYARD",
         "ORC": "ORCHARD", "GRH": "GREENHOUSE", "BLT": "NON_FARM", "WAT": "NON_FARM",
         "NCU": "NO_CROP", "GRS": "NO_CROP", "NAT": "NO_CROP"}          # TRE is decided per parcel (belt or plot)
BELT = re.compile(r"belt|windbreak|tree line|line of trees|row along|along (the )?(road|canal|track|field|edge)|hedge", re.I)
TREEWORD = re.compile(r"orchard|tree|canopy|garden|sapling", re.I)
VINEWORD = re.compile(r"vine", re.I)


def alfalfa_signature(cuts, green):
    """Rules v2_3 signature: >= 3 cuts with >= 120 green days in >= 2 of 2023-2025."""
    return sum(1 for y in ("2023", "2024", "2025") if cuts.get(y, 0) >= 3 and green.get(y, 0) >= 120) >= 2


def perennial_pattern(green, cycles):
    """Strategy report: long green season (>= 150 days) in >= 3 years and no crop cycle in >= 3 years."""
    return sum(green.get(y, 0) >= 150 for y in YEARS) >= 3 and sum((cycles.get(y) or 0) == 0 for y in YEARS) >= 3


def is_belt(reason, area_ha, width):
    length = area_ha * 1e4 / max(width or 1, 1)
    return bool(BELT.search(reason or "")) or ((width or 99) < 15 and length / max(width or 1, 1) >= 6)


def signal_type(cuts, green, cycles, peak25):
    """Field land decided by the satellite signals (reproduces the AI's own field types 92.8% of the time)."""
    if alfalfa_signature(cuts, green):
        return "FIELD_CROP", "alfalfa"
    if (cycles.get("2025") or 0) >= 1 or (peak25 or 0) >= .4:
        return "FIELD_CROP", None
    return "NO_CROP", "field pattern"


def classify(row, ev):
    """One parcel -> (type6, attributes, confidence, source)."""
    cuts, green, cycles = json.loads(row.cuts), json.loads(row.green_days), json.loads(row.cycles)
    peak25, reason = ev.get("peak25"), row.reason or ""
    attrs = []
    if row.status == "classified":
        t = row.type
        if t == "TRE":
            belt = is_belt(reason, row.area_ha, row.mean_width_m)
            return ("NON_FARM", ["tree belt"], row.confidence, "v1") if belt else ("ORCHARD", ["tree plot (R1)"], row.confidence, "rule R1")
        if row.attribute == "uncertain_2025_activity":           # strategy: weak spring green-up is not a crop season
            return "NO_CROP", ["weak spring green-up"], row.confidence, "rule weak-spring"
        if t == "ALF":
            attrs.append("alfalfa" if alfalfa_signature(cuts, green) else "alfalfa, no cut signature")
        if t in ("NCU", "GRS", "NAT"):
            attrs.append({"NCU": "field pattern", "GRS": "grass / weeds", "NAT": "natural ground"}[t])
        if row.converted_from == "NAT":
            attrs.append("crop history 2021-2025")
        if t in ("BLT", "WAT"):
            attrs.append({"BLT": "buildings / yards / tracks", "WAT": "water"}[t])
        return TYPE6[t], attrs, row.confidence, "v1"
    # deferred -> most-likely type, low confidence
    photo = json.loads(row.photo_structure or "{}")
    trees = photo.get("ORC", 0) + photo.get("TRE", 0)
    if trees >= .5:
        return "ORCHARD", ["tree structure in photo (R2)"], "low", "most-likely R2"
    if photo:
        main = max(photo, key=photo.get)
        if photo[main] >= .5 and main in ("VIN", "GRH", "BLT", "WAT"):
            return {"VIN": "VINEYARD", "GRH": "GREENHOUSE", "BLT": "NON_FARM", "WAT": "NON_FARM"}[main], [], "low", "most-likely photo"
        if photo[main] >= .5 and main == "NAT":
            if any((cycles.get(y) or 0) >= 1 for y in YEARS):
                t, a = signal_type(cuts, green, cycles, peak25)
                return t, ["crop history 2021-2025"] + ([a] if a else []), "low", "most-likely photo+history"
            return "NO_CROP", ["natural ground"], "low", "most-likely photo"
    if TREEWORD.search(reason) and perennial_pattern(green, cycles):
        return "ORCHARD", ["tree reason + perennial pattern (R3)"], "low", "most-likely R3"
    if VINEWORD.search(reason) and not TREEWORD.search(reason) and perennial_pattern(green, cycles):
        return "VINEYARD", ["vine reason + perennial pattern"], "low", "most-likely reason"
    t, a = signal_type(cuts, green, cycles, peak25)
    return t, ([a] if a else []), "low", "most-likely signals"


def owner_labels():
    """internal_parcel_id -> (owner type, blind?) from every owner check so far."""
    def key(q):
        raw = json.loads((OUT / "queues" / f"{q}.key.json").read_text(encoding="utf-8"))
        return {k["id"]: k["internal_parcel_id"] for k in (raw["items"] if isinstance(raw, dict) else raw)}
    labels = {}
    for queue, file in (("blind_t300", "blind_t300__owner.json"), ("blind_alf_b1", "blind_alf_b1__owner.json"),
                        ("owner_prod_t100_v22", "owner_prod_t100_v22__owner.json")):
        ids = key(queue)
        for item, e in json.loads((OUT / "answers" / file).read_text(encoding="utf-8")).items():
            f = e.get("final") or {}
            if f and not f.get("defer"):
                labels[ids[item]] = (f["decision"]["type"], True)
    ids = {**key("prod_t100_v22"), **key("prod_t1000")}
    for vset in ("t100_v22", "sonnet200"):
        vkey = json.loads((OUT / "verify" / f"{vset}.key.json").read_text(encoding="utf-8"))["items"]
        shown = {i["vid"]: i["shown_type"] for i in json.loads((OUT / "verify" / f"{vset}.json").read_text(encoding="utf-8"))["items"]}
        path = OUT / "answers" / f"verify_{vset}__owner.json"
        for vid, o in (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}).items():
            pid = ids.get(vkey[vid]["item"])
            if pid is None or pid in labels:
                continue
            if o["verdict"] == "disagree" and o.get("correct"):
                labels[pid] = (o["correct"], True)          # the owner typed the correction after seeing a wrong answer
            elif o["verdict"] == "agree" and not vkey[vid]["trap"]:
                labels[pid] = (shown[vid], False)
    return labels


def owner6(t):
    return "ORCHARD" if t == "TRE" else TYPE6.get(t)


def score(table):
    labels = owner_labels()
    out = {}
    for blind in (True, False):
        sel = [(p, t) for p, (t, b) in labels.items() if b == blind]
        rows = table.set_index("internal_parcel_id").loc[[p for p, _ in sel if p in set(table.internal_parcel_id)]]
        res = {}
        for name, mask in (("all", rows.type6.notna()), ("v1 classified", rows.source != rows.source.where(rows.confidence == "low")),
                           ("most-likely (was deferred)", rows.confidence == "low")):
            sub = rows[mask]
            truth = [owner6(dict(sel)[p]) for p in sub.index]
            ok = int(sum(a == b for a, b in zip(sub.type6, truth)))
            res[name] = {"right": ok, "checked": len(sub), "rate": round(ok / len(sub), 3) if len(sub) else None}
        out["blind" if blind else "anchored"] = res
    return out


def alphaearth_flag(table):
    """Nearest look-alikes (AlphaEarth group shares + stability) labelled by confident v1.1 answers; audit only."""
    a = pd.read_parquet(AE / "parcels_2025/parcels.parquet", columns=["internal_parcel_id"] + [f"share_g{i}" for i in range(7)])
    s = pd.read_parquet(AE / "stability/parcels.parquet", columns=["internal_parcel_id", "stability_mean", "pixel_stability_mean"])
    f = a.merge(s, on="internal_parcel_id").set_index("internal_parcel_id").fillna(0)
    f = (f - f.mean()) / f.std().replace(0, 1)
    t = table.set_index("internal_parcel_id")
    ids = [p for p in t.index if p in f.index and pd.notna(t.at[p, "type6"])]
    ref = [p for p in ids if t.at[p, "confidence"] == "high"]
    X, R = f.loc[ids].to_numpy(np.float32), f.loc[ref].to_numpy(np.float32)
    ref_type = t.loc[ref, "type6"].to_numpy()
    votes = {}
    for start in range(0, len(ids), 2000):
        d = ((X[start:start + 2000, None, :] - R[None, :, :]) ** 2).sum(-1)
        nn = np.argsort(d, axis=1)[:, 1:16]                     # 15 nearest, skipping the parcel itself when present
        for k, row in zip(ids[start:start + 2000], nn):
            vals, counts = np.unique(ref_type[row], return_counts=True)
            votes[k] = (vals[counts.argmax()], counts.max() / 15)
    return votes


def build():
    target = OUT / "releases" / "landtypes_v1_1_draft"
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten.")
    v1 = pd.read_parquet(V1)
    queue = json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))
    peak = {i["id"]: max([v for v in (i["hints"]["ndvi"].get("2025") or []) if v is not None], default=None) for i in queue["items"]}
    rows = []
    for row in v1.itertuples(index=False):
        if row.status == "fixed_nci":
            rows.append({**row._asdict(), "type6": None, "attrs_v11": "not classifiable from imagery", "confidence_v11": None, "source": "fixed"})
            continue
        t6, attrs, conf, src = classify(row, {"peak25": peak.get(row.item_id)})
        rows.append({**row._asdict(), "type6": t6, "attrs_v11": "; ".join(attrs), "confidence_v11": conf, "source": src})
    table = pd.DataFrame(rows).rename(columns={"confidence": "confidence_v1"}).rename(columns={"confidence_v11": "confidence"})
    votes = alphaearth_flag(table)
    table["alphaearth_lookalike"] = table.internal_parcel_id.map(lambda p: votes.get(p, (None, None))[0])
    table["alphaearth_agrees"] = [None if v is None else v == t for v, t in zip(table.alphaearth_lookalike, table.type6)]
    # second-pass candidates: every most-likely parcel, orchard candidates, and medium answers AlphaEarth disputes
    green = table.green_days.map(lambda g: json.loads(g) if isinstance(g, str) else {})
    cyc = table.cycles.map(lambda g: json.loads(g) if isinstance(g, str) else {})
    shares = table.shares.map(lambda s: json.loads(s) if isinstance(s, str) else {})
    r4 = (table.type == "ALF") & [perennial_pattern(g, c) and (s.get("ORC", 0) + s.get("TRE", 0)) > 0 for g, c, s in zip(green, cyc, shares)]
    cand = table[(table.confidence == "low") | (table.source == "rule R1") | r4 |
                 ((table.confidence == "medium") & (table.alphaearth_agrees == False))]  # noqa: E712
    target.mkdir(parents=True)
    table.to_parquet(target / "parcels.parquet", index=False)
    table.to_csv(target / "parcels.csv", index=False, encoding="utf-8-sig")
    reasons = {"most-likely (was deferred)": int((cand.confidence == "low").sum()), "R1 tree plot -> orchard": int((cand.source == "rule R1").sum()),
               "R4 alfalfa + perennial + trees": int(r4.sum()),
               "medium answer, AlphaEarth disagrees": int(((cand.confidence == "medium") & (cand.alphaearth_agrees == False)).sum())}  # noqa: E712
    (target / "second_pass_candidates.json").write_text(json.dumps({"reasons": reasons, "items": cand.item_id.tolist()}), encoding="utf-8")
    sc = score(table)
    (target / "score.json").write_text(json.dumps(sc, indent=1), encoding="utf-8")
    summary = {"type6": table.type6.value_counts(dropna=False).to_dict(), "confidence": table.confidence.value_counts(dropna=False).to_dict(),
               "sources": table.source.value_counts().to_dict(), "second_pass": {"total": len(cand), **reasons},
               "alphaearth_agrees_share": round(float(table.alphaearth_agrees.dropna().mean()), 3), "score": sc}
    (target / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1, default=str))


def second_pass_type(final, row):
    """A second-pass final answer -> 6-type code (same mapping as v1, TRE split by belt test)."""
    t = final["decision"]["type"]
    if t == "TRE":
        return "NON_FARM" if is_belt(final.get("reason"), row.area_ha, row.mean_width_m) else "ORCHARD"
    if final["decision"].get("attribute") == "uncertain_2025_activity":
        return "NO_CROP"
    return TYPE6.get(t)


def merge(queues=("v11_second_pass", "v11_second_pass_b")):
    """Final v1.1: per candidate group, adopt the second-pass answers only where they beat (or tie) the deterministic
    draft on the owner-labelled hidden controls; everything else keeps the draft. Writes releases/landtypes_v1_1/."""
    draft = pd.read_parquet(OUT / "releases/landtypes_v1_1_draft/parcels.parquet")
    target = OUT / "releases" / "landtypes_v1_1"
    if target.exists():
        raise SystemExit(f"{target} exists; versions are never overwritten.")
    second = {}
    for queue in queues:
        for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
            for item_id, e in json.loads(path.read_text(encoding="utf-8")).items():
                if e.get("final") and not e["final"].get("defer"):
                    second.setdefault(item_id, e["final"])
    signal = set(json.loads((OUT / "releases/landtypes_v1_1_draft/signal_disputed.json").read_text(encoding="utf-8"))["items"])
    labels = owner_labels()
    d = draft.set_index("item_id")
    group = lambda r: ("deferred" if r.confidence == "low" else "R1 tree plot" if r.source == "rule R1"
                       else "signal-disputed" if r.name in signal
                       else "AlphaEarth-disputed medium" if r.confidence == "medium" and r.alphaearth_agrees is False
                       else "R4" if r.type == "ALF" else "control only")
    report, adopt = {}, {}
    for g in ("deferred", "R1 tree plot", "signal-disputed", "AlphaEarth-disputed medium", "R4", "control only"):
        rows = [(i, r) for i, r in d.iterrows() if i in second and group(r) == g]
        ctrl = [(i, r) for i, r in rows if r.internal_parcel_id in labels]
        truth = {i: owner6(labels[r.internal_parcel_id][0]) for i, r in ctrl}
        old_ok = sum(r.type6 == truth[i] for i, r in ctrl)
        new_ok = sum(second_pass_type(second[i], r) == truth[i] for i, r in ctrl)
        by_conf = {}
        for c in ("high", "medium", "low"):
            sel = [(i, r) for i, r in ctrl if second[i].get("confidence") == c]
            by_conf[c] = f"{sum(second_pass_type(second[i], r) == truth[i] for i, r in sel)}/{len(sel)}"
        adopt[g] = g != "control only" and len(ctrl) > 0 and new_ok >= old_ok
        report[g] = {"answered": len(rows), "controls": len(ctrl), "draft_right": old_ok, "second_pass_right": new_ok,
                     "second_pass_by_confidence": by_conf, "adopted": adopt[g]}
    final = draft.copy()
    for idx, r in final.iterrows():
        i = r.item_id
        if i in second and adopt.get(group(d.loc[i])):
            new = second_pass_type(second[i], r)
            final.at[idx, "type6"] = new
            final.at[idx, "source"] = f"second pass ({group(d.loc[i])})"
            final.at[idx, "confidence"] = second[i].get("confidence")
            final.at[idx, "attrs_v11"] = "; ".join(filter(None, [r.attrs_v11, f"second pass: {second[i]['decision']['type']}"]))
    # Owner rule (2026-10-31): very long, narrow parcels are roads, tracks or canals. Calibrated on owner labels:
    # length/width >= 60 -> 5 of 6 were BLT; 40-60 -> 0 of 5. A high-confidence AI answer is kept.
    # Second owner rule: field strips are straight rectangles, roads/tracks/canals bend. Straightness = area / area of
    # the tightest rotated rectangle (owner labels: roads median 0.23, crop/perennial strips 0.92-0.95). Long (>= 10x)
    # narrow (< 15 m) parcels under 0.4 that are typed crop/orchard/vineyard become NON_FARM; natural ground stays.
    straight = pd.read_parquet(OUT / "releases/landtypes_v1_1_draft/straightness.parquet").set_index("internal_parcel_id").straight
    linear = curved = 0
    for idx, r in final.iterrows():
        if r.status == "fixed_nci" or not r.mean_width_m:
            continue
        length = r.area_ha * 1e4 / r.mean_width_m
        if (length / r.mean_width_m >= 10 and r.mean_width_m < 15 and straight.get(r.internal_parcel_id, 1) < .4
                and r.type6 in ("FIELD_CROP", "ORCHARD", "VINEYARD") and r.confidence != "high"):
            final.at[idx, "type6"] = "NON_FARM"
            final.at[idx, "source"] = "rule curved linear feature"
            final.at[idx, "attrs_v11"] = "; ".join(filter(None, [r.attrs_v11, "likely road / track / canal (curved shape)"]))
            curved += 1
            continue
        if r.mean_width_m < 12 and length / r.mean_width_m >= 60 and r.type6 != "NON_FARM" and r.confidence != "high":
            final.at[idx, "type6"] = "NON_FARM"
            final.at[idx, "source"] = "rule linear feature"
            final.at[idx, "attrs_v11"] = "; ".join(filter(None, [r.attrs_v11, "likely road / track / canal (shape)"]))
            linear += 1
    report["linear feature rules"] = {"very long (>= 60x)": linear, "curved long (straightness < 0.4)": curved}
    target.mkdir(parents=True)
    final.to_parquet(target / "parcels.parquet", index=False)
    final.to_csv(target / "parcels.csv", index=False, encoding="utf-8-sig")
    summary = {"groups": report, "type6": final.type6.value_counts(dropna=False).to_dict(),
               "confidence": final.confidence.value_counts(dropna=False).to_dict(), "score": score(final)}
    (target / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
    elif sys.argv[1:2] == ["merge"]:  # superseded after the independent audit (R4 grouping bug, too few controls)
        raise SystemExit("Use scripts/alphaearth_landtypes_v11_merge.py merge (audited merge).")
    elif sys.argv[1:2] == ["score"] and len(sys.argv) == 3:
        print(json.dumps(score(pd.read_parquet(Path(sys.argv[2]) / "parcels.parquet")), indent=1))
    else:
        raise SystemExit(__doc__)
