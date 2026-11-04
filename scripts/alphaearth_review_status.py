"""Read-only progress and watchdog checks for the land-type photo review (never shown to reviewers).

Usage: alphaearth_review_status.py [QUEUE]   prints JSON with per-reviewer integrity, rates and pause triggers.
"""
from __future__ import annotations
import collections
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_core.land_types import decide, weak_spring_peak

CONFIGS = {c["version"]: c for c in (json.loads(f.read_text(encoding="utf-8"))
                                      for f in sorted((ROOT / "config").glob("alphaearth_types_20261030_v*.json")))}
CONFIG = CONFIGS["alphaearth_types_20261030_v1"]


def rule_type(record, item):
    """Type the rules of the record's own version give (v1 records carry no version)."""
    config = CONFIGS.get(record.get("rules"), CONFIG)
    shares = record["shares"]
    hints = item.get("hints") or {}
    crop = config["rules"].get("natural_ground_crop_season") or {}
    if crop.get("action") == "convert" and "NAT" in shares and any((hints.get("cycles") or {}).get(y) for y in crop["years"]):
        # same as the server: only the natural-ground share becomes field land, then the type is decided again
        to = crop["result_if_2025_cycle"] if (hints["cycles"].get("2025") or 0) >= 1 else crop["result"]
        shares = {k: v for k, v in shares.items() if k != "NAT"}
        shares[to] = shares.get(to, 0) + record["shares"]["NAT"]
    decision = decide(shares, config)["type"]
    weak = config["rules"].get("weak_spring_peak")
    if weak and decision in weak["applies_to"] and weak_spring_peak(item.get("hints") or {}, weak):
        return weak["result"]
    return decision
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
SEALS = ["irrigation_stress_review_20260907_v1", "readability_20260907_v1", "activity_change_area50_20260907_v1",
         "agent_review_20260906_v6", "activity_change_history_review_20260906_v1"]


def check_reviewer(path, items, in_progress=frozenset()):
    answers = json.loads(path.read_text(encoding="utf-8"))
    issues, types, deferred, contradicted, finals = [], collections.Counter(), 0, 0, 0
    for item_id, entry in answers.items():
        if item_id not in items:
            issues.append(f"{item_id}: not in queue")
            continue
        final = entry.get("final")
        if not final:
            if "photo" in entry:
                if item_id not in in_progress:  # a reviewer with a live claim is between its photo and final saves
                    issues.append(f"{item_id}: photo reading without final answer")
            continue
        finals += 1
        if "photo" not in entry:
            issues.append(f"{item_id}: final answer without a photo-stage reading")
        if final.get("defer"):
            deferred += 1
            types["DEFER"] += 1
            if not final.get("reason"):
                issues.append(f"{item_id}: deferred without reason")
            continue
        if (final.get("decision") or {}).get("type") == "DEFER":  # the rules sent it to the owner (e.g. tree rows only)
            deferred += 1
        for field in ("shares", "confidence", "reason", "decision", "sources"):
            if not final.get(field):
                issues.append(f"{item_id}: missing {field}")
        try:
            if rule_type(final, items[item_id]) != final["decision"]["type"]:
                issues.append(f"{item_id}: stored type differs from the rules")
        except (ValueError, KeyError) as exc:
            issues.append(f"{item_id}: invalid shares ({exc})")
        if items[item_id]["mode"] == "small" and not {"perennial_structure", "built"} <= set(final.get("flags", {})):
            issues.append(f"{item_id}: small parcel without required flags")
        # Recompute with the current rules (round 1 stored results from an earlier, faulty perennial check).
        from alphaearth_photo_review import validate_answer
        try:
            current = validate_answer(items[item_id], {**final, "stage": "final"})["contradictions"]
            if "natural_ground_crop_season" not in CONFIGS.get(final.get("rules"), CONFIG)["rules"]:
                current = [c for c in current if not c.startswith("natural ground but")]  # rule added in v2_2
            if "alfalfa_cut_signature" not in CONFIGS.get(final.get("rules"), CONFIG)["rules"]:
                current = [c for c in current if not c.startswith(("repeated cuts like alfalfa", "alfalfa but no year"))]
        except (ValueError, KeyError):
            current = final.get("contradictions", [])
        if current:
            contradicted += 1
        types[final["decision"]["type"]] += 1
    return {"final": finals, "types": dict(types), "deferral_rate": round(deferred / finals, 3) if finals else None,
            "contradiction_rate": round(contradicted / finals, 3) if finals else None, "issues": issues}


def live_claims(queue, items, now=None):
    """Parcels whose unit is still claimed by a reviewer (lease not expired)."""
    import time
    now = time.time() if now is None else now
    data = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    members = {b["id"]: b["items"] for b in data.get("blocks", [])} or {i: [i] for i in items}
    path = OUT / "claims" / f"{queue}.json"
    claims = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return frozenset(i for u, c in claims.items() if c["until"] > now for i in members.get(u, []))


def process_health(queue, items, done, now=None):
    """Crash and tool checks: claims that expired with parcels still open (a reviewer stopped mid-work) and
    problems logged by the server and CLI (rejected saves, failed renders, bad answer format)."""
    import time
    now = time.time() if now is None else now
    data = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))
    members = {b["id"]: b["items"] for b in data.get("blocks", [])} or {i: [i] for i in items}
    claims_path = OUT / "claims" / f"{queue}.json"
    claims = json.loads(claims_path.read_text(encoding="utf-8")) if claims_path.exists() else {}
    abandoned = {u: {"reviewer": c["reviewer"], "open": [i for i in members.get(u, []) if i not in done]}
                 for u, c in claims.items() if c["until"] <= now and any(i not in done for i in members.get(u, []))}
    log = OUT / "logs" / "problems.jsonl"
    problems = [p for p in (json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip())
                if p.get("queue") == queue] if log.exists() else []
    return {"open_parcels": len(items) - len(done), "abandoned_units": abandoned, "tool_problems": len(problems),
            "tool_problems_by_kind": dict(collections.Counter(p["kind"] for p in problems))}


def main():
    queue = sys.argv[1] if len(sys.argv) > 1 else "gold_accuracy"
    items = {i["id"]: i for i in json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))["items"]}
    pause = CONFIG["watchdog"]["pause"]
    report = {"queue": queue, "parcels": len(items), "reviewers": {}, "pause_triggers": []}
    live = live_claims(queue, items)  # read the (large) queue once, not once per reviewer
    for path in sorted((OUT / "answers").glob(f"{queue}__*.json")):
        reviewer = path.stem.split("__", 1)[1]
        if reviewer == "owner":
            continue  # owner answers are checked separately, never mixed with reviewer statistics
        r = check_reviewer(path, items, live)
        report["reviewers"][reviewer] = r
        if r["final"] >= 10 and json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8")).get("forced_choice"):
            report.setdefault("recorded_only", []).append(f"{reviewer}: deferral {r['deferral_rate']}, contradictions {r['contradiction_rate']}")
        elif r["final"] >= 10:  # forced-choice queues (owner): deferral/contradiction rates are recorded, not triggers
            if r["contradiction_rate"] > pause["contradictions_above"]:
                report["pause_triggers"].append(f"{reviewer}: contradiction rate {r['contradiction_rate']}")
            low, high = pause["deferral_outside"]
            if not low <= r["deferral_rate"] <= high:
                report["pause_triggers"].append(f"{reviewer}: deferral rate {r['deferral_rate']} outside {low}-{high}")
        if r["issues"]:
            report["pause_triggers"].append(f"{reviewer}: {len(r['issues'])} integrity issue(s)")
    done = set()
    for path in (OUT / "answers").glob(f"{queue}__*.json"):
        if not path.stem.endswith("__owner"):
            done |= {k for k, v in json.loads(path.read_text(encoding="utf-8")).items() if "final" in v}
    report["parcels_final"] = len(done)
    report.update(process_health(queue, items, done))
    if report["tool_problems"] > max(5, .1 * max(len(done), 1)):
        report["pause_triggers"].append(f"{report['tool_problems']} tool problems logged (over 10% of answered parcels)")
    bad = []
    for slug in SEALS:
        for name, rec in json.loads((ROOT / "config" / f"{slug}.review.lock.json").read_text())["files"].items():
            if not (ROOT / name).exists() or hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != rec["sha256"]:
                bad.append(name)
    report["release_seals_intact"] = not bad
    if bad:
        report["pause_triggers"].append(f"release seal changed: {bad[:3]}")
    report["disk_free_gb"] = round(shutil.disk_usage(ROOT).free / 1e9, 1)
    if report["disk_free_gb"] < 5:
        report["pause_triggers"].append("disk space below 5 GB")
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
