"""Build a narrow data-only update on the sealed v6 product."""
from pathlib import Path
import json
import shutil
import sys
import geopandas as gpd
import mapbox_vector_tile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from release_tools import sha256
from run_observation_analysis import write_json
import prepare_activity_change_history as previous_delivery
from prepare_activity_change_area50 import SLUG, OUT, REVIEW

BASE_SLUG = "agent_review_20260906_v6"
BASE = ROOT / "output" / f"frontend_{BASE_SLUG}"
FRONTEND = ROOT / "output" / f"frontend_{SLUG}"
PUBLIC_DIR = f"data/activity_change/{SLUG}"
LOCK = ROOT / "config" / f"{SLUG}.review.lock.json"


def verify_base():
    names = [f"agent_review_20260906_v{i}" for i in range(1, 7)] + ["activity_change_history_review_20260906_v1"]
    for slug in names:
        lock = ROOT / "config" / f"{slug}.review.lock.json"
        assert lock.exists(), f"Wait for sealed base: {slug}"
        for name, record in json.loads(lock.read_text())["files"].items():
            assert sha256(ROOT / name) == record["sha256"], name
    manifest = json.loads((OUT / "manifest.json").read_text())
    for name, digest in manifest["inputs"].items():
        assert sha256(ROOT / name) == digest, name


def build():
    verify_base()
    assert not FRONTEND.exists(), "Preserve old builds"
    shutil.copytree(BASE, FRONTEND)
    frame = gpd.read_parquet(OUT / "candidates.parquet")
    previous_delivery.FRONTEND = FRONTEND
    previous_delivery.PUBLIC_DIR = PUBLIC_DIR
    tiles = previous_delivery.tiles(frame)
    payload = json.loads((REVIEW / "summary.json").read_text())
    payload["tile_delivery"] = {"url": f"/{PUBLIC_DIR}/{{z}}/{{x}}/{{y}}.pbf", "layer_name": "activity_change",
                                "min_zoom": 8, "max_zoom": 15, "bounds": frame.total_bounds.tolist()}
    write_json(REVIEW / "summary.json", payload)
    write_json(REVIEW / "build.json", {"base": BASE_SLUG, "ui_byte_identical": True, "tiles": tiles})
    print(json.dumps(tiles))


def seal():
    verify_base()
    assert not LOCK.exists(), "Do not reseal"
    for path in BASE.rglob("*"):
        if path.is_file():
            assert sha256(path) == sha256(FRONTEND / path.relative_to(BASE)), str(path)
    allowed = {p.relative_to(BASE).as_posix() for p in BASE.rglob("*") if p.is_file()}
    winners = gpd.read_parquet(OUT / "candidates.parquet")
    expected = {int(r.public_parcel_id): r for r in winners.itertuples()}
    seen = set()
    for path in FRONTEND.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(FRONTEND).as_posix()
        assert relative in allowed or (relative.startswith(PUBLIC_DIR + "/") and path.suffix == ".pbf"), relative
        if relative.startswith(PUBLIC_DIR + "/"):
            for feature in mapbox_vector_tile.decode(path.read_bytes())["activity_change"]["features"]:
                row = expected[feature["id"]]
                assert feature["properties"] == {"cadastre_code": row.cadastre_code, "change_class": row.change_class, "stage": row.stage, "area_ha": row.area_official_m2/10000}
                seen.add(feature["id"])
    assert seen == set(expected)
    names = ["run_activity_change_area50_review.py", "wp_core/activity_change_area50.py", "wp_core/activity_change_area50_agent.py",
             "scripts/prepare_activity_change_area50.py", "scripts/deliver_activity_change_area50.py", "tests/test_activity_change_area50.py",
             "scripts/verify_activity_change_area50_http.py", "docs/ACTIVITY_CHANGE_AREA50_2026_09_07_EN.md"]
    files = [ROOT / n for n in names] + [REVIEW / "summary.json", REVIEW / "parcel_lookup.json"]
    files += [p for folder in (FRONTEND, OUT) for p in folder.rglob("*") if p.is_file()]
    write_json(LOCK, {"version": SLUG, "base": BASE_SLUG, "port": 8526,
                      "files": {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in files}})
    write_json(REVIEW / "delivery_verification.json", {"passed": True, "parcel_ids": len(seen), "base_files_byte_identical": True, "public_allowlist_passed": True})
    print("Verified and sealed partial-season area gate")


if __name__ == "__main__":
    {"build": build, "seal": seal}[sys.argv[1]]()
