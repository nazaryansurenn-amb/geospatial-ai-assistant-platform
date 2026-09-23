"""Local-only additive 8526 consolidation review; retain the existing parcel APIs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
SLUG = "consolidation_review_20260906_v1"
DATA_SLUG = "land_potential_1km_review_20260906_v1"
BASE_SLUG = "land_potential_1km_review_20260906_v2"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8526)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if not args.preview:
        for slug in (BASE_SLUG, SLUG):
            for name, record in json.loads((ROOT / "config" / f"{slug}.review.lock.json").read_text())["files"].items():
                path = (ROOT / name).resolve()
                assert ROOT in path.parents
                assert path.stat().st_size == record["bytes"] and hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"], name
    os.environ["LAND_ANALYTICS_PROFILE"] = "use_type_v2"
    os.environ["WORKING_PRODUCT_HOST"] = "127.0.0.1"
    os.environ["WORKING_PRODUCT_PORT"] = str(args.port)
    import app
    app.DIST_ROOT = ROOT / "output" / f"frontend_{SLUG}"
    app.LAND_ANALYTICS_INDEX = ROOT / "server_data" / f"land_analytics_{DATA_SLUG}.sqlite3"
    app.LAND_ANALYTICS_SUMMARY = ROOT / "server_data" / f"land_analytics_summary_{DATA_SLUG}.json"
    review = ROOT / "server_data/review" / SLUG
    summary = json.loads((review / "summary.json").read_text())
    lookup = json.loads((review / "parcel_lookup.json").read_text())

    class Handler(app.ProductRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path == "/api/land/consolidation":
                self._send_json(summary)
                return
            super().do_GET()

        def _land_parcel(self, query):
            code = query.get("code", [""])[0].strip()
            if app.CADASTRE_CODE_RE.fullmatch(code):
                with sqlite3.connect(app.LAND_ANALYTICS_INDEX.as_uri() + "?mode=ro", uri=True) as c:
                    row = c.execute("SELECT potential_class,household_agriculture,road_excluded FROM potential_expansion WHERE cadastre_code=?", (code,)).fetchone()
                if row:
                    self._send_json({"potential": {"potentialClass": row[0], "scope": "nearby_1km"}, "activity": {"household": bool(row[1]), "roadExcluded": bool(row[2])}})
                    return
            super()._land_parcel(query)

        def _send_json(self, payload, status=200):
            request = urlsplit(self.path)
            if request.path == "/api/land/parcel" and status == 200 and "error" not in payload:
                code = parse_qs(request.query).get("code", [""])[0].strip()
                payload = dict(payload)
                payload["consolidation"] = lookup.get(code)
                if "potential" not in payload:
                    with sqlite3.connect(app.LAND_ANALYTICS_INDEX.as_uri() + "?mode=ro", uri=True) as c:
                        row = c.execute("SELECT potential_class FROM parcel_analytics WHERE cadastre_code=?", (code,)).fetchone()
                    payload["potential"] = {"potentialClass": row[0] if row else "not_candidate", "scope": "inside_I_II"}
            super()._send_json(payload, status=status)

    app.ProductRequestHandler = Handler
    app.main()


if __name__ == "__main__":
    main()
