"""Private unused-land/elevation screening. No runtime imports from parent projects."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from affine import Affine
from rasterio.features import geometry_mask
from shapely.geometry import box
from shapely.ops import nearest_points

ROOT = Path(__file__).resolve().parents[1]
SLUG = "land_potential_20260906_v1"
OUT = ROOT / "data/analysis/land_potential" / SLUG
REVIEW = ROOT / "server_data/review" / SLUG
BASIS = ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"
INDEX = ROOT / "server_data/land_analytics_transitions_area_20260906_v1.sqlite3"
GRID = ROOT / "data/source/activity_2026/lower_hrazdan_eo_terrain_grid_2026.npz"
SEASONS = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1/seasons"
POLICY = {
    "scope": "saved_lower_hrazdan_stages_I_II",
    "years": [2021, 2022, 2023, 2024, 2025],
    "history": "preserved stable_no_activity; >=3 no-activity years, >=75% of observed, <=1 used",
    "recent_years": [2024, 2025],
    "current": "preserved ready no_current_activity, latest recorded 2026-08-23",
    "recent_eo": "both recent years covered; no positive vegetation_signal; spatial_reason empty",
    "minimum_grid_area_coverage": 0.95,
    "minimum_open_land_fraction": 0.80,
    "open_worldcover_codes": [30, 40, 60],
    "maximum_built_water_fraction": 0.10,
    "built_water_worldcover_codes": [50, 80],
    "canal_reference": "nearest point on existing assigned stage line to parcel representative point",
    "terrain_reference_radius_m": 45.0,
    "elevation_rule": "parcel minimum > canal reference window maximum: mechanical; parcel maximum < reference window minimum: gravity; otherwise review",
    "source_dem_nominal_resolution_m": 90,
    "dem_output_grid_spacing_m": 10,
    "hydraulic_feasibility": "unassessed",
    "greenhouse_and_legal_suitability": "not established; owner visual/field assessment required",
    "source_landcover_date": "cached categorical context; not a new 2026 land-cover observation",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def inputs():
    return [BASIS, INDEX, GRID, GRID.with_suffix(".json"), ROOT / "data/source/lower_hrazdan.geojson",
            ROOT / "server_data/land_analytics_summary_transitions_area_20260906_v1.json",
            *[SEASONS / f"{y}.parquet" for y in POLICY["years"]]]


def samples(geometry, transform, shape):
    """Return exact intersected pixel areas; small parcels do not vanish at centers."""
    x0, y0, x1, y1 = geometry.bounds
    a, _, c, _, e, f = tuple(transform)[:6]
    col0, col1 = max(0, int(np.floor((x0-c)/a))), min(shape[1], int(np.ceil((x1-c)/a)))
    row0, row1 = max(0, int(np.floor((y1-f)/e))), min(shape[0], int(np.ceil((y0-f)/e)))
    if col1 <= col0 or row1 <= row0:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([])
    t = transform * Affine.translation(col0, row0)
    mask = geometry_mask([geometry], (row1-row0, col1-col0), t, invert=True, all_touched=True)
    rows, cols = np.where(mask)
    rows, cols = rows + row0, cols + col0
    xs, ys = c + cols*a, f + rows*e
    cells = shapely.box(xs, ys+e, xs+a, ys)
    area = shapely.area(shapely.intersection(cells, geometry))
    keep = area > 1e-7
    return rows[keep], cols[keep], area[keep]


def terrain_class(parcel_min, parcel_max, reference_min, reference_max):
    if not np.all(np.isfinite([parcel_min, parcel_max, reference_min, reference_max])):
        return "review", "terrain_missing"
    if parcel_min > reference_max:
        return "mechanical_candidate", "above_canal_terrain"
    if parcel_max < reference_min:
        return "gravity_candidate", "below_canal_terrain"
    return "review", "terrain_elevation_overlap"


def history_pass(codes):
    if len(codes) != 5 or any(x not in (0, 1, 2, 3) for x in codes):
        return False
    observed = sum(x != 0 for x in codes)
    inactive = codes.count(3)
    used = sum(x in (1, 2) for x in codes)
    return observed >= 3 and inactive >= 3 and inactive/observed >= .75 and used <= 1 and codes[-2:] == [3, 3]


def analyze():
    if (OUT / "complete.json").exists():
        verify()
        print(json.dumps(read(OUT / "report.json")), flush=True)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    REVIEW.mkdir(parents=True, exist_ok=True)
    source_hashes = {p.relative_to(ROOT).as_posix(): sha(p) for p in inputs()}
    write(OUT / "manifest.json", {"version": SLUG, "created_utc": datetime.now(timezone.utc).isoformat(),
          "policy": POLICY, "inputs": source_hashes, "code_sha256": sha(Path(__file__)), "private": True})
    frame = gpd.read_parquet(BASIS)
    with sqlite3.connect(INDEX.as_uri()+"?mode=ro", uri=True) as con:
        old = pd.read_sql_query("SELECT cadastre_code,activity_stage,activity_state,activity_class,household,road_excluded,history_class,annual_state_codes FROM parcel_analytics", con)
    frame = frame.merge(old.drop(columns="road_excluded"), on="cadastre_code", validate="one_to_one")
    assert len(frame) == 43984 and frame.public_parcel_id.is_unique
    assert frame.stage.eq(frame.activity_stage).all()
    # Geometry-cache flags predate the accepted 160-parcel household correction.
    # Preserve geometry/code/area from the basis and active exclusions from 8526.
    frame["household_agriculture"] = frame.household.astype(bool)
    with sqlite3.connect(INDEX.as_uri()+"?mode=ro", uri=True) as con:
        roads = dict(con.execute("SELECT cadastre_code,road_excluded FROM parcel_analytics"))
    assert frame.road_excluded.eq(frame.cadastre_code.map(roads).astype(bool)).all()
    frame["eligible"] = ~(frame.household_agriculture | frame.road_excluded)
    assert int(frame.eligible.sum()) == 22802
    frame["potential_class"] = "not_candidate"
    frame["reason"] = "historical_activity_or_unresolved_history"
    frame.loc[~frame.eligible, "potential_class"] = "excluded"
    frame.loc[frame.household_agriculture, "reason"] = "household_excluded"
    frame.loc[frame.road_excluded, "reason"] = "road_excluded"
    codes = frame.annual_state_codes.map(lambda x: json.loads(x) if x else [])
    historical = frame.eligible & frame.history_class.eq("stable_no_activity")
    current = historical & frame.activity_state.eq("ready") & frame.activity_class.eq("no_current_activity")
    initial = current & codes.map(history_pass)
    frame.loc[historical & ~current, "reason"] = "current_activity_or_unresolved_current"
    frame.loc[current & ~initial, "reason"] = "recent_completed_year_activity"
    frame.loc[initial, ["potential_class", "reason"]] = ["review", "pending_eo_check"]
    frame["initial_nonuse_screen"] = initial
    eligible_ids = set(frame.loc[initial, "internal_parcel_id"])
    for y in [2024, 2025]:
        s = pd.read_parquet(SEASONS / f"{y}.parquet").set_index("internal_parcel_id")
        frame[f"covered_{y}"] = frame.internal_parcel_id.map(s.covered).fillna(False).astype(bool)
        frame[f"vegetation_signal_{y}"] = frame.internal_parcel_id.map(s.vegetation_signal).fillna(False).astype(bool)
        frame[f"spatial_reason_{y}"] = frame.internal_parcel_id.map(s.spatial_reason).fillna("missing")
        allowed = set(s.index[s.covered & ~s.vegetation_signal & s.spatial_reason.fillna("").eq("")])
        eligible_ids &= allowed
    supported = initial & frame.internal_parcel_id.isin(eligible_ids)
    frame.loc[initial & ~supported, "reason"] = "recent_eo_coverage_or_spatial_or_vegetation_conflict"
    frame["recent_eo_supported"] = supported
    terrain = np.load(GRID, allow_pickle=False)
    metadata = read(GRID.with_suffix(".json"))
    transform = Affine(*metadata["transform"])
    projected = frame.to_crs(metadata["crs"])
    canals = gpd.read_file(ROOT / "data/source/lower_hrazdan.geojson").to_crs(metadata["crs"])
    lines = {stage: shapely.union_all(g.geometry.values) for stage, g in canals.groupby("stage")}
    elevation, cover = terrain["elevation"], terrain["worldcover"]
    details = []
    for n, idx in enumerate(frame.index[supported], 1):
        row = projected.loc[idx]
        geom = row.geometry
        rr, cc, weights = samples(geom, transform, elevation.shape)
        d = {"internal_parcel_id": row.internal_parcel_id, "grid_coverage": float(weights.sum()/geom.area)}
        reason = "grid_coverage_incomplete"
        kind = "review"
        if d["grid_coverage"] >= POLICY["minimum_grid_area_coverage"]:
            wc = cover[rr, cc]
            d["open_fraction"] = float(weights[np.isin(wc, POLICY["open_worldcover_codes"])].sum()/weights.sum())
            d["built_water_fraction"] = float(weights[np.isin(wc, POLICY["built_water_worldcover_codes"])].sum()/weights.sum())
            d["urban_context_fraction"] = float(weights[terrain["urban_mask"][rr, cc] != 0].sum()/weights.sum())
            if d["open_fraction"] < .8 or d["built_water_fraction"] > .1:
                reason = "landcover_constraint_or_mixed_cover"
            else:
                ref = nearest_points(geom.representative_point(), lines[row.stage])[1]
                rad = POLICY["terrain_reference_radius_m"]
                r2, c2, w2 = samples(box(ref.x-rad, ref.y-rad, ref.x+rad, ref.y+rad), transform, elevation.shape)
                d.update(canal_reference_x=float(ref.x), canal_reference_y=float(ref.y),
                         canal_distance_m=float(ref.distance(geom.representative_point())),
                         canal_window_coverage=float(w2.sum()/(4*rad*rad)))
                if d["canal_window_coverage"] < .95:
                    reason = "canal_reference_outside_saved_dem"
                else:
                    vals, cv = elevation[rr, cc], elevation[r2, c2]
                    d.update(parcel_elevation_min=float(np.min(vals)), parcel_elevation_max=float(np.max(vals)),
                             parcel_elevation_mean=float(np.average(vals, weights=weights)),
                             canal_elevation_min=float(np.min(cv)), canal_elevation_max=float(np.max(cv)),
                             canal_elevation_mean=float(np.average(cv, weights=w2)))
                    d["relative_elevation_m"] = d["parcel_elevation_mean"]-d["canal_elevation_mean"]
                    kind, reason = terrain_class(d["parcel_elevation_min"], d["parcel_elevation_max"], d["canal_elevation_min"], d["canal_elevation_max"])
        frame.loc[idx, ["potential_class", "reason"]] = [kind, reason]
        details.append(d)
        if n % 250 == 0:
            print(json.dumps({"measured_parcels": n, "total": int(supported.sum())}), flush=True)
    frame = frame.merge(pd.DataFrame(details), on="internal_parcel_id", how="left", validate="one_to_one")
    frame["hydraulic_feasibility"] = "unassessed"
    frame["suitability_verified"] = False
    frame.to_parquet(OUT / "all_parcels.parquet", index=False)
    candidates = frame[frame.potential_class.isin(["gravity_candidate", "mechanical_candidate"])]
    candidates.to_parquet(OUT / "candidates.parquet", index=False)
    candidates[["cadastre_code", "stage", "area_official_m2", "potential_class"]].to_csv(OUT / "candidates.csv", index=False, encoding="utf-8-sig")
    frame.drop(columns="geometry").to_csv(OUT / "all_parcels_private.csv", index=False, encoding="utf-8-sig")
    summaries = {}
    for scope in ["lower_hrazdan", "stage_1", "stage_2"]:
        f = frame if scope == "lower_hrazdan" else frame[frame.stage.eq(scope)]
        counts = {k: int(f.potential_class.eq(k).sum()) for k in ["gravity_candidate", "mechanical_candidate", "review", "not_candidate", "excluded"]}
        summaries[scope] = {"eligible_parcel_count": int(f.eligible.sum()), "screened_nonuse_count": int(f.initial_nonuse_screen.sum()),
                            "candidate_count": counts["gravity_candidate"]+counts["mechanical_candidate"], "class_counts": counts,
                            "candidate_official_area_ha": float(f.loc[f.potential_class.isin(["gravity_candidate", "mechanical_candidate"]), "area_official_m2"].sum()/10000)}
    report = {"version": SLUG, "status": "private_preliminary_owner_review", "summaries": summaries,
              "reason_counts": frame[frame.eligible].reason.value_counts().to_dict(), "initial_nonuse": int(initial.sum()),
              "recent_eo_supported": int(supported.sum()), "all_parcels": len(frame), "no_new_downloads": True,
              "suitability_and_hydraulic_feasibility_verified": False}
    write(OUT / "report.json", report)
    outputs = {p.name: sha(p) for p in OUT.iterdir() if p.is_file() and p.name != "complete.json"}
    write(OUT / "complete.json", {"outputs": outputs, "finished_utc": datetime.now(timezone.utc).isoformat()})
    verify()
    print(json.dumps(report), flush=True)


def verify():
    manifest, complete = read(OUT / "manifest.json"), read(OUT / "complete.json")
    for name, digest in manifest["inputs"].items():
        assert sha(ROOT / name) == digest, name
    assert sha(Path(__file__)) == manifest["code_sha256"]
    for name, digest in complete["outputs"].items():
        assert sha(OUT / name) == digest, name
    frame, basis = gpd.read_parquet(OUT / "all_parcels.parquet"), gpd.read_parquet(BASIS)
    frame = frame.set_index("internal_parcel_id").loc[basis.internal_parcel_id].reset_index()
    assert frame.geometry.to_wkb().tolist() == basis.geometry.to_wkb().tolist()
    for k in ["cadastre_code", "area_official_m2", "road_excluded"]:
        assert frame[k].tolist() == basis[k].tolist(), k
    with sqlite3.connect(INDEX.as_uri()+"?mode=ro", uri=True) as con:
        active_households = dict(con.execute("SELECT cadastre_code,household FROM parcel_analytics"))
    assert frame.household_agriculture.eq(frame.cadastre_code.map(active_households).astype(bool)).all()
    assert len(frame) == 43984 and int(frame.eligible.sum()) == 22802
    assigned = frame.potential_class.isin(["gravity_candidate", "mechanical_candidate"])
    assert frame.loc[assigned, "eligible"].all() and frame.loc[assigned, "initial_nonuse_screen"].all()
    assert frame.loc[assigned, "recent_eo_supported"].all()
    for kind in ["gravity_candidate", "mechanical_candidate"]:
        g = frame[frame.potential_class.eq(kind)]
        if kind == "gravity_candidate":
            assert (g.parcel_elevation_max < g.canal_elevation_min).all()
        else:
            assert (g.parcel_elevation_min > g.canal_elevation_max).all()
        assert g.open_fraction.ge(.8).all() and g.built_water_fraction.le(.1).all()
    assert sum(read(OUT / "report.json")["summaries"]["lower_hrazdan"]["class_counts"].values()) == len(frame)
    write(REVIEW / "analysis_verification.json", {"status": "passed", "parcels": len(frame), "candidates": int(assigned.sum()),
          "immutable_geometry_codes_area_exclusions": True, "inputs_and_outputs_sha256_verified": True})
