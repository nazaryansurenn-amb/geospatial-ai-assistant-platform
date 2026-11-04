"""Land types v1.2 (owner decision 2026-10-31): neighbour help on top of the frozen v1.1, as pre-registered in
docs/ALPHAEARTH_V12_PREREGISTRATION_2026_10_31_EN.md. Never edits v1.1.

  alphaearth_landtypes_v12.py targets   -> v12/targets.json (Group A fired parcels, Group B targets, adjacency)
  alphaearth_landtypes_v12.py candidates -> v12/candidates.parquet: the free rule groups C, D, A1, A2, P, K (Addendum 3)
                                           and the AI target lists B, W, B-fix, S; candidates only, nothing adopted
  alphaearth_landtypes_v12.py blocks    -> queue v12_blocks: B + W + B-fix targets in blocks of adjoining targets, with
                                           owner-labelled neighbours as hidden controls (run plan step 1)
  alphaearth_landtypes_v12.py strips-pilot -> queue v12_strips_pilot: 60 random S strips (seed 20261118) + hidden
                                           owner-labelled strips; 'continues / differs / unclear' reasons (step 2)
  alphaearth_landtypes_v12.py strips    -> queue v12_strips: all remaining S strips (only after the pilot's go)
  alphaearth_landtypes_v12.py pilot     -> queue v12_block_pilot: 10 blocks (3 owner cases + 4 + 3, seed 20261113),
                                           preferring blocks that hold owner-labelled parcels (hidden controls)

Blocks reuse the production items of prod_full_v24 (same item ids and signals); reviewers never see v1.1 types,
hints, neighbour votes or owner labels.
"""
from __future__ import annotations
import collections
import glob
import json
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_landtypes_v11 import OUT, ROOT, owner_labels  # noqa: E402
from alphaearth_landtypes_v11_merge import is_belt, type6_of  # noqa: E402

RELEASE = OUT / "releases/landtypes_v1_1"
WORK = OUT / "v12"
SIX = {"FIELD_CROP", "NO_CROP", "VINEYARD", "ORCHARD", "GREENHOUSE", "NON_FARM"}
PERENNIAL = {"VINEYARD", "ORCHARD"}
CASES = {"case 1": None, "case 2": None, "case 3": "f12163"}       # cases 1-2 are found by their pattern below
WINDOW_M, MAX_PARCELS = 350, 15   # the block view zooms out to fit (zoom 18 ~0.5 m/px: vine rows visible)


def adjacency(ids):
    """internal id -> [(neighbour, shared boundary m)] for pairs sharing >= 10 m."""
    import geopandas as gpd
    spec = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    g = gpd.read_parquet(ROOT / spec)[["internal_parcel_id", "geometry"]]
    g = g[g.internal_parcel_id.isin(ids)].to_crs(32638).reset_index(drop=True)
    buf = g.copy()
    buf["geometry"] = g.buffer(1.0)
    pairs = gpd.sjoin(buf, buf, predicate="intersects")
    pairs = pairs[pairs.index < pairs.index_right]
    nb = collections.defaultdict(list)
    for a, b in zip(pairs.index, pairs.index_right):
        s = buf.geometry[a].intersection(buf.geometry[b]).area / 2.0   # 2 m wide overlap strip -> shared length
        if s >= 10:
            pa, pb = g.internal_parcel_id[a], g.internal_parcel_id[b]
            nb[pa].append((pb, round(s, 1)))
            nb[pb].append((pa, round(s, 1)))
    return nb


def second_pass():
    sec = {}
    for q in ("v11_second_pass", "v11_second_pass_b"):
        for p in glob.glob(str(OUT / "answers" / f"{q}__claude*.json")):
            for k, e in json.loads(Path(p).read_text(encoding="utf-8")).items():
                if e.get("final") and not e["final"].get("defer"):
                    sec[k] = e["final"]
    return sec


def targets():
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    labels = owner_labels()
    row = d.set_index("internal_parcel_id")
    nb = adjacency(d.internal_parcel_id.tolist())
    sec = second_pass()
    straight = pd.read_parquet(OUT / "releases/landtypes_v1_1_draft/straightness.parquet").set_index("internal_parcel_id").straight

    def confident(p):
        r = row.loc[p]
        return r.type6_model in SIX and r.confidence in ("high", "medium") and r.source_v11 == "v1"

    def votes(p):
        w = collections.Counter()
        for q, s in nb.get(p, []):
            if confident(q):
                w[row.loc[q].type6_model] += s
        return w, sum(1 for q, _ in nb.get(p, []) if confident(q))

    group_a, group_b = [], {}
    for r in d.itertuples():
        if r.type6 != "UNCLASSIFIED" or r.internal_parcel_id in labels:
            continue
        w, n = votes(r.internal_parcel_id)
        tot = sum(w.values())
        f = sec.get(r.item_id)
        sp = type6_of(f, r, straight.get(r.internal_parcel_id)) if f else None
        if (f and f.get("confidence") in ("medium", "high") and not f.get("contradictions") and sp in ("FIELD_CROP", "NO_CROP", "NON_FARM")
                and n >= 2 and tot and w[sp] / tot >= .6):
            group_a.append({"item": r.item_id, "type": sp, "share": round(w[sp] / tot, 2), "neighbours": n,
                            "thin": bool((r.mean_width_m or 99) < 15)})
        if tot and (w["VINEYARD"] + w["ORCHARD"]) / tot >= .5:
            group_b[r.internal_parcel_id] = "unclassified next to perennials"
    pairs = set()
    for p, lst in nb.items():
        for q, s in lst:
            if s >= 30 and {row.loc[p].type6, row.loc[q].type6} == PERENNIAL:
                pairs.add(tuple(sorted((p, q))))
    for p, q in pairs:
        group_b.setdefault(p, "vineyard-orchard pair")
        group_b.setdefault(q, "vineyard-orchard pair")
    # owner cases: 1 = an orchard parcel with >= 3 vineyard neighbours (one plantation split); 2 = a vineyard strip with
    # >= 2 unclassified neighbours whose hint is vineyard; 3 = f12163
    case1 = sorted((p for p in row.index if row.loc[p].type6 == "ORCHARD"
                    and sum(row.loc[q].type6 == "VINEYARD" and s >= 30 for q, s in nb.get(p, [])) >= 3),
                   key=lambda p: -row.loc[p].area_ha)
    case2 = sorted((p for p in row.index if row.loc[p].type6 == "VINEYARD" and (row.loc[p].mean_width_m or 99) < 25
                    and sum(row.loc[q].type6 == "UNCLASSIFIED" and row.loc[q].hint == "VINEYARD" for q, _ in nb.get(p, [])) >= 2),
                   key=lambda p: -sum(row.loc[q].type6 == "UNCLASSIFIED" for q, _ in nb.get(p, [])))
    case3 = d[d.item_id == CASES["case 3"]].internal_parcel_id.tolist()
    WORK.mkdir(exist_ok=True)
    out = {"group_a": group_a, "group_b": group_b, "vin_orc_pairs": sorted(pairs),
           "cases": {"case 1": [row.loc[p].item_id for p in case1[:5]], "case 2": [row.loc[p].item_id for p in case2[:5]],
                     "case 3": [row.loc[p].item_id for p in case3]},
           "adjacency": {p: lst for p, lst in nb.items()}}
    (WORK / "targets.json").write_text(json.dumps(out), encoding="utf-8")
    print(json.dumps({"group_a": len(group_a), "group_a_types": collections.Counter(a["type"] for a in group_a),
                      "group_a_thin": sum(a["thin"] for a in group_a), "group_b_targets": len(group_b),
                      "group_b_reasons": collections.Counter(group_b.values()), "vin_orc_pairs": len(pairs),
                      "case candidates": out["cases"]}, default=str))


def block_for(seed, nb, items, labelled_ids):
    """The seed plus its adjoining parcels (longest shared edge first) while they fit one window and the cap."""
    def fits(ids):
        b = [items[i]["metric"]["bounds"] for i in ids]
        return (max(x[2] for x in b) - min(x[0] for x in b) <= WINDOW_M and max(x[3] for x in b) - min(x[1] for x in b) <= WINDOW_M)
    chosen = [seed]
    if not fits(chosen):
        return None
    for q, _ in sorted(nb.get(seed, []), key=lambda x: -x[1]):
        if q in items and q not in chosen and len(chosen) < MAX_PARCELS and fits(chosen + [q]):
            chosen.append(q)
    return chosen


def pilot(seed=20261113):
    name = "v12_block_pilot"
    if (OUT / "queues" / f"{name}.json").exists():
        raise SystemExit("Queue already exists; it is never overwritten.")
    t = json.loads((WORK / "targets.json").read_text(encoding="utf-8"))
    nb = {p: [tuple(x) for x in lst] for p, lst in t["adjacency"].items()}
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    pid_of = dict(zip(d.item_id, d.internal_parcel_id))
    src = json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))
    key = {k["id"]: k for k in json.loads((OUT / "queues/prod_full_v24.key.json").read_text(encoding="utf-8"))["items"]}
    items = {pid_of[i["id"]]: i for i in src["items"] if i["id"] in pid_of}      # reviewable parcels only (no fixed NCI)
    labelled = set(owner_labels())
    rng = random.Random(seed)
    used, blocks = set(), []

    def add(seed_pid, reason):
        if seed_pid in used or seed_pid not in items:
            return False
        ids = block_for(seed_pid, nb, items, labelled)
        if not ids or len(ids) < 2 or used & set(ids):
            return False
        used.update(ids)
        blocks.append({"id": f"V{len(blocks) + 1:03d}", "items": [items[p]["id"] for p in ids], "reason": reason,
                       "seed": items[seed_pid]["id"], "controls": [items[p]["id"] for p in ids if p in labelled]})
        return True

    for case, cands in t["cases"].items():
        for item in cands:
            if add(pid_of[item], case):
                break
    unc = sorted(p for p, r in t["group_b"].items() if r == "unclassified next to perennials")
    pairs = sorted({p for pair in t["vin_orc_pairs"] for p in pair[:1]})
    near_label = {p for p in items if any(q in labelled for q, _ in nb.get(p, []))}
    for pool, n, reason in ((unc, 4, "unclassified next to perennials"), (pairs, 3, "vineyard-orchard pair")):
        pool = pool[:]
        rng.shuffle(pool)
        pool.sort(key=lambda p: p not in near_label)          # blocks with hidden controls first (stable: random within)
        k = 0
        for p in pool:
            if k >= n:
                break
            k += add(p, reason)
    chosen = [items[p] for p in used]
    rng.shuffle(chosen)
    data = {"queue": name, "rules": src["rules"], "forced_choice": True, "block_review": True,
            "items": chosen, "fixed_status": [], "blocks": [{"id": b["id"], "items": b["items"]} for b in blocks]}
    (OUT / "queues" / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (OUT / "queues" / f"{name}.key.json").write_text(json.dumps(
        {"items": [key[i["id"]] for i in chosen], "blocks": blocks, "seed": seed,
         "preregistration": "docs/ALPHAEARTH_V12_PREREGISTRATION_2026_10_31_EN.md"}), encoding="utf-8")
    print(json.dumps({"blocks": [(b["id"], b["reason"], len(b["items"]), len(b["controls"])) for b in blocks],
                      "parcels": len(chosen)}))


# Addendum 3 dilemma patterns, matched on the v1 reviewer's reason (fixed before any owner answer)
ALF_ANN = r"\b(?:alf\w*|alfalfa)\b.*\b(?:ann|annual|crop|ploughed)\b|\b(?:ann|annual|crop)\b.*\balf"
TREES = (r"(?:orchard|fruit|garden).*(?:non-fruit|woodlot|woodland|tree cover|tree belt|windbreak|\bTRE\b)"
         r"|(?:non-fruit|woodlot|woodland|tree cover|\bTRE\b).*(?:orchard|fruit)")
VIN_ORC = r"(?:vineyard|vines?\b).*(?:orchard|trees?\b)|(?:orchard|trees?\b).*(?:vineyard|vines?\b)"
CROP_FALLOW = (r"(?:crop|\bann\w*|cultivat\w*).*(?:fallow|grass|weed|bare|no crop|\bncu\b|not cultivated|stubble)"
               r"|(?:fallow|grass|weed|bare|\bncu\b|stubble).*(?:crop|\bann\w*|cultivat\w*)")
NOT_FIELD = r"tree|vine|orchard|build|house|yard|greenhouse"


def signal_owned(strip, neighbour):
    """Addendum 3 guard: True when a strip's 2025 monthly NDVI is the neighbour's (r >= 0.95 and mean |diff| <= 0.05)."""
    pairs = [(a, b) for a, b in zip(strip or [], neighbour or []) if a is not None and b is not None]
    if len(pairs) < 8:
        return False
    xs, ys = zip(*pairs)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = sum((x - mx) ** 2 for x in xs) ** .5
    sy = sum((y - my) ** 2 for y in ys) ** .5
    r = sum((x - mx) * (y - my) for x, y in pairs) / (sx * sy) if sx and sy else 0
    return r >= .95 and sum(abs(x - y) for x, y in pairs) / len(pairs) <= .05


def first_group(flags):
    """The Addendum 3 priority order: a parcel belongs to the first group whose rule fires."""
    return next((g for g in ("C", "D", "A1", "A2", "P", "K") if flags.get(g)), None)


def candidates():
    import re
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    labels = owner_labels()
    row = d.set_index("internal_parcel_id")
    t = json.loads((WORK / "targets.json").read_text(encoding="utf-8"))
    nb = {p: [tuple(x) for x in lst] for p, lst in t["adjacency"].items()}
    sec = second_pass()
    straight = pd.read_parquet(OUT / "releases/landtypes_v1_1_draft/straightness.parquet").set_index("internal_parcel_id").straight
    q = {i["id"]: i for i in json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))["items"]}
    pid_item = dict(zip(d.internal_parcel_id, d.item_id))
    ndvi = lambda item: (q[item]["hints"]["ndvi"].get("2025") if item in q else None)
    group_a = {a["item"]: a["type"] for a in t["group_a"]}

    def confident(p):
        r = row.loc[p]
        return r.type6_model in SIX and r.confidence in ("high", "medium") and r.source_v11 == "v1"

    def boundary_share(p, types):
        w = collections.Counter()
        for n, s_ in nb.get(p, []):
            w[row.loc[n].type6 if isinstance(row.loc[n].type6, str) else "NCI"] += s_
        tot = sum(w.values())
        return sum(w[x] for x in types) / tot if tot else 0

    out = []
    for r in d[(d.type6 == "UNCLASSIFIED") & ~d.internal_parcel_id.isin(labels)].itertuples():
        reason, f = r.reason or "", sec.get(r.item_id) or {}
        shares = f.get("shares") or json.loads(r.shares or "{}")
        thin = (r.mean_width_m or 99) < 15
        owned = thin and any(signal_owned(ndvi(r.item_id), ndvi(pid_item[n])) for n, _ in nb.get(r.internal_parcel_id, []) if confident(n))
        peak = max([v for v in (ndvi(r.item_id) or []) if v is not None], default=0)
        flags = {
            "C": bool(re.search(ALF_ANN, reason, re.I)) and peak >= .5 and not thin and not owned
                 and r.hint not in PERENNIAL and boundary_share(r.internal_parcel_id, {"VINEYARD"}) < .3,
            "D": bool(re.search(TREES, reason, re.I)) and shares.get("ORC", 0) + shares.get("TRE", 0) >= .5
                 and not is_belt(reason, r.mean_width_m, straight.get(r.internal_parcel_id)),
            "A1": group_a.get(r.item_id) == "FIELD_CROP" and not owned,
            "A2": group_a.get(r.item_id) in ("NO_CROP", "NON_FARM") and not owned,
            "P": shares.get("VIN", 0) + shares.get("ORC", 0) >= .6 or bool(re.search(VIN_ORC, reason, re.I)),
            "K": r.hint in ("FIELD_CROP", "NO_CROP") and bool(re.search(CROP_FALLOW, reason, re.I))
                 and not re.search(NOT_FIELD, reason, re.I)}
        g = first_group(flags)
        if g:
            new = {"C": "FIELD_CROP", "D": "ORCHARD", "A1": "FIELD_CROP", "A2": group_a.get(r.item_id),
                   "P": "PERENNIAL", "K": "CROPLAND_UNCERTAIN"}[g]
            out.append({"item_id": r.item_id, "internal_parcel_id": r.internal_parcel_id, "group": g, "v12_candidate": new,
                        "area_ha": r.area_ha, "thin": thin, "signal_owned": owned, "hint": r.hint})
    cand = pd.DataFrame(out)
    # AI target lists (run later, after the usage reset)
    unc = set(d[(d.type6 == "UNCLASSIFIED") & ~d.internal_parcel_id.isin(labels)].internal_parcel_id) - set(cand.internal_parcel_id)
    b = {p for p in t["group_b"] if p in unc or row.loc[p].type6 in PERENNIAL}
    w = {p for p in unc if row.loc[p].hint in PERENNIAL and row.loc[p].photo_current in (True, 1)} - b
    bfix = {p for p in row.index if row.loc[p].type6 == "FIELD_CROP" and row.loc[p].source_v11 == "v1" and p not in labels
            and boundary_share(p, {"VINEYARD"}) >= .3}
    strips = {p for p in unc - b - w if (row.loc[p].mean_width_m or 99) < 15}
    WORK.mkdir(exist_ok=True)
    target = WORK / "candidates.parquet"
    if target.exists():
        raise SystemExit(f"{target} exists; candidate lists are never overwritten.")
    cand.to_parquet(target, index=False)
    lists = {"B": sorted(b), "W": sorted(w), "B-fix": sorted(bfix), "S": sorted(strips)}
    (WORK / "ai_targets.json").write_text(json.dumps(lists), encoding="utf-8")
    summary = {"unclassified": int((d.type6 == "UNCLASSIFIED").sum()),
               "rule_groups": {g: {"parcels": int((cand.group == g).sum()), "ha": round(cand[cand.group == g].area_ha.sum(), 1)}
                               for g in ("C", "D", "A1", "A2", "P", "K")},
               "signal_owned_strips_blocked": int(cand.signal_owned.sum()),
               "ai_targets": {k: len(v) for k, v in lists.items()},
               "left_after_rules_and_ai_lists": len(unc - b - w - strips)}
    (WORK / "candidates_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


def _queue_context():
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    pid_of = dict(zip(d.item_id, d.internal_parcel_id))
    src = json.loads((OUT / "queues/prod_full_v24.json").read_text(encoding="utf-8"))
    key = {k["id"]: k for k in json.loads((OUT / "queues/prod_full_v24.key.json").read_text(encoding="utf-8"))["items"]}
    items = {pid_of[i["id"]]: i for i in src["items"] if i["id"] in pid_of}      # reviewable parcels only
    t = json.loads((WORK / "targets.json").read_text(encoding="utf-8"))
    nb = {p: [tuple(x) for x in lst] for p, lst in t["adjacency"].items()}
    return d, src, key, items, nb


def _write(name, chosen, blocks, key, src, extra):
    if (OUT / "queues" / f"{name}.json").exists():
        raise SystemExit("Queue already exists; it is never overwritten.")
    data = {"queue": name, "rules": src["rules"], "forced_choice": True, "items": chosen, "fixed_status": []}
    if blocks is not None:
        data["blocks"] = [{"id": b["id"], "items": b["items"]} for b in blocks]
    (OUT / "queues" / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (OUT / "queues" / f"{name}.key.json").write_text(json.dumps(
        {"items": [key[i["id"]] for i in chosen], "blocks": blocks, **extra,
         "preregistration": "docs/ALPHAEARTH_V12_PREREGISTRATION_2026_10_31_EN.md"}), encoding="utf-8")


def blocks(seed=20261115):
    """Run plan step 1: one block per group of adjoining targets; owner-labelled neighbours join as hidden controls."""
    d, src, key, items, nb = _queue_context()
    ai = json.loads((WORK / "ai_targets.json").read_text(encoding="utf-8"))
    role = {}
    for g in ("B", "W", "B-fix"):
        for p in ai[g]:
            if p in items:
                role.setdefault(p, g)
    labelled = set(owner_labels())
    rng = random.Random(seed)
    controls = sorted({q for p in role for q, _ in nb.get(p, []) if q in labelled and q in items and q not in role})
    rng.shuffle(controls)
    controls = controls[:max(15, round(.05 * len(role)))]
    for c in controls:
        role[c] = "control"
    members = set(role)

    def fits(ids):
        b = [items[i]["metric"]["bounds"] for i in ids]
        return max(x[2] for x in b) - min(x[0] for x in b) <= WINDOW_M and max(x[3] for x in b) - min(x[1] for x in b) <= WINDOW_M
    order = sorted(members, key=lambda p: (role[p] == "control", -items[p]["area_ha"]))
    used, out = set(), []
    for seed_p in order:
        if seed_p in used or not fits([seed_p]):
            continue
        chosen, frontier = [seed_p], [seed_p]
        while frontier and len(chosen) < MAX_PARCELS:
            cur = frontier.pop(0)
            for q, _ in sorted(nb.get(cur, []), key=lambda x: -x[1]):
                if q in members and q not in used and q not in chosen and len(chosen) < MAX_PARCELS and fits(chosen + [q]):
                    chosen.append(q)
                    frontier.append(q)
        if all(role[p] == "control" for p in chosen):
            continue                                    # a block of controls only would reveal them
        used.update(chosen)
        out.append({"id": f"K{len(out) + 1:04d}", "items": [items[p]["id"] for p in chosen],
                    "roles": {items[p]["id"]: role[p] for p in chosen}})
    chosen_items = [items[p] for p in used]
    rng.shuffle(chosen_items)
    _write("v12_blocks", chosen_items, out, key, src, {"seed": seed, "too_large_for_a_block": sorted(
        items[p]["id"] for p in members - used)})
    roles = collections.Counter(r for b in out for r in b["roles"].values())
    print(json.dumps({"blocks": len(out), "parcels": len(chosen_items), "roles": roles,
                      "left_out_too_large": len(members - used)}))


def strips_pilot(seed=20261118, n=60):
    """Run plan step 2: 60 random S strips, each a single close-up with its neighbours visible, plus hidden controls."""
    d, src, key, items, nb = _queue_context()
    ai = json.loads((WORK / "ai_targets.json").read_text(encoding="utf-8"))
    pool = sorted(p for p in ai["S"] if p in items)
    rng = random.Random(seed)
    pick = rng.sample(pool, n)
    labelled = set(owner_labels())
    row = d.set_index("internal_parcel_id")
    ctrl_pool = sorted(p for p in labelled if p in items and (row.loc[p].mean_width_m or 99) < 15 and row.loc[p].status != "fixed_nci")
    ctrl = rng.sample(ctrl_pool, min(6, len(ctrl_pool)))
    chosen = [items[p] for p in pick + ctrl]
    rng.shuffle(chosen)
    _write("v12_strips_pilot", chosen, None, key, src, {"seed": seed, "strips": [items[p]["id"] for p in pick],
                                                        "controls": [items[p]["id"] for p in ctrl]})
    print(json.dumps({"strips": len(pick), "controls": len(ctrl)}))


def strips(seed=20261124):
    """Run plan step 2, after the pilot's go: every remaining S strip (pilot strips excluded), single close-ups."""
    d, src, key, items, nb = _queue_context()
    ai = json.loads((WORK / "ai_targets.json").read_text(encoding="utf-8"))
    pilot = set(json.loads((OUT / "queues/v12_strips_pilot.key.json").read_text(encoding="utf-8"))["strips"])
    chosen = [items[p] for p in sorted(ai["S"]) if p in items and items[p]["id"] not in pilot]
    random.Random(seed).shuffle(chosen)
    _write("v12_strips", chosen, None, key, src, {"seed": seed})
    print(json.dumps({"strips": len(chosen)}))


if __name__ == "__main__":
    {"targets": targets, "pilot": pilot, "candidates": candidates, "blocks": blocks, "strips-pilot": strips_pilot,
     "strips": strips}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
