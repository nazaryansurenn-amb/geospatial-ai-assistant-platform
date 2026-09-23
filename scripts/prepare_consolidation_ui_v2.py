"""Preserve v1 and package its unchanged data with the requested fifth-tab UI."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_tools import sha256
from run_observation_analysis import write_json

BASE_SLUG = "consolidation_review_20260906_v1"
SLUG = "consolidation_review_20260906_v2"
BASE = ROOT / "output" / f"frontend_{BASE_SLUG}"
FRONTEND = ROOT / "output" / f"frontend_{SLUG}"
BASE_REVIEW = ROOT / "server_data/review" / BASE_SLUG
REVIEW = ROOT / "server_data/review" / SLUG
SOURCE = REVIEW / "frontend_source"
LOCK = ROOT / "config" / f"{SLUG}.review.lock.json"


def verify_base():
    for slug in ("land_potential_1km_review_20260906_v2", BASE_SLUG):
        for name, record in json.loads((ROOT / "config" / f"{slug}.review.lock.json").read_text())["files"].items():
            assert sha256(ROOT / name) == record["sha256"], name


def prepare():
    verify_base()
    assert not REVIEW.exists() and not FRONTEND.exists(), "Preserve prior versions"
    shutil.copytree(BASE_REVIEW / "frontend_source", SOURCE)
    shutil.copytree(BASE / "data", FRONTEND / "data")
    for name in ("summary.json", "parcel_lookup.json", "tile_verification.json"):
        shutil.copy2(BASE_REVIEW / name, REVIEW / name)
    print("Prepared UI-only revision; all analytical inputs unchanged")


def finish():
    assert not LOCK.exists(), "Cannot overwrite a sealed version"
    verify_base()
    for name in ("summary.json", "parcel_lookup.json", "tile_verification.json"):
        assert sha256(BASE_REVIEW / name) == sha256(REVIEW / name)
    original = {p.relative_to(BASE).as_posix(): sha256(p) for p in (BASE / "data").rglob("*") if p.is_file()}
    copied = {p.relative_to(FRONTEND).as_posix(): sha256(p) for p in (FRONTEND / "data").rglob("*") if p.is_file()}
    assert copied == original
    delivered = [p for p in FRONTEND.rglob("*") if p.is_file()]
    assert (FRONTEND / "index.html").is_file()
    for p in delivered:
        name = p.relative_to(FRONTEND).as_posix()
        assert name in original or name == "index.html" or (name.startswith("assets/") and p.suffix in (".js", ".css")), name
    files = [ROOT / "run_consolidation_review_v2.py", ROOT / "scripts/prepare_consolidation_ui_v2.py",
             REVIEW / "summary.json", REVIEW / "parcel_lookup.json",
             *[p for p in SOURCE.rglob("*") if p.is_file()], *delivered]
    write_json(LOCK, {"version": SLUG, "base": BASE_SLUG, "port": 8526,
        "files": {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in files}})
    write_json(REVIEW / "delivery_verification.json", {"passed": True, "ui_only": True,
        "all_map_data_byte_identical": True, "summary_and_parcel_lookup_byte_identical": True,
        "previous_seals_valid": True, "data_files": len(original)})
    print("Verified unchanged data and sealed fifth-tab UI revision")


if __name__ == "__main__":
    {"prepare": prepare, "finish": finish}[sys.argv[1]]()
