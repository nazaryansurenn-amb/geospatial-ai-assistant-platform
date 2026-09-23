"""Derive single-transition changes without overwriting the strict 2025 review."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import geopandas as gpd
import pandas as pd
import mapbox_vector_tile
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
from shapely.geometry import box
from prepare_land_analytics_delivery import tile_range, tile_bounds_3857, tile_width_m
from release_tools import sha256
from run_observation_analysis import write_json, now
from wp_core.activity_change_history import YEARS, CLASSES, transition, summary

SLUG = "activity_change_history_review_20260906_v1"
BASE_SLUG = "agent_review_20260906_v5"
BASE = ROOT / "output" / f"frontend_{BASE_SLUG}"
REVIEW = ROOT / "server_data/review" / SLUG
SOURCE = REVIEW / "frontend_source"
FRONTEND = ROOT / "output" / f"frontend_{SLUG}"
OUT = ROOT / "data/analysis/activity_change" / SLUG
BASIS = ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"
INDEX = ROOT / "server_data/land_analytics_land_potential_1km_review_20260906_v1.sqlite3"
SEASONS = ROOT / "data/analysis/parcel_eo/v5_full_halo_2021_2025"
PUBLIC_DIR = f"data/activity_change/{SLUG}"
LOCK = ROOT / "config" / f"{SLUG}.review.lock.json"
BASE_LOCKS = [f"agent_review_20260906_v{i}" for i in range(1, 6)] + ["hectares_review_20260906_v1", "consolidation_review_20260906_v2"]
PROPS = {"cadastre_code", "change_class", "stage", "area_ha"}


def verify_base():
    for slug in BASE_LOCKS:
        for name, record in json.loads((ROOT / "config" / f"{slug}.review.lock.json").read_text())["files"].items():
            assert sha256(ROOT / name) == record["sha256"], name


def prepare():
    verify_base()
    assert not OUT.exists() and not REVIEW.exists() and not FRONTEND.exists(), "Do not overwrite versions"
    basis = gpd.read_parquet(BASIS)
    assert basis.crs.to_epsg() == 4326 and basis.geometry.is_valid.all() and not basis.geometry.is_empty.any()
    assert basis.cadastre_code.is_unique and basis.internal_parcel_id.is_unique and basis.public_parcel_id.is_unique
    with sqlite3.connect(INDEX.as_uri() + "?mode=ro", uri=True) as c:
        history = pd.read_sql_query("SELECT cadastre_code,activity_stage,household,road_excluded,annual_state_codes,profile_year_count FROM parcel_analytics", c)
    assert history.cadastre_code.is_unique and set(history.cadastre_code) == set(basis.cadastre_code)
    frame = basis.drop(columns=["household_agriculture", "road_excluded", "preview_state"]).merge(history, on="cadastre_code", validate="one_to_one")
    assert frame.stage.eq(frame.activity_stage).all()
    results = [transition(r.annual_state_codes, r.profile_year_count, bool(r.household), bool(r.road_excluded)) for r in frame.itertuples()]
    frame["change_class"] = [r[0] for r in results]
    frame["change_year"] = pd.array([r[1] for r in results], dtype="Int64")
    frame["codes"] = frame.annual_state_codes.map(lambda value: json.loads(value) if value else [0] * 5)
    selected = frame[frame.change_class.isin(CLASSES)].copy()
    quality = []
    season_paths = [SEASONS / f"seasonal_features_{year}.parquet" for year in YEARS]
    # Independently replay the existing seasonal state formulas for every candidate.
    for year, path in zip(YEARS, season_paths):
        raw = pd.read_parquet(path)
        assert raw.cadastre_code.is_unique and raw.analysis_year.eq(year).all()
        check = selected[["cadastre_code", "internal_parcel_id", "codes"]].merge(raw, on="cadastre_code", validate="one_to_one", suffixes=("_basis", "_eo"))
        assert len(check) == len(selected) and check.internal_parcel_id_basis.eq(check.internal_parcel_id_eo).all()
        assert check.feature_status.eq("seasonal_profile").all() and check.season_complete.eq(True).all()
        active = check.ndvi_p90.ge(.40) & check.vegetation_observation_rate.ge(.40) & check.vegetation_fraction_max.ge(.30)
        inactive = check.persistent_bare_observation_rate.ge(.60) & check.vegetation_observation_rate.le(.20)
        codes = pd.Series(2, index=check.index)
        codes.loc[active] = 1
        codes.loc[inactive] = 3
        assert codes.eq(check.codes.map(lambda values: values[year - 2021])).all(), f"Conflicting seasonal evidence in {year}"
        quality.append({"year": year, "candidates_verified": len(check), "complete_profiles": int(check.season_complete.sum()),
                        "minimum_usable_observations": int(check.usable_observation_count.min()) if len(check) else None})
    OUT.mkdir(parents=True)
    REVIEW.mkdir(parents=True)
    frame.drop(columns="codes").to_parquet(OUT / "register.parquet", index=False)
    selected.drop(columns="codes").to_parquet(OUT / "candidates.parquet", index=False)
    lookup = {r.cadastre_code: {"changeClass": r.change_class, "changeYear": None if pd.isna(r.change_year) else int(r.change_year)} for r in frame.itertuples()}
    payload = {"analysis_version": SLUG, "observation_years": list(YEARS),
               "summaries": {}, "bounds": {}, "created_at": now(), "area_basis": "whole_cadastral_parcels"}
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        subset = frame if scope == "lower_hrazdan" else frame[frame.stage.eq(scope)]
        payload["summaries"][scope] = summary(subset)
        payload["bounds"][scope] = {}
        for kind in CLASSES:
            group = subset[subset.change_class.eq(kind)]
            payload["bounds"][scope][kind] = list(group.total_bounds) if len(group) else None
    shutil.copytree(ROOT / "server_data/review" / BASE_SLUG / "frontend_source", SOURCE)
    shutil.copytree(BASE / "data", FRONTEND / "data")
    tiles_info = tiles(selected)
    payload["tile_delivery"] = {"url": f"/{PUBLIC_DIR}/{{z}}/{{x}}/{{y}}.pbf", "layer_name": "activity_change", "min_zoom": 8, "max_zoom": 15,
                                "bounds": list(selected.total_bounds) if len(selected) else list(basis.total_bounds)}
    write_json(REVIEW / "summary.json", payload)
    write_json(REVIEW / "parcel_lookup.json", lookup)
    inputs = [BASIS, INDEX, *season_paths]
    write_json(OUT / "manifest.json", {"analysis_version": SLUG, "created_at": payload["created_at"],
        "inputs": {p.relative_to(ROOT).as_posix(): sha256(p) for p in inputs},
        "source_states": "Existing seasonal history: 1 active, 2 partial, 3 no observed activity; 0 unknown",
        "rule": "All five seasons adequately observed; exactly one switch between active/partial and no observed activity; no reversal through 2025; retain first year of new state",
        "previous_preserved_version": "activity_change_2025_review_20260906_v1",
        "no_250m_inputs": True, "no_2026_activity_input": True, "current_household_and_road_exclusions": True,
        "caveat": "An observed state transition, not independently established cultivation or permanent abandonment. Partial is the preserved broad residual seasonal class."})
    write_json(OUT / "verification.json", {"passed": True, "register_parcels": len(frame), "candidates": len(selected),
        "yearly_replay": quality, "geometry_unchanged": True, "code_and_official_area_unchanged": True,
        "excluded_roads_or_households_in_candidates": 0, "tile_verification": tiles_info})
    print(json.dumps(payload["summaries"]["lower_hrazdan"], indent=2))


def tiles(frame):
    metric = frame.to_crs(3857)
    seen, total_bytes, tile_count = set(), 0, 0
    if len(metric):
        for z in range(8, 16):
            xs, ys = tile_range(tuple(metric.total_bounds), z)
            for x in xs:
                for y in ys:
                    bounds = tile_bounds_3857(z, x, y)
                    pad = tile_width_m(z) * 8 / 4096
                    clip = box(bounds[0]-pad, bounds[1]-pad, bounds[2]+pad, bounds[3]+pad)
                    features = []
                    for r in metric.iloc[metric.sindex.query(clip, predicate="intersects")].itertuples():
                        geom = r.geometry.intersection(clip).simplify(tile_width_m(z)/4096*.25, preserve_topology=True)
                        if not geom.is_empty:
                            features.append({"id": int(r.public_parcel_id), "geometry": geom,
                                "properties": {"cadastre_code": r.cadastre_code, "stage": r.stage,
                                               "change_class": r.change_class, "area_ha": r.area_official_m2 / 10000}})
                    content = mapbox_vector_tile.encode({"name": "activity_change", "features": features}, default_options={"quantize_bounds": bounds, "extents": 4096, "on_invalid_geometry": on_invalid_geometry_make_valid})
                    for f in mapbox_vector_tile.decode(content)["activity_change"]["features"]:
                        assert set(f["properties"]) == PROPS
                        seen.add(f["id"])
                    p = FRONTEND / PUBLIC_DIR / str(z) / str(x) / f"{y}.pbf"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content)
                    total_bytes += len(content)
                    tile_count += 1
    assert seen == set(frame.public_parcel_id)
    return {"tiles": tile_count, "bytes": total_bytes, "parcel_ids": len(seen)}


def finish():
    assert not LOCK.exists(), "Preserve sealed version"
    verify_base()
    manifest = json.loads((OUT / "manifest.json").read_text())
    for name, digest in manifest["inputs"].items():
        assert sha256(ROOT / name) == digest, name
    for p in (BASE / "data").rglob("*"):
        if p.is_file():
            assert sha256(p) == sha256(FRONTEND / p.relative_to(BASE)), str(p)
    basis = gpd.read_parquet(BASIS).set_index("cadastre_code").sort_index()
    frame = gpd.read_parquet(OUT / "register.parquet").set_index("cadastre_code").sort_index()
    assert len(basis) == len(frame) and basis.crs == frame.crs
    assert (basis.geometry.to_wkb() == frame.geometry.to_wkb()).all()
    assert basis.area_official_m2.equals(frame.area_official_m2)
    expected = {int(r.public_parcel_id): r for r in frame[frame.change_class.isin(CLASSES)].reset_index().itertuples()}
    seen = set()
    for p in (FRONTEND / PUBLIC_DIR).rglob("*.pbf"):
        for f in mapbox_vector_tile.decode(p.read_bytes())["activity_change"]["features"]:
            r = expected[f["id"]]
            assert f["properties"] == {"cadastre_code": r.cadastre_code, "change_class": r.change_class, "stage": r.stage, "area_ha": r.area_official_m2 / 10000}
            seen.add(f["id"])
    assert seen == set(expected)
    previous_names = {p.relative_to(BASE).as_posix() for p in (BASE / "data").rglob("*") if p.is_file()}
    for p in FRONTEND.rglob("*"):
        if p.is_file():
            name = p.relative_to(FRONTEND).as_posix()
            assert name in previous_names or name == "index.html" or (name.startswith(PUBLIC_DIR + "/") and p.suffix == ".pbf") or (name.startswith("assets/") and p.suffix in (".js", ".css")), name
    files = [ROOT / "run_activity_change_history_review.py", ROOT / "wp_core/activity_change_history.py", ROOT / "wp_core/activity_change_history_agent.py", ROOT / "scripts/prepare_activity_change_history.py",
             ROOT / "scripts/verify_activity_change_http.py", ROOT / "tests/test_activity_change_history.py",
             REVIEW / "summary.json", REVIEW / "parcel_lookup.json",
             *[p for p in OUT.rglob("*") if p.is_file()], *[p for p in SOURCE.rglob("*") if p.is_file()], *[p for p in FRONTEND.rglob("*") if p.is_file()]]
    write_json(LOCK, {"version": SLUG, "base": BASE_SLUG, "port": 8526,
        "files": {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in files}})
    write_json(REVIEW / "delivery_verification.json", {"passed": True, "parcels": len(seen), "geometry_and_area_unchanged": True, "all_prior_map_data_unchanged": True})
    print("Verified and sealed single-transition 2021-2025 changes")


if __name__ == "__main__":
    {"prepare": prepare, "finish": finish}[sys.argv[1]]()
