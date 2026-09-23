"""Check additive HTTP behavior and preserve 8525 across the authorized switch."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.request import urlopen

from run_observation_analysis import now, write_json

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT / "server_data/review/consolidation_review_20260906_v1"
FRONTEND = ROOT / "output/frontend_consolidation_review_20260906_v1"


def get(port, path):
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def digest(body):
    return hashlib.sha256(body).hexdigest()


def execute(phase):
    port = 8527 if phase == "before" else 8526
    assert get(8525, "/health")[0] == get(port, "/health")[0] == 200
    working = digest(get(8525, "/")[1])
    assert get(port, "/")[1] == (FRONTEND / "index.html").read_bytes()
    summary = json.loads(get(port, "/api/land/consolidation")[1])
    assert summary == json.loads((REVIEW / "summary.json").read_text())
    assert summary["summaries"]["lower_hrazdan"]["all"]["groups"] == 31
    assert summary["summaries"]["lower_hrazdan"]["priority"]["groups"] == 20
    lookup = json.loads((REVIEW / "parcel_lookup.json").read_text())
    codes = sorted(lookup)[::20] + ["04-087-0119-0005", "04-002-0145-0007", "04-023-0135-0011"]
    index = ROOT / "server_data/land_analytics_land_potential_1km_review_20260906_v1.sqlite3"
    with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as c:
        codes.append(c.execute("SELECT cadastre_code FROM potential_expansion WHERE potential_class='gravity_candidate' LIMIT 1").fetchone()[0])
    replies = {}
    for code in codes:
        status, body = get(port, f"/api/land/parcel?code={code}")
        assert status == 200, code
        reply = json.loads(body)
        assert reply.pop("consolidation") == lookup.get(code)
        if phase == "before":
            old_status, old = get(8526, f"/api/land/parcel?code={code}")
            assert old_status == 200 and reply == json.loads(old), code
        replies[code] = reply
    assert get(port, "/api/land/parcel?code=invalid")[0] == 400
    assert get(port, "/api/land/parcel?code=99-999-9999-9999")[0] == 404
    privacy = ["/server_data/review/consolidation_review_20260906_v1/parcel_lookup.json",
               "/data/analysis/land_consolidation/consolidation_strip_priority_20260906_v1/report.json",
               "/config/consolidation_strip_priority_20260906_v1.json", "/wp_core/consolidation_delivery.py"]
    for path in privacy:
        assert get(port, path)[0] in (403, 404), path
    previous_api = json.loads(get(port, "/api/land/delivery")[1])
    if phase == "before":
        assert get(8526, "/")[1] == (ROOT / "output/frontend_land_potential_1km_review_20260906_v2/index.html").read_bytes()
        assert previous_api == json.loads(get(8526, "/api/land/delivery")[1])
    else:
        before = json.loads((REVIEW / "http_before.json").read_text())
        assert working == before["working_8525_html_sha256"]
        assert replies == before["prior_parcel_replies"]
        assert digest(json.dumps(previous_api, sort_keys=True).encode()) == before["prior_delivery_sha256"]
    result = {"passed": True, "verified_at": now(), "port": port, "working_8525_html_sha256": working,
        "new_html_sha256": digest(get(port, "/")[1]), "prior_parcel_replies": replies,
        "prior_delivery_sha256": digest(json.dumps(previous_api, sort_keys=True).encode()),
        "sampled_parcels": len(codes), "prior_land_potential_and_parcel_fields_preserved": True,
        "private_paths_blocked": privacy, "groups": 31, "priority_groups": 20}
    write_json(REVIEW / f"http_{phase}.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "prior_parcel_replies"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["before", "after"])
    execute(parser.parse_args().phase)
