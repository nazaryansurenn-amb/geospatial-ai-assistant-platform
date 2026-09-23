"""Serve the additive, sealed potential review; preserve the original app module."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
SLUG = "land_potential_20260906_v1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8526)
    args = parser.parse_args()
    lock = json.loads((ROOT / "config" / f"{SLUG}.review.lock.json").read_text())
    for name, record in lock["files"].items():
        path = (ROOT / name).resolve()
        assert ROOT in path.parents
        assert path.stat().st_size == record["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"], name
    os.environ["LAND_ANALYTICS_PROFILE"] = "use_type_v2"
    os.environ["WORKING_PRODUCT_HOST"] = "127.0.0.1"
    os.environ["WORKING_PRODUCT_PORT"] = str(args.port)
    import app
    app.DIST_ROOT = ROOT / "output" / f"frontend_{SLUG}"
    app.LAND_ANALYTICS_INDEX = ROOT / "server_data" / f"land_analytics_{SLUG}.sqlite3"
    app.LAND_ANALYTICS_SUMMARY = ROOT / "server_data" / f"land_analytics_summary_{SLUG}.json"
    class PotentialHandler(app.ProductRequestHandler):
        def _send_json(self, payload, status=200):
            req = urlsplit(self.path)
            if req.path == "/api/land/parcel" and status == 200 and "error" not in payload:
                code = parse_qs(req.query).get("code", [""])[0]
                with sqlite3.connect(app.LAND_ANALYTICS_INDEX.as_uri()+"?mode=ro", uri=True) as c:
                    row = c.execute("SELECT potential_class FROM parcel_analytics WHERE cadastre_code=?", (code,)).fetchone()
                payload = dict(payload)
                payload["potential"] = {"potentialClass": row[0] if row else "not_candidate"}
            super()._send_json(payload, status=status)
    app.ProductRequestHandler = PotentialHandler
    app.main()


if __name__ == "__main__":
    main()
