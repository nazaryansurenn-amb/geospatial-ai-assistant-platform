"""Local read-only API regression and deployment evidence for the new tab."""
from pathlib import Path
import hashlib
import json
import sys
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SLUG = "activity_change_history_review_20260906_v1"
REVIEW = ROOT / "server_data/review" / SLUG


def get(port, path):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main():
    port = int(sys.argv[1])
    phase = "preview" if port == 8527 else "activation"
    result = {"phase": phase, "port": port, "passed": False, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "api": {}}
    endpoints = ["/health", "/api/land/delivery", "/api/land/activity-2026", "/api/land/history-2021-2025", "/api/land/use-type", "/api/land/consolidation"]
    if phase == "activation":
        baseline = json.loads((REVIEW / "http_preview.json").read_text())
    for path in endpoints:
        status, body = get(port, path)
        assert status == 200, path
        if phase == "preview":
            assert json.loads(body) == json.loads(get(8526, path)[1]), path
        else:
            assert hashlib.sha256(body).hexdigest() == baseline["api"][path], path
        result["api"][path] = hashlib.sha256(body).hexdigest()
    reference = json.loads((REVIEW / "summary.json").read_text())
    status, body = get(port, "/api/land/activity-change")
    assert status == 200 and json.loads(body) == reference
    assert len(body) < 20000
    result["summary_bytes"] = len(body)
    lookup = json.loads((REVIEW / "parcel_lookup.json").read_text())
    samples = {}
    for code, row in lookup.items():
        samples.setdefault((row["changeClass"], row["changeYear"]), code)
    checked = {}
    for code in samples.values():
        path = f"/api/land/parcel?code={code}"
        status, body = get(port, path)
        assert status == 200
        payload = json.loads(body)
        assert payload.pop("activityChange") == lookup[code]
        if phase == "preview":
            assert payload == json.loads(get(8526, path)[1]), code
        else:
            assert payload == baseline["parcel_regression"][code], code
        checked[code] = payload
    result["parcel_regression"] = checked
    for path, expected in [("/api/land/parcel?code=invalid", 400), ("/api/land/parcel?code=99-999-9999-9999", 404),
                           ("/.env", 404), ("/server_data/review/" + SLUG + "/parcel_lookup.json", 404),
                           ("/data/analysis/activity_change/" + SLUG + "/register.parquet", 404),
                           ("/wp_core/activity_change_history.py", 404)]:
        assert get(port, path)[0] == expected, path
    result["private_paths_rejected"] = True
    stable_hash = hashlib.sha256(get(8525, "/")[1]).hexdigest()
    result["8525_html_sha256"] = stable_hash
    html = get(port, "/")[1]
    expected_html = (ROOT / "output" / f"frontend_{SLUG}" / "index.html").read_bytes()
    assert html == expected_html
    result["html_sha256"] = hashlib.sha256(html).hexdigest()
    if phase == "preview":
        old_html = get(8526, "/")[1]
        assert old_html == (ROOT / "output/frontend_agent_review_20260906_v5/index.html").read_bytes()
        result["previous_8526_html_sha256"] = hashlib.sha256(old_html).hexdigest()
    else:
        assert stable_hash == baseline["8525_html_sha256"]
    result["passed"] = True
    (REVIEW / f"http_{phase}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed": True, "phase": phase, "unchanged_endpoints": len(endpoints), "parcel_samples": len(checked), "summary_bytes": result["summary_bytes"]}, indent=2))


if __name__ == "__main__":
    main()
