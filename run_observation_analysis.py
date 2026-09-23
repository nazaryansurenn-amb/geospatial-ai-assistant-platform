"""Autonomous, local-only EO feature calculation and rule baseline.

Run/verify are independent of the weather collector and all map servers.
No downloads, LLM calls, old classifications, tiles, or public files are used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from release_tools import local_path, sha256
from wp_core.observation_rules import CORE, FRACTIONS, INDICES, Policy, VERSION, predominant, seasonal_features

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def write_table(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp.parquet")
    data.to_parquet(temp, index=False, compression="zstd")
    os.replace(temp, path)


@contextmanager
def run_lock(path):
    import msvcrt
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def settings(root, relative):
    config_path = local_path(root, relative)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for key in ("version", "input_version"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", config[key]):
            raise ValueError("Invalid version slug")
    if config["years"] != [2021, 2022, 2023, 2024, 2025] or config["publication"] != "internal_draft_only":
        raise ValueError("Only completed 2021-2025 internal analysis is authorized")
    if not 1 <= config["batch_parcels"] <= 4096:
        raise ValueError("Unbounded parcel batch")
    base = local_path(root, "data/observations/" + config["input_version"])
    output = local_path(root, "data/analysis/observation_rules/" + config["version"])
    return config, Policy(**config["policy"]), base, output


def inputs(root, config, base):
    specification = json.loads((base / "specification.json").read_text(encoding="utf-8"))
    if specification["config"]["years"] != config["years"]:
        raise ValueError("EO years differ")
    basis_path = local_path(root, specification["config"]["basis"])
    if sha256(basis_path) != specification["scope"]["basis_sha256"]:
        raise ValueError("Pinned cadastral geometry changed")
    scope = pd.read_parquet(base / "scope.parquet")
    parcels = pd.read_parquet(base / "parcels.parquet").sort_values("internal_parcel_id").reset_index(drop=True)
    if len(scope) != config["expected_population"] or len(parcels) != config["expected_eligible"]:
        raise ValueError("Unexpected scope size")
    for data in (scope, parcels):
        if data.internal_parcel_id.isna().any() or not data.internal_parcel_id.is_unique or not data.cadastre_code.is_unique:
            raise ValueError("Missing/duplicate cadastral identity")
    if set(scope.loc[scope.included, "internal_parcel_id"]) != set(parcels.internal_parcel_id):
        raise ValueError("Eligible/excluded scope mismatch")
    if (parcels.household.astype(bool) | parcels.road_excluded.astype(bool)).any():
        raise ValueError("Household/road included in open-field analysis")
    geometry = gpd.read_parquet(basis_path).set_index("internal_parcel_id")
    if geometry.crs is None or not geometry.index.is_unique:
        raise ValueError("Invalid cadastral CRS or identity")
    selected = geometry.loc[parcels.internal_parcel_id]
    if not selected.is_valid.all() or selected.is_empty.any():
        raise ValueError("Invalid cadastral geometry")
    if not np.array_equal(selected.cadastre_code.to_numpy(), parcels.cadastre_code.to_numpy()):
        raise ValueError("Changed cadastral codes")
    if not np.array_equal(selected.area_official_m2.to_numpy(), parcels.area_official_m2.to_numpy()):
        raise ValueError("Changed official areas")
    centers = selected.to_crs(32638).centroid
    parcels["spatial_group"] = [f"{int(x//2000)}_{int(y//2000)}" for x, y in zip(centers.x, centers.y)]
    parcels["area_group"] = pd.cut(parcels.area_official_m2 / 10000, [0, .1, .5, 2, np.inf],
                                    labels=["small", "medium", "large", "very_large"], include_lowest=True).astype(str)
    db = local_path(root, f"server_data/collector/{config['input_version']}/jobs.sqlite3")
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        jobs = [dict(r) for r in connection.execute(
            "SELECT id,state,output,sha256,rows,payload FROM jobs WHERE kind=? ORDER BY id", ("eo",))]
    if not jobs or any(j["state"] != "complete" for j in jobs):
        raise ValueError("EO collection is not complete; weather is deliberately not a dependency")
    records = []
    for j in jobs:
        scene = json.loads(j.pop("payload"))
        records.append({**j, "date": scene["properties"]["datetime"][:10], "scene_id": scene["id"]})
    if any(int(j["date"][:4]) not in config["years"] for j in records):
        raise ValueError("Out-of-period observation")
    pins = {p.relative_to(root).as_posix(): sha256(p) for p in
            (base / "specification.json", base / "schema.json", base / "parcels.parquet", base / "scope.parquet", basis_path)}
    return parcels, scope, records, specification, pins


def load_year(root, jobs, parcels, geometry_hash, input_version):
    dates = sorted({j["date"] for j in jobs})
    if not dates:
        raise ValueError("Year has no scenes")
    year = int(dates[0][:4])
    days = pd.to_datetime(dates).dayofyear.to_numpy()
    shape = (len(parcels), len(dates))
    values = {k: np.full(shape, np.nan, dtype=np.float32) for k in (*INDICES, *FRACTIONS)}
    supports = {k: np.zeros(shape, dtype=np.float32) for k in INDICES}
    pixels = np.zeros(shape, dtype=np.int32)
    scores = np.full(shape, -1., dtype=np.float32)
    selection = np.full(shape, -1, dtype=np.int16)
    identity = parcels.set_index("internal_parcel_id")
    fields = ["internal_parcel_id", "cadastre_code", "observation_date", "scene_id", "geometry_version", "data_version",
              "ndvi_count", *FRACTIONS, *[f"{k}_{suffix}" for k in INDICES for suffix in ("mean", "valid_fraction")]]
    ordered = sorted(jobs, key=lambda j: (j["date"], j["scene_id"]))
    for number, j in enumerate(ordered):
        path = local_path(root, j["output"])
        if not path.is_relative_to(root / "data/observations" / input_version / "eo") or sha256(path) != j["sha256"]:
            raise ValueError("EO path/checksum mismatch")
        frame = pq.ParquetFile(path).read(columns=fields).to_pandas()
        positions = identity.index.get_indexer(frame.internal_parcel_id)
        if len(frame) != len(parcels) or len(frame) != j["rows"] or not frame.internal_parcel_id.is_unique or (positions < 0).any():
            raise ValueError("Missing or duplicate parcel-scene row")
        if not np.array_equal(frame.cadastre_code.to_numpy(), identity.cadastre_code.iloc[positions].to_numpy()):
            raise ValueError("EO cadastral code mismatch")
        for column, expected in (("geometry_version", geometry_hash), ("data_version", input_version),
                                 ("scene_id", j["scene_id"]), ("observation_date", j["date"])):
            if not frame[column].eq(expected).all():
                raise ValueError("EO version/date mismatch: " + column)
        for k in INDICES:
            f = frame[k + "_valid_fraction"].to_numpy()
            if not np.isfinite(f).all() or (f < 0).any() or (f > 1.000001).any():
                raise ValueError("Invalid spatial support")
            if np.isinf(frame[k + "_mean"].to_numpy()).any():
                raise ValueError("Infinite EO index")
        valid = np.minimum.reduce([frame[k + "_valid_fraction"].to_numpy() for k in CORE])
        valid = np.where(np.isfinite(frame[[k + "_mean" for k in CORE]].to_numpy()).all(axis=1), valid, 0)
        d = dates.index(j["date"])
        better = valid > scores[positions, d]
        target = positions[better]
        # Keep a coherent source row; duplicate dates never become extra votes.
        for k in (*INDICES, *FRACTIONS):
            values[k][target, d] = frame[k + "_mean" if k in INDICES else k].to_numpy()[better]
        for k in INDICES:
            supports[k][target, d] = frame[k + "_valid_fraction"].to_numpy()[better]
        pixels[target, d] = frame.ndvi_count.to_numpy()[better]
        scores[target, d] = valid[better]
        selection[target, d] = number
    if (selection < 0).any():
        raise ValueError("Unaccounted parcel/date")
    provenance = {"year": year, "scenes": len(jobs), "distinct_dates": len(dates),
                  "same_date_rule": "greatest minimum core-index support, scene_id tie-break; coherent row",
                  "selection_sha256": hashlib.sha256(selection.tobytes()).hexdigest(),
                  "selected_rows_per_scene": {j["scene_id"]: int((selection == n).sum()) for n, j in enumerate(ordered)}}
    return days, values, supports, pixels, provenance


def review_sample(parcels):
    data = parcels.copy()
    data["review_group"] = np.select([
        data.crop_type_candidate.eq("annual") & data.annual_cycle_candidate.eq("single_cycle"),
        data.crop_type_candidate.eq("annual") & data.annual_cycle_candidate.eq("two_cycles"),
        data.crop_type_candidate.eq("perennial")], ["single", "double", "perennial"], default="boundary")
    data["order"] = data.internal_parcel_id.map(lambda s: hashlib.sha256(s.encode()).hexdigest())
    samples, shortfalls = [], {}
    for group in ("single", "double", "perennial", "boundary"):
        candidates = data.loc[data.review_group.eq(group)].sort_values("order").copy()
        strata = ["activity_stage", "area_group", "spatial_group"]
        candidates["round"] = candidates.groupby(strata).cumcount()
        candidates = candidates.sort_values(["round", "order"])
        # Alternate stages before exhausting a stage's strata.
        stage_groups = {s: candidates.loc[candidates.activity_stage.eq(s)] for s in ("stage_1", "stage_2")}
        chosen = []
        for i in range(30):
            for s in stage_groups:
                if i < len(stage_groups[s]) and len(chosen) < 30:
                    chosen.append(stage_groups[s].iloc[i])
        if chosen:
            samples.append(pd.DataFrame(chosen))
        shortfalls[group] = 30 - len(chosen)
    result = pd.concat(samples, ignore_index=True).drop(columns=["order", "round"])
    result["verified_label"] = ""
    result["verification_status"] = "pending_dated_imagery_and_owner_review"
    result["training_eligible"] = False
    return result, shortfalls


def verify_output(output, expected_ids):
    progress = json.loads((output / "progress.json").read_text())
    for relative, digest in progress["files"].items():
        if sha256(local_path(output, relative)) != digest:
            raise ValueError("Changed analytical output: " + relative)
    frames = [pd.read_parquet(output / "seasons" / f"{y}.parquet") for y in range(2021, 2026)]
    for year, frame in zip(range(2021, 2026), frames):
        if set(frame.internal_parcel_id) != expected_ids or not frame.internal_parcel_id.is_unique or not frame.year.eq(year).all():
            raise ValueError("Season output coverage failure")
        if ((~frame.covered) & frame.type_code.ne(0)).any() or (frame.cycle_code.gt(0) & frame.type_code.ne(1)).any():
            raise ValueError("Missing evidence was classified")
    summary = pd.read_parquet(output / "parcel_results.parquet")
    if set(summary.internal_parcel_id) != expected_ids or not summary.internal_parcel_id.is_unique or not summary.cadastre_code.is_unique:
        raise ValueError("Parcel result coverage failure")
    if summary.accepted.any() or summary.household.any() or summary.road_excluded.any():
        raise ValueError("Unexpected publication or excluded parcel")
    features = [pq.ParquetFile(output / "features" / f"{y}.parquet") for y in range(2021, 2026)]
    if any(f.metadata.num_rows != len(expected_ids) or any(k + "_median" not in f.schema_arrow.names for k in INDICES) for f in features):
        raise ValueError("Feature schema/count mismatch")
    sample = pd.read_parquet(output / "review_sample.parquet")
    if sample.training_eligible.any() or not sample.internal_parcel_id.is_unique:
        raise ValueError("Unverified training labels")
    return {"technical_checks_passed": True, "parcel_count": len(summary), "parcel_year_rows": sum(map(len, frames)),
            "years": [2021, 2022, 2023, 2024, 2025], "indices": len(INDICES), "review_sample": len(sample),
            "classification_accuracy_measured": False, "visual_review_complete": False, "map_changed": False}


def execute(root, config_relative, command):
    config, policy, base, output = settings(root, config_relative)
    parcels, scope, jobs, spec, pins = inputs(root, config, base)
    source_files = [Path(__file__), root / "wp_core/observation_rules.py", root / "release_tools.py"]
    manifest = {"algorithm": VERSION, "config": config, "policy": asdict(policy), "input_pins": pins,
                "eo_scenes": jobs, "code_sha256": {p.relative_to(root).as_posix(): sha256(p) for p in source_files},
                "weather_required": False, "legacy_250m_used": False, "old_crop_labels_used": False,
                "approval": "draft_not_accepted", "classification_accuracy": "not_measured"}
    if command == "verify":
        if json.loads((output / "manifest.json").read_text()) != manifest:
            raise ValueError("Pinned analysis inputs/code changed")
        result = verify_output(output, set(parcels.internal_parcel_id))
        print(json.dumps(result, indent=2), flush=True)
        return result
    output.mkdir(parents=True, exist_ok=True)
    with run_lock(output / "run.lock"):
        if (output / "manifest.json").exists():
            if json.loads((output / "manifest.json").read_text()) != manifest:
                raise ValueError("Use a new version: pinned inputs/code differ")
        else:
            write_json(output / "manifest.json", manifest)
        if (output / "report.json").exists():
            return verify_output(output, set(parcels.internal_parcel_id))
        progress_path = output / "progress.json"
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"started": now(), "files": {}, "years": []}
        for relative, digest in progress["files"].items():
            if sha256(local_path(output, relative)) != digest:
                raise ValueError("Resume output changed")

        def record(path):
            progress["files"][path.relative_to(output).as_posix()] = sha256(path)

        for year in config["years"]:
            if year in progress["years"]:
                continue
            if shutil.disk_usage(root).free < config["minimum_free_gb"] * 1024**3:
                raise RuntimeError("Insufficient disk space")
            started = time.monotonic()
            print(f"{year}: validating source scenes and building coherent daily observations", flush=True)
            year_jobs = [j for j in jobs if j["date"].startswith(str(year))]
            days, values, supports, pixels, provenance = load_year(root, year_jobs, parcels, spec["scope"]["basis_sha256"], config["input_version"])
            chunks = []
            for start in range(0, len(parcels), config["batch_parcels"]):
                stop = start + config["batch_parcels"]
                features = seasonal_features(days, {k: v[start:stop] for k, v in values.items()},
                                            {k: v[start:stop] for k, v in supports.items()}, pixels[start:stop], year, policy)
                features.insert(0, "internal_parcel_id", parcels.internal_parcel_id.iloc[start:stop].to_numpy())
                features.insert(1, "cadastre_code", parcels.cadastre_code.iloc[start:stop].to_numpy())
                features.insert(2, "year", year)
                chunks.append(features)
            features = pd.concat(chunks, ignore_index=True)
            diagnostic_columns = [c for c in features if not any(c.startswith(k + "_") for k in (*INDICES, *FRACTIONS))]
            season = features[diagnostic_columns].copy()
            for folder, data in (("features", features), ("seasons", season)):
                path = output / folder / f"{year}.parquet"
                write_table(path, data)
                record(path)
            path = output / "date_selection" / f"{year}.json"
            write_json(path, provenance)
            record(path)
            progress["years"].append(year)
            progress["updated"] = now()
            write_json(progress_path, progress)
            print(f"{year}: {len(features)} parcels; {int(season.covered.sum())} assessable; {season.type_code.value_counts().to_dict()}; {time.monotonic()-started:.1f}s", flush=True)
            del values, supports, pixels, chunks, features, season
            gc.collect()
        seasons = [pd.read_parquet(output / "seasons" / f"{y}.parquet") for y in config["years"]]
        arrays = [np.column_stack([s[c].to_numpy() for s in seasons]) for c in ("type_code", "cycle_code", "covered", "vegetation_signal")]
        result = pd.concat([parcels, predominant(*arrays, policy)], axis=1)
        for i, year in enumerate(config["years"]):
            result[f"type_{year}"] = arrays[0][:, i]
            result[f"cycle_{year}"] = arrays[1][:, i]
        sample, shortfalls = review_sample(result)
        for name, data in (("parcel_results", result), ("scope", scope), ("review_sample", sample)):
            path = output / f"{name}.parquet"
            write_table(path, data)
            record(path)
        sample.to_csv(output / "review_sample.csv", index=False, encoding="utf-8-sig")
        record(output / "review_sample.csv")
        write_json(progress_path, progress)
        checked = verify_output(output, set(parcels.internal_parcel_id))
        summaries = {}
        for name, subset in (("whole_zone", result), *result.groupby("activity_stage", sort=True)):
            summaries[name] = {"parcels": len(subset), "types": subset.crop_type_candidate.value_counts().to_dict(),
                               "cycles": subset.annual_cycle_candidate.value_counts().to_dict(),
                               "history_signals": subset.history_signal.value_counts().to_dict(),
                               "assessable_years": {str(k): v for k, v in subset.assessable_years.value_counts().sort_index().to_dict().items()}}
        report = {"analysis_version": config["version"], "started": progress["started"], "finished": now(),
                  "status": "completed_internal_draft", "source_scene_count": len(jobs),
                  "source_row_count": sum(j["rows"] for j in jobs), "summaries": summaries,
                  "season_coverage": {str(y): {"assessable": int(s.covered.sum()), "quality_reasons": s.quality_reason.value_counts().to_dict(),
                                      "type_reasons": s.type_reason.value_counts().to_dict()} for y, s in zip(config["years"], seasons)},
                  "verification": checked, "sample_shortfalls": shortfalls,
                  "pending": ["Dated-image visual review and independent labels", "Local calibration and holdout evaluation",
                              "Weather-based phenology context", "Additional built/water/greenhouse checks before land-potential selection"],
                  "limitations": ["Uncalibrated rules, not field-validated classifications", "No specific crop identification",
                                  "Persistent greenness is not proof of agricultural management or irrigation",
                                  "Annual-cycle screening cannot independently prove a second sowing or distinguish all interrow/cutting patterns",
                                  "Calendar windows are broad screening assumptions, not year-specific weather-calibrated growing seasons",
                                  "All 18 indices have features; only documented core signals enter the baseline rules",
                                  "No 2026 activity, weather attribution, land-potential acceptance, ML training, or map release performed"]}
        write_json(output / "report.json", report)
        print(json.dumps({"output": output.relative_to(root).as_posix(), "verification": checked,
                          "whole_zone": summaries["whole_zone"]}, indent=2), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "verify"))
    parser.add_argument("--config", default="config/observation_analysis.json")
    args = parser.parse_args()
    execute(ROOT, args.config, args.command)
