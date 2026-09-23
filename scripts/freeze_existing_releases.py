"""Preserve existing compiled outputs, without calculating or rebuilding them."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_tools import sha256, verify_release


def main():
    catalog_path = ROOT / "config/releases.json"
    if catalog_path.exists():
        raise SystemExit("Releases already frozen; create a new explicitly named release instead")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    catalog = {"schema_version": 1, "releases": {}}
    profiles = {
        "working": ("v2", "dist", "land_analytics_summary.json", "land_analytics_v2_2026_09_05.sqlite3"),
        "use_type_review": ("use_type_v2", "dist_use_type_v2", "land_analytics_summary_v3_2026_09_05.json", "land_analytics_v3_2026_09_05.sqlite3"),
    }
    for name, (profile, dist, summary_name, index_name) in profiles.items():
        release = ROOT / "releases" / f"{name}_{stamp}"
        release.mkdir(parents=True, exist_ok=False)
        shutil.copy2(ROOT / "app.py", release / "app.py")
        shutil.copytree(ROOT / dist, release / "dist")
        (release / "server_data").mkdir()
        for filename in ["cadastre_search.sqlite3", summary_name, index_name]:
            shutil.copy2(ROOT / "server_data" / filename, release / "server_data" / filename)
        summary = json.loads((release / "server_data" / summary_name).read_text(encoding="utf-8"))
        manifest = {
            "schema_version": 1, "created_at": stamp, "analytics_profile": profile,
            "delivery_version": summary["delivery_version"],
            "summary": f"server_data/{summary_name}", "index": f"server_data/{index_name}",
            "tile_url": summary["tile_delivery"]["url"],
            "analytical_approval": "preserved_not_newly_approved; use_type_rejected_pending_replanning",
            "files": {p.relative_to(release).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                      for p in sorted(release.rglob("*")) if p.is_file()},
        }
        (release / "release.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        catalog["releases"][name] = {"directory": release.relative_to(ROOT).as_posix(),
                                    "manifest_sha256": sha256(release / "release.json")}
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    for name in profiles:
        print(json.dumps(verify_release(ROOT, name)))


if __name__ == "__main__":
    main()
