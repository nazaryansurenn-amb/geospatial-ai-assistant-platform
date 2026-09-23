"""Confirm that the fifth-tab revision changes no existing API or working 8525."""
import json
import sys
from pathlib import Path

from verify_consolidation_review_http import get, digest
from run_observation_analysis import write_json, now

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT / "server_data/review/consolidation_review_20260906_v2"
FRONTEND = ROOT / "output/frontend_consolidation_review_20260906_v2"


def verify(phase):
    port = 8527 if phase == "before" else 8526
    lookup = json.loads((REVIEW / "parcel_lookup.json").read_text())
    codes = sorted(lookup)[::20] + ["04-087-0119-0005", "04-002-0145-0007", "04-023-0135-0011"]
    paths = ["/health", "/api/land/delivery", "/api/land/consolidation"]
    paths += [f"/api/land/parcel?code={code}" for code in codes]
    replies = {}
    for path in paths:
        status, body = get(port, path)
        assert status == 200, path
        replies[path] = json.loads(body)
        if phase == "before":
            old_status, old_body = get(8526, path)
            assert old_status == status and json.loads(old_body) == replies[path], path
    assert get(port, "/")[1] == (FRONTEND / "index.html").read_bytes()
    working = digest(get(8525, "/")[1])
    if phase == "after":
        baseline = json.loads((REVIEW / "http_before.json").read_text())
        assert replies == baseline["api_replies"]
        assert working == baseline["working_8525_html_sha256"]
    for path in ["/server_data/review/consolidation_review_20260906_v2/parcel_lookup.json",
                 "/config/consolidation_review_20260906_v2.review.lock.json", "/run_consolidation_review_v2.py"]:
        assert get(port, path)[0] in (403, 404), path
    assert get(port, "/api/land/parcel?code=invalid")[0] == 400
    assert get(port, "/api/land/parcel?code=99-999-9999-9999")[0] == 404
    write_json(REVIEW / f"http_{phase}.json", {"passed": True, "verified_at": now(),
        "api_replies": replies, "working_8525_html_sha256": working, "port": port,
        "all_public_api_responses_unchanged": True, "private_routes_blocked": True})
    print(f"{phase}: {len(paths)} API responses unchanged; private routes blocked; 8525 preserved")


if __name__ == "__main__":
    assert sys.argv[1] in ("before", "after")
    verify(sys.argv[1])
