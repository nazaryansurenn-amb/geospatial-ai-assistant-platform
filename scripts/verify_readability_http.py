"""Read-only regression against the active analytical version."""
import hashlib
import json
from pathlib import Path
import sys
from verify_activity_change_http import get

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/readability_20260907_v1"


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8529
    paths = ["/health", "/api/land/delivery", "/api/land/activity-2026", "/api/land/history-2021-2025",
             "/api/land/use-type", "/api/land/consolidation", "/api/land/degradation", "/api/land/activity-change"]
    before = json.loads((ROOT / "server_data/review/activity_change_area50_20260907_v1/http_activation.json").read_text())
    paths += ["/api/land/parcel?code=" + code for code in before["parcel_regression"]]
    verified = {}
    for path in paths:
        live_status, live = get(8526, path)
        status, body = get(port, path)
        assert status == live_status == 200 and body == live, path
        verified[path] = hashlib.sha256(body).hexdigest()
    for path, name in [("/", "readability-20260907-v1.html"), ("/ui/readability-20260907-v1.css", "readability-20260907-v1.css")]:
        status, body = get(port, path)
        assert status == 200 and body == (ROOT / "ui" / name).read_bytes(), path
    for path in ["/.env", "/ui/readability-20260907-v1.html", "/run_readability_review.py", "/server_data/review/readability_20260907_v1/http_preview.json"]:
        assert get(port, path)[0] == 404, path
    assert hashlib.sha256(get(8525, "/")[1]).hexdigest() == before["8525_html_sha256"]
    assert hashlib.sha256(get(8526, "/")[1]).hexdigest() == before["html_sha256"]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "http_preview.json").write_text(json.dumps({"passed":True, "port":port, "baseline":8526,
        "identical_api_responses":verified, "8525_8526_unchanged":True, "private_paths_rejected":True},indent=2),encoding="utf-8")
    print(json.dumps({"passed":True,"identical_responses":len(verified),"baseline_unchanged":True}))


if __name__ == "__main__":
    main()
