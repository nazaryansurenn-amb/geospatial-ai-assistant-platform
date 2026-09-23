"""Versioned candidate build using full local Copernicus observations only."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import geopandas as gpd
import numpy as np
import pandas as pd
from wp_core.predominant_use import classify_season, aggregate_years, POLICY, VERSION
from release_tools import read_release, verify_release, sha256

from wp_core.use_type_release import CANDIDATE_SLUG as SLUG
os.environ["LAND_ANALYTICS_DELIVERY_SLUG"] = SLUG
import prepare_land_analytics_delivery as delivery

EO_ROOT = ROOT / "data/analysis/parcel_eo/full_seasons_2021_2025_v2"
PARCELS = ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"
OUTPUT = ROOT / "data/analysis/land_use" / SLUG
METRICS = ["ndvi_mean", "ndmi_mean", "bsi_mean", "vegetation_fraction", "bare_fraction",
           "surface_water_fraction", "valid_fraction", "spatial_support_pixel_count"]


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_basis():
    verify_release(ROOT, "working")
    directory, manifest = read_release(ROOT, "working")
    parcels = gpd.read_parquet(PARCELS)
    with sqlite3.connect((directory / manifest["index"]).as_uri() + "?mode=ro", uri=True) as con:
        baseline = pd.read_sql_query("SELECT * FROM parcel_analytics", con)
    if set(parcels.cadastre_code) != set(baseline.cadastre_code):
        raise ValueError("Working release and canonical population differ")
    # Old crop classifications are deliberately not analytical inputs.
    baseline = baseline.drop(columns=[c for c in baseline if c.startswith("crop_") or c.startswith("annual_cycle")])
    columns = ["internal_parcel_id", "public_parcel_id", "cadastre_code", "area_official_m2", "geometry"]
    frame = parcels[columns].merge(baseline, on="cadastre_code", how="left", validate="one_to_one")
    frame = frame.rename(columns={"activity_stage": "stage", "activity_state": "preview_state",
                                  "household": "household_agriculture"})
    frame["household_agriculture"] = frame.household_agriculture.astype(bool)
    frame["road_excluded"] = frame.road_excluded.astype(bool)
    if frame.household_agriculture.sum() != 20096 or frame.road_excluded.sum() != 1086:
        raise ValueError("Preserved working masks changed")
    source = gpd.read_file(ROOT / "data/source/cadastre/parcels_wua.gpkg",
                           columns=["cadastre_code", "area_official_m2"])
    original = source.set_index("cadastre_code").loc[frame.cadastre_code]
    if original.crs != frame.crs or not np.array_equal(original.geometry.to_wkb().values, frame.geometry.to_wkb().values):
        raise ValueError("Canonical geometry differs from immutable source")
    if not np.array_equal(original.area_official_m2.values, frame.area_official_m2.values):
        raise ValueError("Official areas changed")
    if frame.cadastre_code.duplicated().any() or frame.internal_parcel_id.duplicated().any() or not frame.is_valid.all():
        raise ValueError("Invalid cadastral population")
    return frame, json.loads((directory / manifest["summary"]).read_text()), directory, manifest


def year_matrices(year, codes, geometry_hash, all_codes):
    fields = ["cadastre_code", "internal_parcel_id", "parcel_geometry_version", "observation_date", "scene_id", *METRICS]
    current = pd.read_parquet(EO_ROOT / f"observations_{year}.parquet", columns=fields)
    previous = pd.read_parquet(EO_ROOT / f"observations_{year-1}.parquet", columns=fields)
    previous = previous.loc[pd.to_datetime(previous.observation_date).dt.month >= 10]
    data = pd.concat([previous, current], ignore_index=True)
    if set(data.parcel_geometry_version) != {geometry_hash} or set(current.cadastre_code) != set(all_codes):
        raise ValueError("EO geometry/population mismatch")
    data["day"] = (pd.to_datetime(data.observation_date) - pd.Timestamp(f"{year}-01-01")).dt.days + 1
    # Same-date overlapping scenes, if present, choose greatest valid support;
    # never concatenate them as an extra temporal observation.
    data = data.sort_values(["valid_fraction", "scene_id"], ascending=[False, True]).drop_duplicates(["cadastre_code", "day"])
    days = np.sort(data.day.unique())
    matrices = [data.pivot(index="cadastre_code", columns="day", values=m).reindex(index=codes, columns=days).to_numpy(dtype="float32") for m in METRICS]
    return days, matrices


def scoped_summary(frame, cycle=False):
    result = {}
    classes = ["single_cycle", "two_cycle_recurring"] if cycle else ["annual", "perennial", "undetermined"]
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        selected = frame if scope == "lower_hrazdan" else frame.loc[frame.stage.eq(scope)]
        fields = selected.loc[~selected.household_agriculture & ~selected.road_excluded]
        series = fields.annual_cycle if cycle else fields.crop_type_candidate
        result[scope] = {"eligible_parcel_count": int(len(fields)),
                         "class_counts": {name: int(series.eq(name).sum()) for name in classes},
                         "household_count": int(selected.household_agriculture.sum()),
                         "road_excluded_count": int(selected.road_excluded.sum())}
    return result


def build():
    if OUTPUT.exists() or delivery.TILE_OUTPUT.exists() or delivery.INDEX_PATH.exists():
        raise FileExistsError("Candidate already exists; use a new version instead of overwriting")
    catalog = json.loads((EO_ROOT / "catalog.json").read_text())
    if not catalog.get("complete") or catalog.get("failures"):
        raise ValueError("Full-season acquisition not complete")
    frame, summary, baseline_dir, baseline_manifest = load_basis()
    codes = frame.cadastre_code.tolist()
    excluded = (frame.household_agriculture | frame.road_excluded).values
    field_codes = frame.loc[~excluded, "cadastre_code"].tolist()
    types, cycles, covered, annual_records = [], [], [], []
    for year in range(2021, 2026):
        days, arrays = year_matrices(year, field_codes, sha256(PARCELS), codes)
        field_result = classify_season(days, *arrays)
        result = {}
        for key, values in field_result.items():
            result[key] = np.zeros(len(frame), dtype=values.dtype)
            result[key][~excluded] = values
        result["type_code"][frame.household_agriculture.values] = -1
        result["type_code"][frame.road_excluded.values] = -2
        types.append(result["type_code"])
        cycles.append(result["cycle_code"])
        covered.append(result["covered"])
        annual = pd.DataFrame({"cadastre_code": codes, "year": year, **result})
        annual_records.append(annual)
        print(f"{year}: covered {result['covered'].sum()}; annual {(result['type_code']==1).sum()}; perennial {(result['type_code']==2).sum()}", flush=True)
    types, cycles, covered = [np.stack(items, axis=1) for items in (types, cycles, covered)]
    labels, cycle_labels, count, cycle_count = aggregate_years(types, cycles, covered)
    labels[excluded] = ""
    cycle_labels[excluded] = ""
    frame["crop_type_candidate"] = labels
    frame["annual_cycle"] = cycle_labels
    frame["crop_profile_year_count"] = count
    frame["assessed_year_count"] = cycle_count
    frame["crop_type_year_codes"] = types.tolist()
    frame["annual_cycle_year_codes"] = cycles.tolist()
    frame["history_class"] = frame.history_class.fillna("")
    frame["annual_state_codes"] = frame.annual_state_codes.map(lambda x: json.loads(x) if x else [])
    for column in ["profile_year_count", "used_year_count", "source_year_count"]:
        frame[column] = frame[column].fillna(0).astype(int)
    OUTPUT.mkdir(parents=True, exist_ok=False)
    fields = frame.loc[~frame.household_agriculture & ~frame.road_excluded]
    if fields.crop_type_candidate.eq("").any() or len(frame) != 43984:
        raise ValueError("Unaccounted parcels")
    frame.drop(columns="geometry").to_parquet(OUTPUT / "classification.parquet", index=False)
    pd.concat(annual_records, ignore_index=True).to_parquet(OUTPUT / "annual_diagnostics.parquet", index=False)
    metrics = delivery.build_tiles(frame.to_crs(3857), VERSION)
    delivery.build_index(frame)
    summary.update(delivery_version=delivery.DELIVERY_VERSION, generated_at_utc=datetime.now(timezone.utc).isoformat(),
        tile_delivery={"url": f"/data/land_analytics/{SLUG}/{{z}}/{{x}}/{{y}}.pbf", "layer_name": "land_analytics",
                       "min_zoom": 10, "max_zoom": 15, "bounds": metrics["bounds_wgs84"]},
        land_use_type={"analysis_version": VERSION, "years": list(range(2021, 2026)),
                       "status": "draft_owner_review", "summaries": scoped_summary(frame)},
        annual_cycles={"analysis_version": VERSION, "years": list(range(2021, 2026)), "summaries": scoped_summary(frame, cycle=True)})
    write_json(delivery.SUMMARY_PATH, summary)
    report = {"analysis_version": VERSION, "status": "not_visually_approved", "parcel_count": len(frame),
        "open_field_count": len(fields), "household_count": int(frame.household_agriculture.sum()),
        "road_excluded_count": int(frame.road_excluded.sum()), "unaccounted_count": 0,
        "class_counts": fields.crop_type_candidate.value_counts().to_dict(),
        "cycle_counts": fields.annual_cycle.value_counts().to_dict(),
        "assessable_season_counts": fields.crop_profile_year_count.value_counts().sort_index().to_dict(),
        "source": {"catalog": str((EO_ROOT/'catalog.json').relative_to(ROOT)), "sha256": sha256(EO_ROOT/'catalog.json')},
        "geometry_sha256": sha256(PARCELS), "immutable_source_wkb_area_match": True,
        "policy": asdict(POLICY), "legacy_250m_used": False, "current_2026_activity_used_for_classification": False,
        "working_release": baseline_manifest["delivery_version"], "tile_metrics": metrics,
        "limitations": ["Rule-based screening is not field-validated accuracy.",
            "Optical parcel means cannot conclusively distinguish woody canopy from interrow grass.",
            "Herbaceous regrowth and crop rotation can be ambiguous; no specific crops are inferred.",
            "Cloud gaps remain unresolved, not interpolated.",
            "Weather-based regional season calibration is not implemented; a broad observation window is used."]}
    write_json(OUTPUT / "report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k in ['class_counts','cycle_counts','assessable_season_counts']}, indent=2), flush=True)
    print(f"candidate outputs: {OUTPUT}", flush=True)


if __name__ == "__main__":
    build()
