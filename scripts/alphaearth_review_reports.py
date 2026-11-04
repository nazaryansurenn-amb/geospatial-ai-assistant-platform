"""Two read-only owner reports for a finished review queue (no AI, no tokens).

  alphaearth_review_reports.py QUEUE

Writes docs/ALPHAEARTH_<QUEUE>_CONFIDENCE_REPORT_EN.md (how sure the AI was on the parcels it classified) and
docs/ALPHAEARTH_<QUEUE>_DEFERRED_REPORT_EN.md (why parcels were sent to the owner). Reasons are grouped by keywords
in the reviewers' own short notes; one parcel can have several reasons, and its first match is its main reason.
"""
from __future__ import annotations
import collections
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/alphaearth_photo_review_20261030_v1"
NAMES = {"ANN": "Annual crops", "ALF": "Alfalfa", "VIN": "Vineyard", "ORC": "Orchard", "GRH": "Greenhouses",
         "NCU": "Not cultivated in 2025", "GRS": "Grass / weeds", "BLT": "Buildings / yards / tracks", "WAT": "Water",
         "NAT": "Natural ground", "TRE": "Non-fruit trees", "MIX": "Mixed cultivated", "PART": "Partly cultivated"}
# (label, pattern) in priority order: the first match is a parcel's main reason
REASONS = [
    ("Thin strip or signal mixed with neighbouring fields", r"thin|narrow|strip|sliver|neighbo|edge|verge|mixed pixel|colou?red by|wedge|corner"),
    ("Old photo only (2021-22)", r"\bold\b|2021 photo|2022 photo|older photo|outdated"),
    ("Orchard vs vineyard vs other trees", r"orchard|vine|tree|sapling|young plant|garden|canopy"),
    ("Green all season: alfalfa vs grass vs crop", r"alfalfa|green all|green most|all-season|all season|grass|meadow|weed|\bdips?\b|\bcuts?\b"),
    ("Photo and satellite signal disagree", r"signal|contradict|conflict|disagree|but the|whereas|while the|ndvi"),
    ("Buildings, yards, tracks or ditches mixed in", r"yard|house|build|barn|shed|road|track|ditch|canal|structure"),
    ("Photo unclear (shadow, haze, dark)", r"shadow|unclear|blur|haze|cloud|dark|not visible|cannot see|can't see|hard to see"),
    ("Greenhouses or tunnels", r"greenhouse|tunnel|plastic"),
    ("Water or wet ground", r"water|wet|pond|marsh|reed"),
]


def load(queue):
    items = {i["id"]: i for i in json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))["items"]}
    fixed = json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8")).get("fixed_status") or []
    finals = {}
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        for item_id, entry in json.loads(path.read_text(encoding="utf-8")).items():
            if entry.get("final"):
                finals[item_id] = entry
    return items, fixed, finals


def pct(a, b):
    return f"{a / b:.0%}" if b else "-"


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def confidence_report(queue, items, fixed, finals):
    typed = {k: e["final"] for k, e in finals.items()
             if not e["final"].get("defer") and e["final"]["decision"]["type"] != "DEFER"}
    by_conf = collections.Counter(f.get("confidence") for f in typed.values())
    area = collections.Counter()
    for k, f in typed.items():
        area[f.get("confidence")] += items[k]["area_ha"]
    n = len(typed)
    lines = [f"# Confidence of the AI-classified parcels: {queue}", "",
             f"Generated {date.today()} from the saved answers (read-only, no AI). AI-interpreted from imagery; not field-verified.", "",
             "## 1. Overview",
             table(["", "Parcels", "Share", "Area (ha)"],
                   [["Classified by the AI", n, "100%", f"{sum(area.values()):,.0f}"],
                    ["High confidence", by_conf["high"], pct(by_conf["high"], n), f"{area['high']:,.0f}"],
                    ["Medium confidence", by_conf["medium"], pct(by_conf["medium"], n), f"{area['medium']:,.0f}"]]),
             "",
             f"Parcels deferred to the owner: {len(finals) - n:,}. Fixed 'not classifiable from imagery' (under 0.01 ha or 5 m wide): {len(fixed):,}.",
             "",
             "**What confidence meant in the owner's blind test** (300-parcel test, rules v2_2): high-confidence answers matched "
             "the owner 10 of 11 times (91%), medium ones 23 of 39 times (59%). The samples are small and the rules have been "
             "improved since, so read these as rough guides.", ""]
    rows = []
    for t in sorted({f["decision"]["type"] for f in typed.values()}, key=lambda t: -sum(f["decision"]["type"] == t for f in typed.values())):
        sel = [k for k, f in typed.items() if f["decision"]["type"] == t]
        hi = sum(typed[k].get("confidence") == "high" for k in sel)
        rows.append([f"{NAMES.get(t, t)} ({t})", len(sel), hi, len(sel) - hi, pct(hi, len(sel)),
                     f"{sum(items[k]['area_ha'] for k in sel):,.0f}"])
    lines += ["## 2. By land type", table(["Type", "Parcels", "High", "Medium", "High share", "Area (ha)"], rows), ""]

    def split(label, test):
        sel = [k for k in typed if test(items[k])]
        hi = sum(typed[k].get("confidence") == "high" for k in sel)
        return [label, len(sel), hi, len(sel) - hi, pct(hi, len(sel))]
    lines += ["## 3. By parcel size and photo",
              table(["Group", "Parcels", "High", "Medium", "High share"],
                    [split("Large (0.5 ha or more)", lambda i: i["mode"] != "small"),
                     split("Small (under 0.5 ha)", lambda i: i["mode"] == "small"),
                     split("Narrower than 10 m", lambda i: i["mean_width_m"] < 10),
                     split("Current photo (2024+)", lambda i: i["photo"]["current"]),
                     split("Old photo only (2021-22)", lambda i: not i["photo"]["current"])]), ""]
    warned = [k for k, f in typed.items() if f.get("contradictions")]
    converted = [k for k, f in typed.items() if f["decision"].get("converted_from") == "NAT"]
    uncertain = [k for k, f in typed.items() if f["decision"].get("attribute") == "uncertain_2025_activity"]
    lines += ["## 4. Extra flags on classified parcels",
              table(["Flag", "Parcels", "What it means"],
                    [["Warning kept on the answer", len(warned), "The satellite check disagreed and the reviewer kept its answer"],
                     ["Natural ground converted by crop history", len(converted), "Owner rule: a crop season 2021-2025 makes it field land (NCU/ANN)"],
                     ["Annual, uncertain 2025 activity", len(uncertain), "Only a weak spring green-up in 2025"]]), ""]
    medium_rare = sum(1 for f in typed.values() if f.get("confidence") == "medium" and f["decision"]["type"] in ("ALF", "VIN", "ORC", "TRE", "GRH", "GRS"))
    lines += ["## 5. Suggested re-check order (for the owner, optional)",
              f"1. Medium-confidence answers of the less common types (alfalfa, vineyard, orchard, non-fruit trees, greenhouses, grass): **{medium_rare:,}** parcels.",
              f"2. Answers with a kept warning: **{len(warned):,}** parcels.",
              f"3. All other medium-confidence answers: **{by_conf['medium'] - medium_rare:,}** parcels (some overlap with 2).",
              "", "High-confidence answers can usually be accepted, with a small random check."]
    return "\n".join(lines) + "\n"


def deferred_report(queue, items, finals):
    deferred = {k: e for k, e in finals.items()
                if e["final"].get("defer") or e["final"]["decision"]["type"] == "DEFER"}
    n = len(deferred)
    main = collections.Counter()
    any_ = collections.Counter()
    main_area = collections.Counter()
    examples = collections.defaultdict(list)
    for k, e in deferred.items():
        reason = (e["final"].get("reason") or e.get("photo", {}).get("reason") or "").lower()
        hits = [label for label, pat in REASONS if re.search(pat, reason)]
        first = hits[0] if hits else "Other / not stated"
        main[first] += 1
        main_area[first] += items[k]["area_ha"]
        for h in hits or ["Other / not stated"]:
            any_[h] += 1
        if len(examples[first]) < 3 and reason:
            examples[first].append(f"{k}: {reason[:110]}")
    stage_photo = sum(1 for e in deferred.values() if (e.get("photo") or {}).get("defer"))
    by_rules = sum(1 for e in deferred.values() if not e["final"].get("defer"))
    total_area = sum(items[k]["area_ha"] for k in deferred)
    lines = [f"# Parcels deferred to the owner: {queue}", "",
             f"Generated {date.today()} from the saved answers (read-only, no AI).", "",
             "## 1. Overview",
             table(["", "Value"],
                   [["Deferred parcels", f"{n:,} ({pct(n, len(finals))} of reviewed)"],
                    ["Total area", f"{total_area:,.0f} ha"],
                    ["Deferred at the photo step (before seeing the satellite signals)", f"{stage_photo:,}"],
                    ["Deferred at the final step (after the satellite signals)", f"{n - stage_photo - by_rules:,}"],
                    ["Deferred by the rules (not by the reviewer)", f"{by_rules:,}"],
                    ["Small parcels (under 0.5 ha)", pct(sum(items[k]['mode'] == 'small' for k in deferred), n)],
                    ["Narrower than 10 m", pct(sum(items[k]['mean_width_m'] < 10 for k in deferred), n)],
                    ["Old photo only (2021-22)", pct(sum(not items[k]['photo']['current'] for k in deferred), n)],
                    ["Estimated owner time at 17 s per parcel", f"about {n * 17 / 3600:.0f} hours"]]), "",
             "## 2. Main reason (one per parcel)",
             table(["Reason", "Parcels", "Share", "Area (ha)"],
                   [[r, c, pct(c, n), f"{main_area[r]:,.0f}"] for r, c in main.most_common()]), "",
             "## 3. All reasons mentioned (a parcel can have several)",
             table(["Reason", "Parcels", "Share"], [[r, c, pct(c, n)] for r, c in any_.most_common()]), "",
             "## 4. Examples of the reviewers' own notes"]
    for r, ex in examples.items():
        lines.append(f"**{r}**")
        lines += [f"- {x}" for x in ex]
        lines.append("")
    sizes = collections.Counter("under 0.1 ha" if items[k]["area_ha"] < .1 else "0.1-0.5 ha" if items[k]["area_ha"] < .5
                                else "0.5-2 ha" if items[k]["area_ha"] < 2 else "2 ha or more" for k in deferred)
    lines += ["## 5. By parcel size",
              table(["Size", "Parcels", "Share"], [[s, sizes[s], pct(sizes[s], n)] for s in ("under 0.1 ha", "0.1-0.5 ha", "0.5-2 ha", "2 ha or more")]),
              "", "Labelling the largest parcels first covers the most land in the least time."]
    return "\n".join(lines) + "\n"


def main():
    queue = sys.argv[1]
    items, fixed, finals = load(queue)
    tag = queue.upper()
    out1 = ROOT / "docs" / f"ALPHAEARTH_{tag}_CONFIDENCE_REPORT_EN.md"
    out2 = ROOT / "docs" / f"ALPHAEARTH_{tag}_DEFERRED_REPORT_EN.md"
    out1.write_text(confidence_report(queue, items, fixed, finals), encoding="utf-8")
    out2.write_text(deferred_report(queue, items, finals), encoding="utf-8")
    print(out1)
    print(out2)


if __name__ == "__main__":
    main()
