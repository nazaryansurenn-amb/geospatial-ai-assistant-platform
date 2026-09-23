"""Serve the separately sealed Transitions v3 area review on localhost:8526."""
import json
import os
from pathlib import Path
from release_tools import local_path, sha256

ROOT = Path(__file__).resolve().parent
SLUG = "transitions_area_20260906_v1"


def main():
    lock = json.loads((ROOT / "config" / f"{SLUG}.review.lock.json").read_text())
    for relative, record in lock["files"].items():
        path = local_path(ROOT, relative)
        if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise ValueError("Review file changed: " + relative)
    os.environ["LAND_ANALYTICS_PROFILE"] = "use_type_v2"
    os.environ["WORKING_PRODUCT_HOST"] = "127.0.0.1"
    os.environ["WORKING_PRODUCT_PORT"] = "8526"
    import app
    app.DIST_ROOT = ROOT / "output" / f"frontend_{SLUG}"
    app.LAND_ANALYTICS_INDEX = ROOT / "server_data" / f"land_analytics_{SLUG}.sqlite3"
    app.LAND_ANALYTICS_SUMMARY = ROOT / "server_data" / f"land_analytics_summary_{SLUG}.json"
    app.main()


if __name__ == "__main__":
    main()
