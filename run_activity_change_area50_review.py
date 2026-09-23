"""Replace only cultivation-change results on the preserved seven-section product."""
import os
os.environ["LAND_ANALYTICS_PROFILE"] = "use_type_v2"
os.environ["WORKING_PRODUCT_HOST"] = "127.0.0.1"
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
import app
import run_agent_review_v6 as base
from wp_core import agent_service_v6
from wp_core.activity_change_area50_agent import install

ROOT = Path(__file__).resolve().parent
SLUG = "activity_change_area50_20260907_v1"


def main():
    lock = ROOT / "config" / f"{SLUG}.review.lock.json"
    if lock.exists():
        for name, record in json.loads(lock.read_text())["files"].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == record["sha256"], name
    else:
        assert "--port" in sys.argv and sys.argv[sys.argv.index("--port") + 1] == "8528", "Unsealed preview is local 8528 only"
    review = ROOT / "server_data/review" / SLUG
    summary = json.loads((review / "summary.json").read_text())
    lookup = json.loads((review / "parcel_lookup.json").read_text())
    parent = app.ProductRequestHandler

    class FinalParcelResponse(parent):
        def _send_json(self, payload, status=200):
            request = urlsplit(self.path)
            if request.path == "/api/land/parcel" and status == 200 and "error" not in payload:
                code = parse_qs(request.query).get("code", [""])[0].strip()
                payload = {**payload, "activityChange": lookup.get(code)}
            super()._send_json(payload, status=status)

    # Base is last in the response chain, so older wrappers cannot overwrite this
    # version's change field. Every unrelated response field remains intact.
    app.ProductRequestHandler = FinalParcelResponse
    serve = app.main

    def with_area50():
        parent_handler = app.ProductRequestHandler
        class Handler(parent_handler):
            def do_GET(self):
                if urlsplit(self.path).path == "/api/land/activity-change":
                    self._send_json(summary)
                    return
                super().do_GET()
        app.ProductRequestHandler = Handler
        app.DIST_ROOT = ROOT / "output" / f"frontend_{SLUG}"
        install(agent_service_v6, summary, lookup)
        serve()
    app.main = with_area50
    base.main()


if __name__ == "__main__":
    main()
