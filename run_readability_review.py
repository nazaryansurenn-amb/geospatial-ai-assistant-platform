"""A presentation-only overlay on the approved area50 working version."""
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import run_activity_change_area50_review as base
import app

ROOT = Path(__file__).resolve().parent
SLUG = "readability_20260907_v1"
ASSETS = {
    "/": ("readability-20260907-v1.html", "text/html; charset=utf-8"),
    "/index.html": ("readability-20260907-v1.html", "text/html; charset=utf-8"),
    "/ui/readability-20260907-v1.css": ("readability-20260907-v1.css", "text/css; charset=utf-8"),
}


def install_handler(parent):
    class ReadabilityHandler(parent):
        def _serve_ui(self, head=False):
            asset = ASSETS.get(urlsplit(self.path).path)
            if not asset:
                return False
            name, mime = asset
            body = (ROOT / "ui" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return True

        def do_GET(self):
            if not self._serve_ui():
                super().do_GET()

        def do_HEAD(self):
            if not self._serve_ui(head=True):
                super().do_HEAD()

    return ReadabilityHandler


def main():
    lock = ROOT / "config" / f"{SLUG}.review.lock.json"
    if lock.exists():
        for name, record in json.loads(lock.read_text())["files"].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == record["sha256"], name
    else:
        assert "--port" in sys.argv and sys.argv[sys.argv.index("--port") + 1] == "8529", "Unsealed preview is 8529 only"
    serve = app.main

    def with_readability():
        app.ProductRequestHandler = install_handler(app.ProductRequestHandler)
        serve()

    app.main = with_readability
    base.main()


if __name__ == "__main__":
    main()
