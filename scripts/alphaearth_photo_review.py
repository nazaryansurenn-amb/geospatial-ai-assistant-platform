"""Photo review tool for land types (owner-approved plan of 2026-10-30), loopback port 8547.

  alphaearth_photo_review.py prepare QUEUE   build a queue: gold_panel (200) or gold_accuracy (300)
  alphaearth_photo_review.py serve           review server on http://127.0.0.1:8547/

Pages draw Esri World Imagery tiles in the browser (display use, as the live map does) with the parcel outline.
Shown: photo date/resolution, and after the photo reading the local Sentinel hints. Never shown: cadastral codes,
AlphaEarth groups, Transitions classes, model output, or other reviewers' answers. Decisions are computed with
wp_core.land_types.decide from the reviewer's shares. Answers are stored per queue and reviewer.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_core.land_types import (contradictions, cut_events, decide, field_blocks, green_days, mean_width, photo_shares,
                                weak_spring_peak)

# v2 (owner decisions after the pilot): structure-only photo stage, fixed NCI status, weak-spring-peak rule,
# independent signals. The pilot answers were made under v1.
TYPES_CONFIG = json.loads((ROOT / "config/alphaearth_types_20261030_v2_4.json").read_text(encoding="utf-8"))
HINT_KEYS = {"ndvi", "cycles", "ndmi_2025", "bsi_2025", "cuts", "green_days"}  # raw Sentinel-2 only (cuts and green
# days come from the same frequent series, v2_3); no product-derived classes
CANDIDATE = json.loads((ROOT / "config/alphaearth_groups_20261030_v1.json").read_text())
DATA = ROOT / CANDIDATE["outputs"]["data"]
OLD_REVIEW = ROOT / CANDIDATE["outputs"]["review"]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
PORT = 8547
REVIEWERS = ["owner", "claude"] + [f"claude-r{i}" for i in range(1, TYPES_CONFIG["pilot"]["reviewers_max"] + 1)]
SMALL_HA = TYPES_CONFIG["rules"]["small_parcel_below_ha"]
PROD_EXCLUDED_TESTS = ["blocktest", "prod_t1000", "prod_t100_v22", "prod_t300_blind"]  # reviewed in test queues (prod_t1000_v2 / _std200* re-review prod_t1000)
DAILY = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1"


def queue_parcels(name):
    import pandas as pd
    if name == "gold_panel":
        return pd.read_csv(OLD_REVIEW / "labels/panel_key.csv").internal_parcel_id.tolist()
    if name == "gold_accuracy":
        return pd.read_csv(OLD_REVIEW / "accuracy/sample_key.csv").internal_parcel_id.tolist()
    if name == "prod":
        # Every eligible parcel inside the AlphaEarth classification except the 300 reviewed pilot parcels and the
        # parcels of the test queues (already reviewed; owner rule: never two reviewers on one parcel).
        taken = set(pd.read_csv(OLD_REVIEW / "accuracy/sample_key.csv").internal_parcel_id)
        for test in PROD_EXCLUDED_TESTS:
            path = OUT / "queues" / f"{test}.key.json"
            if path.exists():
                key = json.loads(path.read_text(encoding="utf-8"))
                taken |= {k["internal_parcel_id"] for k in (key["items"] if isinstance(key, dict) else key)}
        return [p for p in pd.read_parquet(DATA / "grid/parcels.parquet").internal_parcel_id if p not in taken]
    raise SystemExit("Unknown queue; use gold_panel, gold_accuracy, prod or blocktest")


def hints(ids):
    """Independent local Sentinel-2 evidence per parcel: monthly NDVI 2021-2025, 2025 NDMI/BSI, cycles per year.
    Product-derived classes (AlphaEarth stability, 8526 history/activity) are excluded (owner decision, v2)."""
    import numpy as np
    import pandas as pd
    out = {i: {"ndvi": {}, "cycles": {}} for i in ids}
    for year in range(2021, 2026):
        d = pd.read_parquet(DAILY / f"daily/{year}.parquet", filters=[("internal_parcel_id", "in", list(ids))],
                            columns=["internal_parcel_id", "observation_date", "ndvi_median", "ndvi_valid_fraction", "ndmi_median", "bsi_median"])
        d = d[d.ndvi_valid_fraction >= .8]
        d["month"] = pd.to_datetime(d.observation_date).dt.month
        monthly = d.groupby(["internal_parcel_id", "month"])[["ndvi_median", "ndmi_median", "bsi_median"]].median()
        # v2_3: frequent series (one value per date) -> cut-and-regrow events and green days (alfalfa evidence)
        dense = d.assign(t=pd.to_datetime(d.observation_date)).groupby(["internal_parcel_id", "t"]).ndvi_median.median()
        for pid, s in dense.groupby(level=0):
            pts = [(t, float(v)) for (_, t), v in s.items()]
            out[pid].setdefault("cuts", {})[str(year)] = cut_events(pts)
            out[pid].setdefault("green_days", {})[str(year)] = green_days(pts)
        for (pid, month), row in monthly.iterrows():
            out[pid]["ndvi"].setdefault(str(year), [None] * 12)[month - 1] = round(float(row.ndvi_median), 3)
            if year == 2025:
                out[pid].setdefault("ndmi_2025", [None] * 12)[month - 1] = None if np.isnan(row.ndmi_median) else round(float(row.ndmi_median), 3)
                out[pid].setdefault("bsi_2025", [None] * 12)[month - 1] = None if np.isnan(row.bsi_median) else round(float(row.bsi_median), 3)
        events = pd.read_parquet(DAILY / f"events/{year}.parquet", columns=["internal_parcel_id"])
        counts = events[events.internal_parcel_id.isin(ids)].internal_parcel_id.value_counts()
        for pid in ids:
            out[pid]["cycles"][str(year)] = int(counts.get(pid, 0))
    return out


def build_items(ids, prefix):
    """Review items for parcel ids, plus the parcels that get the fixed NCI status instead of review."""
    import geopandas as gpd
    import pandas as pd
    import shapely
    parcels = pd.read_parquet(DATA / "grid/parcels.parquet").set_index("internal_parcel_id").loc[ids]
    photo = pd.read_parquet(DATA / "photo_meta/parcels.parquet").set_index("internal_parcel_id").loc[ids]
    with __import__("sqlite3").connect((ROOT / "server_data/agent_v7/parcels.sqlite3").as_uri() + "?mode=ro", uri=True) as con:
        community = pd.read_sql_query("SELECT cadastre_code, community FROM parcels", con).set_index("cadastre_code").community
    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    metric = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[ids].to_crs(32638)
    metric_simple = metric.geometry.simplify(.25)  # 25 cm: invisible at 0.23 m/px, keeps queue files small
    lonlat = gpd.GeoSeries(metric_simple, crs=32638).to_crs(4326)
    fixed_rule = TYPES_CONFIG["rules"]["fixed_status"]
    width = max(len(str(len(ids))), 3)
    review_ids = [pid for pid in ids if not (float(parcels.area_official_m2[pid]) / 1e4 < fixed_rule["below_ha"]
                                             or mean_width(metric.geometry[pid].area, metric.geometry[pid].length) < fixed_rule["below_mean_width_m"])]
    evidence = hints(review_ids)
    items, key, fixed = [], [], []
    for n, pid in enumerate(ids, 1):
        shape, ha = metric.geometry[pid], float(parcels.area_official_m2[pid]) / 1e4
        w = round(mean_width(shape.area, shape.length), 1)
        item_id = f"{prefix}{n:0{width}d}"
        key.append({"id": item_id, "internal_parcel_id": pid})
        if pid not in evidence:
            fixed.append({"id": item_id, "type": fixed_rule["code"], "area_ha": round(ha, 4), "mean_width_m": w})
            continue
        geo = lonlat[pid]
        parts = [geo] if geo.geom_type == "Polygon" else sorted(geo.geoms, key=lambda s: -s.area)
        point = shapely.point_on_surface(lonlat[pid])
        has19 = isinstance(photo.zoom19_date[pid], str)
        bounds = shape.bounds
        items.append({"id": item_id, "community": community.get(parcels.cadastre_code[pid]), "area_ha": round(ha, 3),
                      "mean_width_m": w, "mode": "small" if ha < SMALL_HA else "shares", "lon": round(point.x, 6), "lat": round(point.y, 6),
                      "outline": [[round(x, 6), round(y, 6)] for x, y in parts[0].exterior.coords],
                      "parts": [[[round(x, 6), round(y, 6)] for x, y in p.exterior.coords] for p in parts],
                      "metric": {"cx": round(shape.centroid.x, 1), "cy": round(shape.centroid.y, 1), "bounds": [round(b, 1) for b in bounds]},
                      "photo": {"date": photo.photo_date[pid], "current": bool(photo.photo_current[pid]),
                                "resolution_m": float(photo.zoom19_resolution_m[pid] if has19 else photo.zoom16_18_resolution_m[pid]),
                                "provider": photo.zoom19_provider[pid] if has19 else photo.zoom16_18_provider[pid], "max_zoom": 19 if has19 else 18},
                      "hints": evidence[pid]})
    return items, key, fixed


def make_blocks(items, prefix="B"):
    """Review windows: neighbouring parcels that fit one zoom-19 photo window together (owner request)."""
    import numpy as np
    b = TYPES_CONFIG["block"]
    centroids = np.array([[i["metric"]["cx"], i["metric"]["cy"]] for i in items])
    bounds = np.array([i["metric"]["bounds"] for i in items])
    groups = field_blocks(centroids, bounds, b["max_parcels"], b["window_m"])
    return [{"id": f"{prefix}{k + 1:05d}", "items": [items[j]["id"] for j in sorted(g, key=lambda j: (-items[j]["metric"]["cy"], items[j]["metric"]["cx"]))]}
            for k, g in enumerate(groups)]


def write_queue(name, items, key, fixed, blocks=None, extra=None):
    folder = OUT / "queues"
    if (folder / f"{name}.json").exists():
        raise SystemExit("Queue already exists; it is never overwritten.")
    folder.mkdir(parents=True, exist_ok=True)
    data = {"queue": name, "rules": TYPES_CONFIG["version"], "items": items, "fixed_status": fixed}
    if blocks is not None:
        data["blocks"] = blocks
    (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (folder / f"{name}.key.json").write_text(json.dumps(key if extra is None else {"items": key, **extra}), encoding="utf-8")
    print(f"{name}: {len(items)} parcels to review ({sum(i['mode'] == 'small' for i in items)} small), "
          f"{len(fixed)} fixed NCI, {len(blocks) if blocks else 0} blocks")


def prepare_sample(name, target, seed, prefix="k"):
    """Production-like test queue: whole field blocks drawn at random from the production parcels until `target`
    parcels need review (fixed-NCI parcels come along with their blocks)."""
    import random
    import geopandas as gpd
    import pandas as pd
    ids = queue_parcels("prod")
    area = pd.read_parquet(DATA / "grid/parcels.parquet").set_index("internal_parcel_id").area_official_m2
    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    metric = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[ids].to_crs(32638).geometry
    rule, b = TYPES_CONFIG["rules"]["fixed_status"], TYPES_CONFIG["block"]
    reviewable = {p: not (area[p] / 1e4 < rule["below_ha"] or mean_width(metric[p].area, metric[p].length) < rule["below_mean_width_m"])
                  for p in ids}
    centroids = [[g.centroid.x, g.centroid.y] for g in metric]
    groups = field_blocks(centroids, [g.bounds for g in metric], b["max_parcels"], b["window_m"])
    random.Random(seed).shuffle(groups)
    chosen, count = [], 0
    for group in groups:
        if count >= target:
            break
        chosen += [ids[j] for j in group]
        count += sum(reviewable[ids[j]] for j in group)
    items, key, fixed = build_items(chosen, prefix)  # a distinct prefix per test keeps item ids unique across queues
    write_queue(name, items, key, fixed, make_blocks(items, prefix.upper()), {"sample": {"seed": seed, "target": target}})


def prepare(name):
    if name == "blocktest":
        return prepare_blocktest()
    if name == "prod_t1000":
        return prepare_sample(name, 1000, 20261031)
    if name == "prod_t100_v22":  # owner: 100 new parcels with lean Opus and rules v2_2, then owner verify
        return prepare_sample(name, 100, 20261106, "m")
    if name == "prod_full_v24":  # owner: the whole area under rules v2_4, test parcels included (owner labels win)
        import pandas as pd
        ids = pd.read_parquet(DATA / "grid/parcels.parquet").internal_parcel_id.tolist()
        items, key, fixed = build_items(ids, "f")
        return write_queue(name, items, key, fixed, make_blocks(items, "F"), {"rules": TYPES_CONFIG["version"]})
    if name == "prod_t300_blind":  # owner: blind test; AI reviews 300, owner labels ~100 without seeing answers
        return prepare_sample(name, 300, 20261107, "n")
    items, key, fixed = build_items(queue_parcels(name), {"gold_panel": "p", "gold_accuracy": "a", "prod": "P"}.get(name, "x"))
    write_queue(name, items, key, fixed, make_blocks(items) if name == "prod" else None)


def prepare_blocktest(pilot_parcels=25, seed=20261103):
    """Comparison test: windows around random small pilot parcels, with all their eligible neighbours."""
    import random
    import geopandas as gpd
    import pandas as pd
    pilot = pd.read_csv(OLD_REVIEW / "accuracy/sample_key.csv")
    pilot_key = {k["internal_parcel_id"]: k["id"] for k in json.loads((OUT / "queues/gold_accuracy.key.json").read_text())}
    answered = {k for k, v in reviewer_finals("gold_accuracy").items()}
    basis = json.loads((ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    eligible = pd.read_parquet(DATA / "grid/parcels.parquet").internal_parcel_id
    geo = gpd.read_parquet(ROOT / basis).set_index("internal_parcel_id").loc[eligible].to_crs(32638)
    centroid = geo.geometry.centroid
    half = TYPES_CONFIG["block"]["window_m"] / 2
    rng = random.Random(seed)
    candidates = [p for p in pilot.internal_parcel_id if pilot_key[p] in answered and geo.geometry[p].area < 5000]
    rng.shuffle(candidates)
    chosen, used, windows = [], set(), []
    for p in candidates:
        c = centroid[p]
        inside = [q for q in geo.index[(abs(centroid.x - c.x) <= half) & (abs(centroid.y - c.y) <= half)]
                  if geo.geometry[q].bounds[2] - geo.geometry[q].bounds[0] <= 2 * half and geo.geometry[q].bounds[3] - geo.geometry[q].bounds[1] <= 2 * half]
        if p not in inside or used & set(inside) or not 4 <= len(inside) <= TYPES_CONFIG["block"]["max_parcels"]:
            continue
        inside = sorted(inside, key=lambda q: (-round(centroid[q].y / 10), centroid[q].x))  # number top to bottom, left to right
        used |= set(inside); chosen.append(p); windows.append(inside)
        if len(chosen) == pilot_parcels:
            break
    ids = [q for w in windows for q in w]
    items, key, fixed = build_items(ids, "t")
    by_pid = {k["internal_parcel_id"]: k["id"] for k in key}
    reviewable = {i["id"] for i in items}
    blocks = [{"id": f"T{k + 1:03d}", "items": [by_pid[q] for q in w if by_pid[q] in reviewable]} for k, w in enumerate(windows)]
    blocks = [b for b in blocks if b["items"]]  # a window whose parcels all got the fixed NCI status has nothing to review
    pilot_map = {by_pid[p]: pilot_key[p] for p in chosen}
    write_queue("blocktest", items, key, fixed, blocks, {"pilot_parcels": pilot_map})
    # Twin queue with the same parcels in single mode, answered by other reviewers, for a parcel-by-parcel comparison.
    write_queue("blocktest_single", items, key, fixed, None, {"pilot_parcels": pilot_map})


def validate_answer(item, body, owner=False, forced=False):
    """Return the stored record or raise ValueError. Final answers get the rule decision attached.
    forced (v1.1 second pass): no deferral; the reviewer gives its most likely type, confidence may be low."""
    stage = body.get("stage")
    if stage not in ("photo", "final"):
        raise ValueError("stage must be photo or final")
    if forced and body.get("defer"):
        raise ValueError("forced choice: do not defer; give your most likely type with confidence l (low), m or h")
    if body.get("defer"):
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ValueError("A deferred parcel needs a reason")
        return {"stage": stage, "defer": True, "reason": reason[:400], "decision": {"type": "DEFER"}}
    confidence = body.get("confidence")
    if confidence not in (("high", "medium", "low") if forced else ("high", "medium")):
        raise ValueError("confidence must be high or medium (otherwise defer)")
    flags = body.get("flags") or {}
    if set(flags) - {"perennial_structure", "active_2025", "built"}:
        raise ValueError("unknown flag")
    common = {"defer": False, "flags": flags, "confidence": confidence, "reason": str(body.get("reason", "")).strip()[:400],
              "sources": body.get("sources") or ["esri_world_imagery"], "rules": TYPES_CONFIG["version"]}
    if stage == "photo" and "FLD" in TYPES_CONFIG["photo_stage"]["allowed"] and not body.get("legacy"):
        # v2: the photo reading records structure only; field land's 2025 use is decided after the signals.
        return {"stage": stage, "structure": photo_shares(body.get("shares"), TYPES_CONFIG), **common, "decision": {"type": "PHOTO"}}
    shares = body.get("shares")
    if not isinstance(shares, dict):
        raise ValueError("shares required")
    shares = {k: float(v) for k, v in shares.items() if float(v) > 0}
    decision = decide(shares, TYPES_CONFIG)
    h = item["hints"]
    crop_rule = TYPES_CONFIG["rules"].get("natural_ground_crop_season")
    crop_years = [y for y in crop_rule["years"] if (h["cycles"].get(y) or 0) >= 1] if crop_rule else []
    # Automatic rules never rewrite the owner's own answer (assessment: m080 NCU was stored as ANN).
    if not owner and crop_rule and crop_rule.get("action") == "convert" and "NAT" in shares and crop_years:
        # v2_4 owner rule: a crop season in any year 2021-2025 means the natural-ground PART is field land; only that
        # share is converted and the type is decided again (f15386: orchard 50% must stay an orchard)
        to = crop_rule["result_if_2025_cycle"] if (h["cycles"].get("2025") or 0) >= 1 else crop_rule["result"]
        converted = {k: v for k, v in shares.items() if k != "NAT"}
        converted[to] = converted.get(to, 0) + shares["NAT"]
        decision = {**decide(converted, TYPES_CONFIG), "attribute": crop_rule["attribute"], "converted_from": "NAT",
                    "converted_shares": converted}
    if not owner and decision["type"] in TYPES_CONFIG["rules"]["weak_spring_peak"]["applies_to"] and weak_spring_peak(h, TYPES_CONFIG["rules"]["weak_spring_peak"]):
        decision = {**decision, "type": "ANN", "attribute": TYPES_CONFIG["rules"]["weak_spring_peak"]["attribute"]}
    record = {"stage": stage, "shares": shares, **common, "decision": decision}
    peaks = {y: max([v for v in vals if v is not None], default=None) for y, vals in h["ndvi"].items()}
    evidence = {"green_years": sum(1 for p in peaks.values() if p is not None and p >= .4),
                "cycles_2025": h["cycles"].get("2025"), "peak_ndvi_2025": peaks.get("2025"), "area_ha": item.get("area_ha")}
    sig = TYPES_CONFIG["rules"].get("alfalfa_cut_signature")
    if sig and "cuts" in h:  # v2_3; queues built before v2_3 carry no frequent-data cuts
        cuts, green = h.get("cuts", {}), h.get("green_days", {})
        years = [y for y in sig["years"] if cuts.get(y, 0) >= sig["min_cuts"] and green.get(y, 0) >= sig["min_green_days"]]
        evidence["alfalfa_cut_years"] = years if len(years) >= sig["min_years"] else []
        weak = TYPES_CONFIG["rules"]["alfalfa_without_cuts"]
        evidence["alfalfa_without_cuts"] = (all(cuts.get(y, 0) < weak["cuts_below"] for y in weak["years"])
                                            and all(green.get(y, 0) < weak["green_days_below"] for y in weak["years"]))
    if crop_rule:  # v2_2 owner rule: land with a detected crop cycle is not "natural ground"
        evidence["crop_cycle_years"] = crop_years
    record["contradictions"] = contradictions({**decision, "flags": flags}, evidence)
    return record


PAGE = (Path(__file__).with_name("alphaearth_photo_review_page.html"))
VERIFY_PAGE = (Path(__file__).with_name("alphaearth_verify_page.html"))
FIRST_PAGE = Path(__file__).with_name("alphaearth_verify_first_page.html")


def owner_first_answer(item, body, saved, choices):
    """Owner-first check (safety review): the owner picks a type BEFORE seeing the map's answer. The first pick is
    locked; later posts only update the comment. Returns the record to store; it carries the revealed answer."""
    if saved and saved.get("owner_type"):
        return {**saved, "note": str(body.get("note", saved.get("note", "")))[:300]}
    pick = body.get("owner_type")
    if pick != "cannot_tell" and pick not in choices:
        raise ValueError("choose a type or can't tell")
    # broader classes (v1.2 Addendum 4) accept any of their sub-types, e.g. PERENNIAL accepts VINEYARD and ORCHARD
    accept = item.get("accept") or [item["shown_type"]]
    verdict = "cannot_tell" if pick == "cannot_tell" else "agree" if pick in accept else "disagree"
    return {"owner_type": pick, "verdict": verdict, "correct": pick if verdict == "disagree" else None,
            "note": str(body.get("note", ""))[:300], "shown": item["shown_type"], "shown_note": item.get("shown_note", ""),
            "at": datetime.now(timezone.utc).isoformat()}


def hide_answers(vset):
    """What the owner-first page may see before answering: no map answer, no stratum note."""
    return {**vset, "items": [{k: v for k, v in i.items() if k not in ("shown_type", "shown_note", "shown_components", "accept")}
                              for i in vset["items"]]}
CHOICES = list(TYPES_CONFIG["types"]) + ["MIX", "PART"]


def reviewer_finals(queue):
    """Final, non-deferred reviewer answers per parcel; the latest answer wins (owner answers excluded)."""
    finals = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            final = entry.get("final")
            if final and not final.get("defer") and (item_id not in finals or final.get("at", "") > finals[item_id]["at"]):
                finals[item_id] = {"reviewer": path.stem.split("__", 1)[1], "type": final["decision"]["type"], "at": final.get("at", ""),
                                   "components": final["decision"].get("components", [])}
    return finals


def make_owner_queue(queue):
    """Parcels every reviewer deferred, as a new queue for the owner, with the reviewers' notes."""
    target = OUT / "queues" / f"owner_{queue}.json"
    if target.exists():
        raise SystemExit("Owner queue already exists; it is never overwritten.")
    source = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    classified = reviewer_finals(queue)
    notes = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        reviewer = path.stem.split("__", 1)[1]
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            final = entry.get("final")
            rule_defer = final and (final.get("decision") or {}).get("type") == "DEFER"  # e.g. tree rows only
            if final and (final.get("defer") or rule_defer) and classified.get(item_id, {}).get("type", "DEFER") == "DEFER":
                notes.setdefault(item_id, []).append(f"{reviewer}: {final.get('reason', '')}" + (" (rules: deferred)" if rule_defer else ""))
    items = [{**it, "notes": notes[it["id"]]} for it in source["items"] if it["id"] in notes]
    target.write_text(json.dumps({"queue": f"owner_{queue}", "rules": source.get("rules"), "items": items},
                                 ensure_ascii=False), encoding="utf-8")
    raw = json.loads((OUT / "queues" / f"{queue}.key.json").read_text())
    key = {k["id"]: k for k in (raw["items"] if isinstance(raw, dict) else raw)}  # newer keys are {"items": [...], ...}
    (OUT / "queues" / f"owner_{queue}.key.json").write_text(json.dumps([key[it["id"]] for it in items]), encoding="utf-8")
    print(f"owner queue: {len(items)} deferred parcels")


def make_verify(queue, size=150, trap_share=.1, seed=20261102):
    """Random sample of reviewer answers plus disclosed deliberately wrong 'check' answers (excluded from results)."""
    import random
    target = OUT / "verify" / f"{queue}.json"
    if target.exists():
        raise SystemExit("Verify set already exists; it is never redrawn.")
    items = {i["id"]: i for i in json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))["items"]}
    finals = reviewer_finals(queue)
    rng = random.Random(seed)
    ids = sorted(finals)
    sample = rng.sample(ids, min(size, len(ids)))
    rest = [i for i in ids if i not in sample]
    traps = rng.sample(rest if len(rest) >= size * trap_share else ids, max(1, round(size * trap_share)))
    shown, key = [], {}
    for item_id in sample + traps:
        truth = finals[item_id]
        trap = item_id in traps and item_id not in sample
        shown_type = rng.choice([c for c in CHOICES if c not in (truth["type"], "TRE")]) if trap else truth["type"]
        vid = f"v{len(shown) + 1:03d}"
        it = items[item_id]
        shown.append({"vid": vid, "lat": it["lat"], "lon": it["lon"], "outline": it["outline"], "photo": it["photo"],
                      "area_ha": it["area_ha"], "community": it["community"], "shown_type": shown_type,
                      "shown_components": [] if trap else truth["components"]})
        key[vid] = {"item": item_id, "trap": trap, "reviewer": truth["reviewer"], "reviewer_type": truth["type"]}
    rng.shuffle(shown)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"queue": queue, "items": shown}, ensure_ascii=False), encoding="utf-8")
    (OUT / "verify" / f"{queue}.key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(f"verify set {queue}: {len(sample)} sampled answers + {sum(v['trap'] for v in key.values())} check answers")


def make_verify_compare(name, base, versions, subset, size=150, trap_share=.1, seed=20261104):
    """Owner verify sample over several reviewer versions of the same parcels (owner-approved t1000 plan).

    Strata: versions agree / disagree x inside / outside `subset` (the standard-reviewer queue), so every version
    gets owner truths and the estimates can be weighted back to the population. The owner sees the latest
    version's answer (or another version's when it deferred) and agrees or corrects it; ~10% disclosed check
    answers are mixed in. The key keeps every version's answer and the stratum sizes."""
    import random
    target = OUT / "verify" / f"{name}.json"
    if target.exists():
        raise SystemExit("Verify set already exists; it is never redrawn.")
    items = {i["id"]: i for i in json.loads((OUT / "queues" / f"{base}.json").read_text(encoding="utf-8"))["items"]}
    answers = {}
    for version in versions:
        for path in sorted((OUT / "answers").glob(f"{version}__claude*.json")):
            for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
                final = entry.get("final")
                if final:
                    answers.setdefault(item_id, {})[version] = {
                        "type": "DEFER" if final.get("defer") else final["decision"]["type"],
                        "components": final.get("decision", {}).get("components", [])}
    inside = {i["id"] for i in json.loads((OUT / "queues" / f"{subset}.json").read_text(encoding="utf-8"))["items"]}
    strata = {}
    for item_id, by_version in answers.items():
        typed = {v["type"] for v in by_version.values() if v["type"] != "DEFER"}
        if not typed:
            continue  # every version deferred: the parcel goes to the owner queue anyway
        agree = len(typed) == 1 and all(v["type"] != "DEFER" for v in by_version.values())
        strata.setdefault(("agree" if agree else "disagree") + ("_std" if item_id in inside else ""), []).append(item_id)
    rng = random.Random(seed)
    per = {s: min(len(ids), size // 4) for s, ids in strata.items()}
    spare = size - sum(per.values())
    for s in sorted(strata, key=lambda s: -len(strata[s])):  # unused allocation goes to the largest strata
        extra = min(spare, len(strata[s]) - per[s])
        per[s] += extra
        spare -= extra
    sample = {s: sorted(rng.sample(sorted(ids), per[s])) for s, ids in strata.items()}
    chosen = [i for ids in sample.values() for i in ids]
    rest = sorted(set(answers) - set(chosen))
    traps = rng.sample(rest, max(1, round(size * trap_share)))
    shown, key = [], {}
    for item_id in chosen + traps:
        by_version = answers[item_id]
        lead = next((v for v in reversed(versions) if v in by_version and by_version[v]["type"] != "DEFER"), None)
        trap = item_id in traps
        if lead is None:
            continue
        truth = by_version[lead]
        shown_type = rng.choice([c for c in CHOICES if c not in (truth["type"], "TRE")]) if trap else truth["type"]
        vid = f"v{len(shown) + 1:03d}"
        it = items[item_id]
        shown.append({"vid": vid, "lat": it["lat"], "lon": it["lon"], "outline": it["outline"], "photo": it["photo"],
                      "area_ha": it["area_ha"], "community": it["community"], "shown_type": shown_type,
                      "shown_components": [] if trap else truth["components"]})
        key[vid] = {"item": item_id, "trap": trap, "shown_version": lead,
                    "stratum": next((s for s, ids in sample.items() if item_id in ids), None),
                    "answers": {v: a["type"] for v, a in by_version.items()}}
    rng.shuffle(shown)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"queue": base, "items": shown}, ensure_ascii=False), encoding="utf-8")
    (OUT / "verify" / f"{name}.key.json").write_text(json.dumps(
        {"versions": versions, "subset": subset, "strata_sizes": {s: len(ids) for s, ids in strata.items()},
         "sampled": {s: len(ids) for s, ids in sample.items()}, "items": key}, indent=1), encoding="utf-8")
    print(f"verify set {name}: {len(chosen)} sampled + {sum(v['trap'] for v in key.values())} check answers; "
          f"strata {({s: (len(strata[s]), per[s]) for s in strata})}")


def make_verify_version(name, shown, others, trap_queue, traps=15, seed=20261105):
    """Owner verify of EVERY typed answer of one reviewer version (owner request: verify the Sonnet results).

    Disclosed check answers come from other parcels of `trap_queue`, shown with a deliberately wrong type. The key
    has the same layout as make_verify_compare, one stratum "all", so alphaearth_review_verify_score.py scores the
    shown version and the `others` on the same owner truths."""
    import random
    target = OUT / "verify" / f"{name}.json"
    if target.exists():
        raise SystemExit("Verify set already exists; it is never redrawn.")
    items = {i["id"]: i for i in json.loads((OUT / "queues" / f"{trap_queue}.json").read_text(encoding="utf-8"))["items"]}
    trap_ids = set(items)
    items.update({i["id"]: i for i in json.loads((OUT / "queues" / f"{shown}.json").read_text(encoding="utf-8"))["items"]})
    versions = [shown] + list(others)
    answers = {}
    for version in versions:
        for path in sorted((OUT / "answers").glob(f"{version}__claude*.json")):
            for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
                final = entry.get("final")
                if final:
                    answers.setdefault(item_id, {})[version] = {
                        "type": "DEFER" if final.get("defer") else final["decision"]["type"],
                        "components": final.get("decision", {}).get("components", [])}
    chosen = sorted(i for i, a in answers.items() if a.get(shown, {}).get("type", "DEFER") != "DEFER")
    rng = random.Random(seed)
    pool = sorted(i for i in trap_ids if i not in answers or shown not in answers[i])
    check = rng.sample(pool, traps)
    out, key = [], {}
    for item_id in chosen + check:
        trap = item_id in check
        if trap:
            real = next((a["type"] for a in answers.get(item_id, {}).values() if a["type"] != "DEFER"), None)
            shown_type, components = rng.choice([c for c in CHOICES if c not in (real, "TRE")]), []
        else:
            shown_type, components = answers[item_id][shown]["type"], answers[item_id][shown]["components"]
        vid = f"v{len(out) + 1:03d}"
        it = items[item_id]
        out.append({"vid": vid, "lat": it["lat"], "lon": it["lon"], "outline": it["outline"], "photo": it["photo"],
                    "area_ha": it["area_ha"], "community": it["community"], "shown_type": shown_type,
                    "shown_components": components})
        key[vid] = {"item": item_id, "trap": trap, "shown_version": shown, "stratum": None if trap else "all",
                    "answers": {v: a["type"] for v, a in answers.get(item_id, {}).items()}}
    rng.shuffle(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"queue": trap_queue, "items": out}, ensure_ascii=False), encoding="utf-8")
    (OUT / "verify" / f"{name}.key.json").write_text(json.dumps(
        {"versions": versions, "subset": shown, "strata_sizes": {"all": len(chosen)}, "sampled": {"all": len(chosen)},
         "items": key}, indent=1), encoding="utf-8")
    print(f"verify set {name}: {len(chosen)} {shown} answers + {len(check)} check answers")


def make_second_pass(name, source, candidates_path, seed=20261110):
    """v1.1 second AI pass (owner-approved strategy step 5): single close-ups of the candidate parcels, forced choice
    (no deferral), with every owner-labelled parcel of `source` mixed in as a hidden control. Item ids stay those of
    `source`; the key lists the controls and the candidate reasons, never served to reviewers."""
    import random
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from alphaearth_landtypes_v11 import owner_labels
    target = OUT / "queues" / f"{name}.json"
    if target.exists():
        raise SystemExit("Queue already exists; it is never overwritten.")
    data = json.loads((OUT / "queues" / f"{source}.json").read_text(encoding="utf-8"))
    raw = json.loads((OUT / "queues" / f"{source}.key.json").read_text(encoding="utf-8"))
    key = {k["id"]: k for k in (raw["items"] if isinstance(raw, dict) else raw)}
    cand = set(json.loads(Path(candidates_path).read_text(encoding="utf-8"))["items"])
    labelled = set(owner_labels())
    controls = sorted(i for i, k in key.items() if k["internal_parcel_id"] in labelled)
    chosen = [it for it in data["items"] if it["id"] in cand or it["id"] in controls]
    random.Random(seed).shuffle(chosen)                         # controls are not grouped or ordered
    target.write_text(json.dumps({"queue": name, "rules": TYPES_CONFIG["version"], "forced_choice": True,
                                  "items": chosen, "fixed_status": []}, ensure_ascii=False), encoding="utf-8")
    (OUT / "queues" / f"{name}.key.json").write_text(json.dumps({
        "items": [key[it["id"]] for it in chosen], "source": source, "controls": controls,
        "candidates": sorted(cand)}), encoding="utf-8")
    print(f"{name}: {len(chosen)} parcels ({len(cand)} candidates, {len(controls)} owner-labelled controls, "
          f"{len(set(controls) & cand)} in both)")


def make_blind_test(name, queue, size=100, rare=("ALF", "VIN", "ORC", "BLT", "GRS", "GRH", "WAT", "MIX", "PART"),
                    per_rare=10, seed=20261108):
    """Blind owner test (owner request after the final report): the owner labels a sample of the AI's typed parcels
    WITHOUT seeing the AI answer. Strata = each rare AI type (up to `per_rare` each, oversampled) + all other typed
    parcels (random fill), so per-type accuracy is measurable and the overall rate can be weighted back. The served
    queue blind_<name> carries no AI answers or notes; the design and the AI types live only in the key file."""
    import random
    target = OUT / "queues" / f"blind_{name}.json"
    if target.exists():
        raise SystemExit("Blind queue already exists; it is never redrawn.")
    source = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    ai = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            final = entry.get("final")
            if final and not final.get("defer") and final["decision"]["type"] != "DEFER":
                ai[item_id] = final["decision"]["type"]
    strata = {}
    for item_id, kind in sorted(ai.items()):
        strata.setdefault(kind if kind in rare else "common", []).append(item_id)
    rng = random.Random(seed)
    sample = {s: sorted(rng.sample(ids, min(per_rare, len(ids)))) for s, ids in strata.items() if s != "common"}
    rest = size - sum(len(v) for v in sample.values())
    sample["common"] = sorted(rng.sample(strata.get("common", []), max(0, min(rest, len(strata.get("common", []))))))
    chosen = [i for ids in sample.values() for i in ids]
    rng.shuffle(chosen)
    items = {i["id"]: i for i in source["items"]}
    target.write_text(json.dumps({"queue": f"blind_{name}", "rules": source.get("rules"),
                                  "items": [items[i] for i in chosen]}, ensure_ascii=False), encoding="utf-8")
    raw = json.loads((OUT / "queues" / f"{queue}.key.json").read_text())
    key = {k["id"]: k for k in (raw["items"] if isinstance(raw, dict) else raw)}
    (OUT / "queues" / f"blind_{name}.key.json").write_text(json.dumps({
        "items": [key[i] for i in chosen], "source": queue,
        "design": {"strata_sizes": {s: len(v) for s, v in strata.items()}, "sampled": {s: len(v) for s, v in sample.items()},
                   "stratum": {i: s for s, ids in sample.items() for i in ids}, "ai": {i: ai[i] for i in chosen}}}), encoding="utf-8")
    print(f"blind queue blind_{name}: {len(chosen)} parcels; sampled per stratum "
          f"{ {s: f'{len(sample[s])}/{len(strata[s])}' for s in sample} }")


def log_problem(kind, **fields):
    """Append one problem (rejected save, failed render, bad answer format) to logs/problems.jsonl for the watchdog."""
    folder = OUT / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "problems.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}, ensure_ascii=False) + "\n")


def finished_by(queue):
    """Parcel -> the reviewer whose final answer (or photo-stage defer) closed it."""
    done = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        reviewer = path.stem.split("__", 1)[1]
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            if "final" in entry:
                done.setdefault(item_id, reviewer)
    return done


def batch_conflicts(item_ids, reviewer, done, claims, unit_of, now):
    """Why answers may not be saved (owner rule: never two reviewers on one parcel): another reviewer already
    closed the parcel, or this reviewer holds no live claim on its unit (an expired unit may be with someone else)."""
    problems = {}
    for item_id in item_ids:
        if done.get(item_id, reviewer) != reviewer:
            problems[item_id] = f"already answered by {done[item_id]}"
            continue
        lease = claims.get(unit_of.get(item_id, item_id))
        if not lease or lease["reviewer"] != reviewer or lease["until"] <= now:
            problems[item_id] = "no live claim on its unit; claim again"
    return problems


def serve():
    import threading
    import time as _time
    queues = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (OUT / "queues").glob("*.json") if not p.stem.endswith(".key")}
    index = {q: {i["id"]: i for i in data["items"]} for q, data in queues.items()}
    block_index = {q: {b["id"]: b for b in data.get("blocks", [])} for q, data in queues.items()}
    page = PAGE.read_text(encoding="utf-8").replace("__TYPES__", json.dumps(TYPES_CONFIG["types"], ensure_ascii=False))
    verify_sets = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (OUT / "verify").glob("*.json")
                   if not p.stem.endswith(".key")} if (OUT / "verify").exists() else {}
    labels = {**{k: v["en"] for k, v in TYPES_CONFIG["types"].items()}, **{k: v["en"] for k, v in TYPES_CONFIG["outcomes"].items()}}
    verify_page = VERIFY_PAGE.read_text(encoding="utf-8").replace("__LABELS__", json.dumps(labels, ensure_ascii=False)) \
        if VERIFY_PAGE.exists() else ""
    first_page = FIRST_PAGE.read_text(encoding="utf-8") if FIRST_PAGE.exists() else ""
    answers_dir = OUT / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    lease_seconds = 45 * 60

    def store(queue, reviewer):
        return answers_dir / f"{queue}__{reviewer}.json"

    def load(path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def dump(path, data):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def public(item):
        return {k: v for k, v in item.items() if k not in ("hints", "metric")}

    def signals(item):
        return {k: v for k, v in item["hints"].items() if k in HINT_KEYS}

    def finished(queue):
        return set(finished_by(queue))

    unit_of = {q: {i: b["id"] for b in blocks.values() for i in b["items"]} for q, blocks in block_index.items()}

    def save_records(queue, reviewer, records):
        """records: list of (item, record); all are written in one locked file update."""
        with lock:
            f = store(queue, reviewer)
            saved = load(f)
            now = datetime.now(timezone.utc).isoformat()
            for item, record in records:
                entry = saved.setdefault(item["id"], {})
                entry[record["stage"]] = {**record, "at": now}
                if record["stage"] == "photo" and record.get("defer"):
                    # Deferring on the photo alone closes the parcel; no second save needed (pilot round 2).
                    entry["final"] = {**record, "stage": "final", "at": now}
            dump(f, saved)

    def result(item, record):
        out = {"decision": record["decision"], "contradictions": record.get("contradictions", [])}
        if record["stage"] == "photo" and not record.get("defer"):
            out["signals"] = signals(item)  # the photo reading is saved first, so the signals can be released now
        return out

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body, kind="application/json; charset=utf-8"):
            body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _args(self):
            request = urlsplit(self.path)
            return request.path, {k: v[0] for k, v in parse_qs(request.query).items()}

        def do_GET(self):
            path, args = self._args()
            q = args.get("queue")
            if path == "/":
                rows = []
                for name, data in sorted(queues.items()):
                    links = " · ".join(f'<a href="/review?queue={name}&reviewer={r}">{r}</a>' for r in ("owner", "claude"))
                    rows.append(f"<li><b>{name}</b> ({len(data['items'])} parcels): {links}</li>")
                for name, v in sorted(verify_sets.items()):
                    rows.append(f"<li><b>Verify {name}</b> ({len(v['items'])} answers to check): <a href=\"/verify?set={name}\">owner</a></li>")
                self._reply(200, f"<!doctype html><meta charset=utf-8><title>Photo Review</title><body style='font:16px system-ui;margin:24px'>"
                                 f"<h1>Land-type photo review</h1><ul>{''.join(rows)}</ul></body>".encode(), "text/html; charset=utf-8")
            elif path == "/review" and q in queues and args.get("reviewer") in REVIEWERS:
                self._reply(200, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/queue" and q in queues:
                # Signals are withheld here; they are released per parcel only after the photo reading.
                self._reply(200, {**queues[q], "items": [public(i) for i in queues[q]["items"]]})
            elif path == "/api/item" and q in queues and args.get("item") in index[q]:
                self._reply(200, public(index[q][args["item"]]))
            elif path == "/api/block" and q in queues and args.get("block") in block_index[q]:
                b = block_index[q][args["block"]]
                self._reply(200, {"id": b["id"], "items": [public(index[q][i]) for i in b["items"]]})
            elif path == "/api/hints" and q in queues and args.get("reviewer") in REVIEWERS:
                item = index[q].get(args.get("item"))
                if item is None:
                    self._reply(404, {"error": "unknown item"})
                elif args["reviewer"] != "owner" and "photo" not in load(store(q, args["reviewer"])).get(item["id"], {}):
                    self._reply(403, {"error": "save the photo reading first"})
                else:
                    self._reply(200, signals(item))
            elif path == "/api/answers" and q in queues and args.get("reviewer") in REVIEWERS:
                self._reply(200, load(store(q, args["reviewer"])))
            elif path == "/verify" and args.get("set") in verify_sets:
                page_ = first_page if verify_sets[args["set"]].get("owner_first") else verify_page
                self._reply(200, page_.encode(), "text/html; charset=utf-8")
            elif path == "/api/verify" and args.get("set") in verify_sets:
                vset = verify_sets[args["set"]]
                self._reply(200, hide_answers(vset) if vset.get("owner_first") else vset)
            elif path == "/api/verify/answers" and args.get("set") in verify_sets:
                self._reply(200, load(answers_dir / f"verify_{args['set']}__owner.json"))
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            path, _ = self._args()
            if path not in ("/api/answer", "/api/batch", "/api/claim", "/api/verify/answer") or \
                    self.headers.get("Host") != f"127.0.0.1:{PORT}" or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}"):
                self._reply(403, {"error": "forbidden"})
                return
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 400000)) or b"{}")
                if path == "/api/verify/answer":
                    vset = verify_sets.get(body.get("set"))
                    if vset is None or body.get("vid") not in {v["vid"] for v in vset["items"]}:
                        raise ValueError("unknown verify item")
                    if vset.get("owner_first"):
                        item = next(v for v in vset["items"] if v["vid"] == body["vid"])
                        with lock:
                            f = answers_dir / f"verify_{body['set']}__owner.json"
                            saved = load(f)
                            saved[body["vid"]] = owner_first_answer(item, body, saved.get(body["vid"]), vset["choices"])
                            dump(f, saved)
                        self._reply(200, saved[body["vid"]])
                        return
                    if body.get("verdict") not in ("agree", "disagree", "cannot_tell"):
                        raise ValueError("verdict must be agree, disagree or cannot_tell")
                    if body["verdict"] == "disagree" and body.get("correct") not in (vset.get("choices") or CHOICES):
                        raise ValueError("choose the correct type")
                    with lock:
                        f = answers_dir / f"verify_{body['set']}__owner.json"
                        saved = load(f)
                        saved[body["vid"]] = {"verdict": body["verdict"], "correct": body.get("correct"),
                                              "note": str(body.get("note", ""))[:300], "at": datetime.now(timezone.utc).isoformat()}
                        dump(f, saved)
                    self._reply(200, {"ok": True})
                    return
                queue, reviewer = body.get("queue"), body.get("reviewer")
                if queue not in queues or reviewer not in REVIEWERS:
                    raise ValueError("unknown queue or reviewer")
                if path == "/api/claim":
                    n = max(1, min(int(body.get("n", 10)), 50))
                    units = list(block_index[queue].values()) if block_index[queue] else [{"id": i, "items": [i]} for i in index[queue]]
                    with lock:
                        claims_path = OUT / "claims" / f"{queue}.json"
                        claims_path.parent.mkdir(exist_ok=True)
                        claims, done, now = load(claims_path), finished(queue), _time.time()
                        granted = []
                        for unit in units:
                            lease = claims.get(unit["id"])
                            if all(i in done for i in unit["items"]) or (lease and lease["until"] > now and lease["reviewer"] != reviewer):
                                continue
                            claims[unit["id"]] = {"reviewer": reviewer, "until": now + lease_seconds}
                            granted.append(unit["id"])
                            if len(granted) == n:
                                break
                        dump(claims_path, claims)
                    # A unit picked up again after a crash hands out only its unfinished parcels.
                    pending = {u: [i for i in (block_index[queue][u]["items"] if block_index[queue] else [u]) if i not in done]
                               for u in granted}
                    self._reply(200, {"units": granted, "pending": pending, "kind": "block" if block_index[queue] else "item"})
                    return
                stage = body.get("stage")
                if path == "/api/batch":
                    with lock:
                        conflicts = batch_conflicts(list(body.get("answers") or {}), reviewer, finished_by(queue),
                                                    load(OUT / "claims" / f"{queue}.json"), unit_of[queue], _time.time())
                    if conflicts:
                        raise ValueError("not saved, " + "; ".join(f"{k}: {v}" for k, v in sorted(conflicts.items())))
                    pairs = []
                    for item_id, answer in (body.get("answers") or {}).items():
                        item = index[queue].get(item_id)
                        if item is None:
                            raise ValueError(f"unknown item {item_id}")
                        try:
                            pairs.append((item, validate_answer(item, {**answer, "stage": stage},
                                                                forced=bool(queues[queue].get("forced_choice")))))
                        except ValueError as exc:
                            raise ValueError(f"{item_id}: {exc}") from None
                    if not pairs:
                        raise ValueError("no answers")
                    save_records(queue, reviewer, pairs)  # all-or-nothing: nothing is saved if any answer is invalid
                    self._reply(200, {"ok": True, "results": {item["id"]: result(item, record) for item, record in pairs}})
                    return
                item = index[queue].get(body.get("item"))
                if item is None:
                    raise ValueError("unknown item")
                record = validate_answer(item, body, owner=reviewer == "owner")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                if path in ("/api/batch", "/api/claim", "/api/answer"):
                    log_problem("rejected_" + path.rsplit("/", 1)[1], queue=locals().get("queue"),
                                reviewer=locals().get("reviewer"), error=str(exc)[:500])
                self._reply(400, {"error": str(exc)})
                return
            save_records(queue, reviewer, [(item, record)])
            self._reply(200, {"ok": True, **result(item, record)})

        def log_message(self, *args):
            pass

    print(f"Photo review: http://127.0.0.1:{PORT}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["prepare"] and len(sys.argv) == 3:
        prepare(sys.argv[2])
    elif sys.argv[1:2] == ["make-verify"] and len(sys.argv) == 3:
        make_verify(sys.argv[2])
    elif sys.argv[1:2] == ["make-verify-compare"] and len(sys.argv) == 6:
        # NAME BASE_QUEUE VERSION,VERSION,... SUBSET_QUEUE (versions oldest first; the last is shown to the owner)
        make_verify_compare(sys.argv[2], sys.argv[3], sys.argv[4].split(","), sys.argv[5])
    elif sys.argv[1:2] == ["make-second-pass"] and len(sys.argv) == 5:
        make_second_pass(sys.argv[2], sys.argv[3], sys.argv[4])  # NAME SOURCE_QUEUE CANDIDATES_JSON
    elif sys.argv[1:2] == ["make-blind-test"] and len(sys.argv) == 4:
        make_blind_test(sys.argv[2], sys.argv[3])  # NAME QUEUE: owner page /review?queue=blind_NAME&reviewer=owner
    elif sys.argv[1:2] == ["make-verify-version"] and len(sys.argv) == 6:
        # NAME SHOWN_QUEUE OTHER,OTHER,... TRAP_QUEUE: every typed answer of SHOWN_QUEUE plus 15 check answers
        make_verify_version(sys.argv[2], sys.argv[3], sys.argv[4].split(","), sys.argv[5])
    elif sys.argv[1:2] == ["make-owner-queue"] and len(sys.argv) == 3:
        make_owner_queue(sys.argv[2])
    elif sys.argv[1:] == ["serve"]:
        serve()
    else:
        sys.exit(__doc__)
