"""Verify the approved typography deployment against its pre-switch snapshot."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from verify_activity_change_http import get

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server_data/review/readability_20260907_v1"


def main():
    baseline = json.loads((OUT / "http_preview.json").read_text())
    for path, digest in baseline["identical_api_responses"].items():
        status, body = get(8526, path)
        assert status == 200 and hashlib.sha256(body).hexdigest() == digest, path
    for path, name in [("/", "readability-20260907-v1.html"), ("/index.html", "readability-20260907-v1.html"),
                       ("/ui/readability-20260907-v1.css", "readability-20260907-v1.css")]:
        status, body = get(8526, path)
        assert status == 200 and body == (ROOT / "ui" / name).read_bytes(), path
    for path in ["/.env", "/ui/readability-20260907-v1.html", "/run_readability_review.py",
                 "/server_data/review/readability_20260907_v1/http_preview.json"]:
        assert get(8526, path)[0] == 404, path
    previous = json.loads((ROOT / "server_data/review/activity_change_area50_20260907_v1/http_activation.json").read_text())
    assert hashlib.sha256(get(8525, "/")[1]).hexdigest() == previous["8525_html_sha256"]
    result = {"passed": True, "port": 8526, "version": "readability_20260907_v1",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "owner_approval": "okay apply on 8526", "identical_api_responses": len(baseline["identical_api_responses"]),
              "approved_html_css_served": True, "private_paths_rejected": True, "8525_unchanged": True,
              "rollback_launcher": "run_activity_change_area50_review.py"}
    (OUT / "http_activation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
