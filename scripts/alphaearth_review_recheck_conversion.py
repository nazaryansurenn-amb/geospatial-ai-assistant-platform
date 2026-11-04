"""Re-decide saved reviewer answers that contain natural ground, after the v2_4 conversion fix (f15386).

  alphaearth_review_recheck_conversion.py QUEUE            list what would change (dry run)
  alphaearth_review_recheck_conversion.py QUEUE --apply    rewrite the changed decisions (review server must be stopped)

The first v2_4 code converted the whole parcel when the decided type was NAT; the fix converts only the
natural-ground share and decides again. Only records of the current rules version are touched; each changed record
keeps its old decision under "decision_before_fix".
"""
from __future__ import annotations
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alphaearth_photo_review import OUT, PORT, TYPES_CONFIG, validate_answer  # noqa: E402


def main():
    queue, apply = sys.argv[1], "--apply" in sys.argv
    if apply:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            raise SystemExit("Stop the review server first: it rewrites answer files while running.")
        except OSError:
            pass
    items = {i["id"]: i for i in json.loads((OUT / "queues" / f"{queue}.json").read_text(encoding="utf-8"))["items"]}
    changed = 0
    for path in sorted((OUT / "answers").glob(f"{queue}__claude*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item_id, entry in data.items():
            final = entry.get("final")
            if not final or final.get("defer") or "NAT" not in (final.get("shares") or {}) \
                    or final.get("rules") != TYPES_CONFIG["version"]:
                continue
            fresh = validate_answer(items[item_id], {**final, "stage": "final"})
            if fresh["decision"] != final["decision"]:
                changed += 1
                print(f"{item_id}: {final['shares']} {final['decision']['type']} -> {fresh['decision']['type']}")
                if apply:
                    final["decision_before_fix"] = final["decision"]
                    final["decision"] = fresh["decision"]
                    final["contradictions"] = fresh["contradictions"]
                    final["fixed_at"] = datetime.now(timezone.utc).isoformat()
        if apply:
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{changed} decisions {'rewritten' if apply else 'would change'}")


if __name__ == "__main__":
    main()
