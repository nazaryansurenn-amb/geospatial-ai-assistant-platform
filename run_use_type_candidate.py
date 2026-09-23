"""Owner-only candidate on 8526, independent of preserved running releases."""
from __future__ import annotations
import os
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from wp_core.use_type_release import CANDIDATE_SLUG as SLUG


def main():
    from release_tools import local_path, sha256
    lock = json.loads((ROOT / "config" / f"{SLUG}.lock.json").read_text())
    for relative, record in lock['files'].items():
        path = local_path(ROOT, relative)
        if not path.is_file() or path.stat().st_size != record['bytes'] or sha256(path) != record['sha256']:
            raise SystemExit(f"Candidate file differs from its pinned version: {relative}")
    os.environ["LAND_ANALYTICS_PROFILE"] = "use_type_v2"
    import app
    app.DIST_ROOT = ROOT / "output" / f"frontend_{SLUG}"
    app.LAND_ANALYTICS_SUMMARY = ROOT / "server_data" / f"land_analytics_summary_{SLUG}.json"
    app.LAND_ANALYTICS_INDEX = ROOT / "server_data" / f"land_analytics_{SLUG}.sqlite3"
    for path in (app.DIST_ROOT / "index.html", app.LAND_ANALYTICS_SUMMARY, app.LAND_ANALYTICS_INDEX):
        if not path.is_file():
            raise SystemExit(f"Candidate is incomplete: {path.name}")
    os.environ["WORKING_PRODUCT_HOST"] = "127.0.0.1"
    os.environ["WORKING_PRODUCT_PORT"] = "8526"
    app.main()


if __name__ == "__main__":
    main()
