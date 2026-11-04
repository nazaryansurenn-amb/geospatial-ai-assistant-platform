"""NCI sliver pairs pilot (owner-approved 2026-11-01: "build classifier for those slivers not classifiable we talked
before"). Two side-by-side "not classifiable" (fixed NCI) slivers are shown together in one review window; an AI
reviewer judges both at once. If they are one piece of land with a certain class, each sliver gets that class and
stays its own parcel (pairing is temporary, for viewing only; geometry, codes and areas are never changed).

  nci_pairs_pilot.py build    write queue nci_pairs_pilot (20 random pairs, seed 20261127); never overwrites
  nci_pairs_pilot.py check    validate_answer on typical small-mode answers for items of the queue (no server)

Pairs: fixed-NCI slivers p, q of land types v1.3 that share a boundary (v12/targets.json adjacency) of at least 70%
of the shorter sliver's length (length = area / mean width = half the perimeter of the metric geometry); greedy
non-overlapping pairs, longest shared boundary first. Items are built by alphaearth_photo_review.build_items with the
fixed NCI rule switched off for this queue only (same item fields, same hints() Sentinel evidence). The review server
must be restarted to load the new queue; this script starts no server and renders nothing.
"""
from __future__ import annotations
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alphaearth_photo_review as review  # noqa: E402  (sets up ROOT on sys.path for wp_core)
from wp_core.land_types import mean_width  # noqa: E402

NAME = "nci_pairs_pilot"
SEED = 20261127
PAIRS = 20
MIN_SHARED = 0.7
RELEASE = review.OUT / "releases/landtypes_v1_3"
TARGETS = review.OUT / "v12/targets.json"


def nci_geometry():
    import geopandas as gpd
    import pandas as pd
    d = pd.read_parquet(RELEASE / "parcels.parquet")
    nci = sorted(d.loc[d.status == "fixed_nci", "internal_parcel_id"])
    basis = json.loads((review.ROOT / "data/observations/observations_2021_2025_v1/specification.json").read_text())["config"]["basis"]
    return gpd.read_parquet(review.ROOT / basis).set_index("internal_parcel_id").loc[nci].to_crs(32638).geometry


def all_pairs(geom):
    """Greedy non-overlapping sliver pairs, longest shared boundary first (ties by ids, deterministic)."""
    adjacency = json.loads(TARGETS.read_text(encoding="utf-8"))["adjacency"]
    nci = set(geom.index)
    length = {p: g.area / mean_width(g.area, g.length) for p, g in geom.items()}
    candidates = {}
    for p in nci:
        for q, shared in adjacency.get(p, []):
            if q in nci and p != q and shared >= MIN_SHARED * min(length[p], length[q]):
                candidates[tuple(sorted((p, q)))] = float(shared)
    used, pairs = set(), []
    for (p, q), shared in sorted(candidates.items(), key=lambda x: (-x[1], x[0])):
        if p in used or q in used:
            continue
        used |= {p, q}
        pairs.append({"a": p, "b": q, "shared_m": round(shared, 1),
                      "length_m": [round(length[p], 1), round(length[q], 1)],
                      "shared_of_shorter": round(shared / min(length[p], length[q]), 3)})
    return pairs, len(candidates)


def build_items_without_nci_rule(ids, prefix):
    """review.build_items with the fixed NCI thresholds set to zero for this call only (every sliver gets an item)."""
    rule = review.TYPES_CONFIG["rules"]["fixed_status"]
    saved = dict(rule)
    rule.update(below_ha=0, below_mean_width_m=0)
    try:
        items, key, fixed = review.build_items(ids, prefix)
    finally:
        rule.clear()
        rule.update(saved)
    if fixed or len(items) != len(ids):
        raise SystemExit(f"expected {len(ids)} review items and no fixed NCI, got {len(items)} / {len(fixed)}")
    return items, key


def build():
    folder = review.OUT / "queues"
    if (folder / f"{NAME}.json").exists() or (folder / f"{NAME}.key.json").exists():
        raise SystemExit("Queue already exists; it is never overwritten.")
    geom = nci_geometry()
    pairs, candidates = all_pairs(geom)
    chosen = random.Random(SEED).sample(pairs, PAIRS)
    # Number the two slivers of each window top to bottom, then left to right (as make_blocks does).
    ids = []
    for pair in chosen:
        ids += sorted((pair["a"], pair["b"]), key=lambda p: (-round(geom[p].centroid.y, 1), round(geom[p].centroid.x, 1)))
    items, key = build_items_without_nci_rule(ids, "s")
    # One type per sliver (pilot design): a long narrow sliver of >= 0.5 ha would get "shares" mode from build_items.
    mode_overridden = [i["id"] for i in items if i["mode"] != "small"]
    for i in items:
        i["mode"] = "small"
    blocks = [{"id": f"S{k + 1:03d}", "items": [items[2 * k]["id"], items[2 * k + 1]["id"]]} for k in range(PAIRS)]
    data = {"queue": NAME, "rules": review.TYPES_CONFIG["version"], "forced_choice": True, "block_review": True,
            "items": items, "fixed_status": [], "blocks": blocks}
    by_pid = {k["internal_parcel_id"]: k["id"] for k in key}
    pair_key = [{**pair, "block": b["id"], "items": [by_pid[pair["a"]], by_pid[pair["b"]]]} for pair, b in zip(chosen, blocks)]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{NAME}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (folder / f"{NAME}.key.json").write_text(json.dumps({
        "items": key, "pairs": pair_key, "seed": SEED, "pairs_available": len(pairs), "candidate_pairs": candidates,
        "mode_set_small": mode_overridden,
        "rule": f"fixed-NCI slivers of landtypes_v1_3, adjacent, shared boundary >= {MIN_SHARED:.0%} of the shorter "
                "sliver's length (area / mean width); greedy non-overlapping, longest shared boundary first",
        "approval": "owner 2026-11-01: pair side-by-side NCI slivers temporarily; each sliver keeps its own parcel",
        "all_pairs": [[p["a"], p["b"], p["shared_m"]] for p in pairs]}, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"queue": str(folder / f"{NAME}.json"), "items": len(items), "blocks": len(blocks),
                      "pairs_available": len(pairs), "candidate_pairs": candidates, "mode_set_small": mode_overridden}))


def check():
    """Typical small-mode answers (as the reviewer CLI parses them) through validate_answer, forced choice."""
    from alphaearth_review_cli import parse_compact
    data = json.loads((review.OUT / "queues" / f"{NAME}.json").read_text(encoding="utf-8"))
    items = data["items"]
    a, b = items[0], items[1]
    lines = {"photo": f"{a['id']} VIN m same: vine rows continue through both\n{b['id']} BLT l differs: track edge",
             "final": f"{a['id']} VIN m same: vine rows continue through both\n{b['id']} BLT l differs: track edge"}
    out = {}
    for stage, text in lines.items():
        for item_id, body in parse_compact(text, stage).items():
            item = a if item_id == a["id"] else b
            record = review.validate_answer(item, {**body, "stage": stage}, forced=bool(data.get("forced_choice")))
            out[f"{stage} {item_id}"] = {"decision": record["decision"], "contradictions": record.get("contradictions", [])}
    for item in items:  # every item must carry what validate_answer reads
        review.validate_answer(item, {"stage": "final", "shares": {"ANN": 1}, "confidence": "low", "reason": "same: x"}, forced=True)
    try:
        review.validate_answer(a, {"stage": "final", "defer": True, "reason": "unsure"}, forced=True)
        out["defer"] = "ACCEPTED (unexpected)"
    except ValueError as exc:
        out["defer"] = f"refused as expected: {exc}"
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    {"build": build, "check": check}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
