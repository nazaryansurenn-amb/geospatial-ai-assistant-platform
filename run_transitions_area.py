"""Checkpointed offline Lower Hrazdan application of the unchanged Transitions v3."""
from __future__ import annotations
import os
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = "1"
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
import json
import multiprocessing
from pathlib import Path
import shutil
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from release_tools import local_path, sha256
from run_observation_analysis import run_lock, write_json, write_table, now
from run_observation_screening import supplement, CORE, BASIS, environment
from wp_core.observation_screening import Policy, predominant
from wp_core.phenology_transitions import TransitionPolicy, classify_year
from wp_core.classification_selection import select_parcel_categories

ROOT = Path(__file__).resolve().parent
CONFIG = "config/transitions_area_20260906_v1.json"
OUT = ROOT / "data/analysis/observation_screening/transitions_area_20260906_v1"
DAILY_COLUMNS = ["internal_parcel_id", "cadastre_code", "observation_date", "scene_id", "geometry_version",
                 *[f"{name}_median" for name in CORE], *[f"{name}_valid_fraction" for name in CORE],
                 "vegetation_fraction", "bare_fraction", "valid_mask_10m", "valid_mask_20m", "vegetated_mask_10m",
                 "ndvi_p10", "ndvi_p90", "ndvi_mean", "ndvi_inner5_mean", "ndvi_inner5_valid_fraction"]
_CONTEXT = None


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(hashes):
    for relative, expected in hashes.items():
        if sha256(local_path(ROOT, relative)) != expected:
            raise ValueError("Pinned file changed: " + relative)


def progress(state, **values):
    write_json(OUT / "progress.json", {"state": state, "updated_at": now(), **values})
    print(json.dumps({"state": state, **values}), flush=True)


def grouped_indices(parcel, count):
    order = np.argsort(parcel, kind="stable")
    return np.split(order, np.cumsum(np.bincount(parcel, minlength=count))[:-1])


def prepare_spatial(config, sample):
    marker = OUT / "spatial_complete.json"
    if marker.exists():
        check(read(marker)["sha256"])
        return pd.read_parquet(OUT / "spatial.parquet")
    source = ROOT / config["input"]
    geo = gpd.read_parquet(ROOT / BASIS).set_index("internal_parcel_id").loc[sample.internal_parcel_id]
    if not np.array_equal(geo.cadastre_code, sample.cadastre_code) or not np.array_equal(geo.area_official_m2, sample.area_official_m2):
        raise ValueError("Cadastral code or official area differs")
    geo = geo.to_crs(32638)
    static = sample[["internal_parcel_id", "cadastre_code", "activity_stage", "area_official_m2"]].copy()
    static["geometry_area_m2"] = geo.area.to_numpy()
    def width(g):
        xy = np.array(g.minimum_rotated_rectangle.exterior.coords)
        return float(np.linalg.norm(np.diff(xy, axis=0), axis=1).min())
    static["minimum_width_m"] = geo.geometry.map(width).to_numpy()
    static["area_pixel_equivalents_10m"] = static.geometry_area_m2 / 100
    files = []
    for res in (10, 20):
        source_path = source / f"sampling/exact_area_{res}m.npz"
        if sha256(source_path) != read(source_path.with_suffix(".json"))["sha256"]:
            raise ValueError("Sampling checksum mismatch")
        with np.load(source_path, allow_pickle=False) as z:
            original = dict(z)
        p, pixels, area = original["parcel"], original["pixel"], original["area"]
        if int(original["count"]) != len(sample):
            raise ValueError("Spatial weight population mismatch")
        total = np.bincount(p, weights=area, minlength=len(sample))
        if not np.allclose(total, geo.area, rtol=1e-7, atol=.01):
            raise ValueError("Sampling no longer covers immutable geometry")
        groups = grouped_indices(p, len(sample))
        t = original["transform"]
        rows, cols = np.divmod(pixels, int(original["width"]))
        x, y = t[2] + cols * res, t[5] - rows * res
        cells = shapely.box(x, y - res, x + res, y)
        arrays = {"parcel": p, "pixel": np.arange(len(p)), "area": area, "count": np.array(len(sample))}
        for distance in (5, 10):
            areas = np.zeros(len(area))
            for i, indices in enumerate(groups):
                inner = geo.geometry.iloc[i].buffer(-distance)
                if not inner.is_empty:
                    areas[indices] = shapely.area(shapely.intersection(cells[indices], inner))
            arrays[f"inner{distance}"] = areas
            static[f"inner{distance}_area_fraction_{res}m"] = np.bincount(p, weights=areas, minlength=len(sample)) / total
        static[f"pure_pixels_{res}m"] = np.bincount(p[area >= res * res * .999], minlength=len(sample))
        path = OUT / f"weights_{res}m.npz"
        np.savez_compressed(path, **arrays)
        files.append(path)
        progress("spatial_preparation", completed_grid_m=res, parcels=len(sample))
    # Reuse the existing neutral spatial control measurements exactly.
    control = pd.read_parquet(ROOT / config["control_measurements"] / "spatial.parquet").set_index("internal_parcel_id")
    positions = pd.Index(static.internal_parcel_id).get_indexer(control.index)
    for name in static.columns:
        if name != "internal_parcel_id" and name in control:
            static.loc[positions, name] = control[name].to_numpy()
    write_table(OUT / "spatial.parquet", static)
    files.append(OUT / "spatial.parquet")
    write_json(marker, {"sha256": {p.relative_to(ROOT).as_posix(): sha256(p) for p in files}})
    return static


def load_grids(config, sample):
    grids = {}
    for res in (10, 20):
        with np.load(ROOT / config["input"] / f"sampling/exact_area_{res}m.npz", allow_pickle=False) as z:
            original = dict(z)
        with np.load(OUT / f"weights_{res}m.npz", allow_pickle=False) as z:
            saved = dict(z)
        weights = {k: saved[k] for k in ("parcel", "pixel", "area", "count")}
        grids[res] = {"grid": original, "gather": original["pixel"], "weights": weights,
                      "groups": grouped_indices(weights["parcel"], len(sample)),
                      "interiors": {d: {**weights, "area": saved[f"inner{d}"]} for d in (5, 10)}}
    return grids


def init_worker(config, extraction):
    global _CONTEXT
    sample = pd.read_parquet(OUT / "selection.parquet")
    grids = load_grids(config, sample)
    static = pd.read_parquet(OUT / "spatial.parquet").set_index("internal_parcel_id")
    manifest = read(ROOT / config["baseline"] / "manifest.json")
    _CONTEXT = (config, sample, grids, static, Policy(**manifest["policy"]), TransitionPolicy(**manifest["transition_policy"]))


def scene_job(item):
    config, sample, grids, _, _, _ = _CONTEXT
    job, scene, bounds, geometry_hash = item
    path = OUT / "scenes" / (job["scene_id"] + ".parquet")
    side = path.with_suffix(".json")
    if side.exists():
        checkpoint = read(side)
        if sha256(path) != checkpoint["sha256"]:
            raise ValueError("Scene checkpoint changed")
        check(checkpoint["raster_sha256"])
        return {"scene": job["scene_id"], "cached": True}
    started = time.perf_counter()
    frame, pins = supplement(ROOT, scene, job, bounds, grids, sample, geometry_hash)
    # Cached controls are measurements, not previous class labels.
    cached = ROOT / config["control_measurements"] / "scenes" / (job["scene_id"] + ".parquet")
    control = pd.read_parquet(cached)
    if not set(control.columns).issubset(frame.columns):
        raise ValueError("Control measurement schema differs")
    positions = pd.Index(frame.internal_parcel_id).get_indexer(control.internal_parcel_id)
    if (positions < 0).any():
        raise ValueError("Control outside full area")
    frame.loc[positions, control.columns] = control.to_numpy()
    if len(frame) != config["expected_eligible"] or not frame.internal_parcel_id.is_unique:
        raise ValueError("Missing supplemental parcel")
    write_table(path, frame)
    write_json(side, {"sha256": sha256(path), "raster_sha256": pins, "rows": len(frame),
                      "elapsed_seconds": round(time.perf_counter() - started, 3)})
    return {"scene": job["scene_id"], "cached": False}


def classify_batch(batch):
    _, sample, grids, static, policy, transition_policy = _CONTEXT
    positions = pd.Index(sample.internal_parcel_id)
    rows, events = [], []
    for identifier, frame in batch:
        position = int(positions.get_loc(identifier))
        weights = [grids[r]["weights"]["area"][grids[r]["groups"][position]] for r in (10, 20)]
        result, found = classify_year(frame, static.loc[identifier].to_dict(), *weights, policy, transition_policy)
        rows.append({"internal_parcel_id": identifier, **result})
        events.extend({"internal_parcel_id": identifier, "year": result["year"], **event} for event in found)
    return rows, events


def year_calculation(year, jobs, pool, config):
    marker = OUT / "years" / f"{year}.json"
    if marker.exists():
        check(read(marker)["sha256"])
        return
    path = OUT / "daily" / f"{year}.parquet"
    if path.exists() and path.with_suffix(".json").exists():
        if sha256(path) != read(path.with_suffix(".json"))["sha256"]:
            raise ValueError("Daily checkpoint changed")
        daily = pd.read_parquet(path)
    else:
        blocks = [pd.read_parquet(OUT / "scenes" / (j["scene_id"] + ".parquet"), columns=DAILY_COLUMNS)
                  for j in jobs if int(j["date"][:4]) == year]
        raw = pd.concat(blocks, ignore_index=True)
        del blocks
        raw["support"] = raw[[k + "_valid_fraction" for k in CORE]].min(axis=1, skipna=False)
        raw["finite"] = np.isfinite(raw[[k + "_median" for k in CORE]]).all(axis=1)
        daily = raw.sort_values(["internal_parcel_id", "observation_date", "finite", "support", "scene_id"],
                                ascending=[True, True, False, False, True]).drop_duplicates(["internal_parcel_id", "observation_date"]).copy()
        del raw
        daily["year"] = year
        write_table(path, daily)
        write_json(path.with_suffix(".json"), {"sha256": sha256(path), "rows": len(daily)})
    progress("classifying_year", year=year, daily_rows=len(daily))
    # A bounded submission queue prevents all parcel DataFrames being pickled at once.
    grouped = iter(daily.groupby("internal_parcel_id", sort=True))
    pending, rows, events, done = set(), [], [], 0
    exhausted = False
    while pending or not exhausted:
        while len(pending) < config["maximum_workers"] * 2 and not exhausted:
            batch = []
            for _ in range(80):
                try:
                    identifier, part = next(grouped)
                    batch.append((identifier, part.reset_index(drop=True)))
                except StopIteration:
                    exhausted = True
                    break
            if batch:
                pending.add(pool.submit(classify_batch, batch))
        if pending:
            future = next(as_completed(pending))
            pending.remove(future)
            seasons, found = future.result()
            rows.extend(seasons)
            events.extend(found)
            done += len(seasons)
            if done % 2000 == 0 or done == config["expected_eligible"]:
                progress("classifying_year", year=year, completed_parcels=done, total=config["expected_eligible"])
    seasons = pd.DataFrame(rows).sort_values("internal_parcel_id").reset_index(drop=True)
    if len(seasons) != config["expected_eligible"] or not seasons.internal_parcel_id.is_unique:
        raise ValueError("Incomplete parcel season")
    files = [OUT / "seasons" / f"{year}.parquet", OUT / "events" / f"{year}.parquet"]
    write_table(files[0], seasons)
    write_table(files[1], pd.DataFrame(events))
    # Exact classification replay of all 120 cached controls in every year.
    old = pd.read_parquet(ROOT / config["baseline"] / "seasons.parquet")
    old = old.loc[old.year.eq(year)].set_index("internal_parcel_id").sort_index()
    new = seasons.set_index("internal_parcel_id").loc[old.index]
    fields = ["crop_type_candidate", "annual_cycle_candidate", "reason", "covered", "usable_dates", "maximum_gap_days"]
    pd.testing.assert_frame_equal(old[fields], new[fields], check_dtype=False)
    write_json(marker, {"sha256": {p.relative_to(ROOT).as_posix(): sha256(p) for p in files},
                        "control_replay_parcels": len(old)})
    progress("year_complete", year=year, parcels=len(seasons), control_replay=len(old))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    config = read(ROOT / CONFIG)
    if not config["owner_authorized_area_application"] or config["years"] != list(range(2021, 2026)) or config["expected_eligible"] != 22802:
        raise ValueError("Unexpected authorization or scope")
    workers = min(args.workers, config["maximum_workers"], max(1, (os.cpu_count() or 1) - 2))
    if workers < 1:
        raise ValueError("Worker count must be positive")
    source = ROOT / config["input"]
    spec = read(source / "specification.json")
    if spec["scope"]["basis_sha256"] != sha256(ROOT / BASIS):
        raise ValueError("Cadastral geometry changed")
    base = read(ROOT / config["baseline"] / "manifest.json")
    control = read(ROOT / config["control_measurements"] / "manifest.json")
    protected = {**base["input_sha256"], **base["code_sha256"], **control["input_sha256"], **control["code_sha256"]}
    for prefix in (config["baseline"], config["control_measurements"],
                   "data/analysis/observation_screening/transitions_20260906_v3_owner_v1"):
        complete = read(ROOT / prefix / "complete.json")
        protected[prefix + "/complete.json"] = sha256(ROOT / prefix / "complete.json")
        protected.update({prefix + "/" + p: h for p, h in complete["outputs"].items()})
    protected["wp_core/classification_selection.py"] = sha256(ROOT / "wp_core/classification_selection.py")
    check(protected)
    pins = {p: sha256(ROOT / p) for p in (CONFIG, "run_transitions_area.py")}
    manifest = {"config": config, "policy": base["policy"], "transition_policy": base["transition_policy"],
                "protected_sha256": protected, "code_sha256": pins, "environment": environment(),
                "cached_control_measurements_reused": True, "previous_labels_as_inference_inputs": False}
    OUT.mkdir(parents=True, exist_ok=True)
    with run_lock(OUT / "run.lock"):
        if (OUT / "manifest.json").exists() and read(OUT / "manifest.json") != manifest:
            raise ValueError("Area calculation pins changed; preserve this version")
        if (OUT / "complete.json").exists():
            check({OUT.relative_to(ROOT).as_posix() + "/" + p: h for p, h in read(OUT / "complete.json")["outputs"].items()})
            print("Completed area calculation verified; no rewrite")
            return
        write_json(OUT / "manifest.json", manifest)
        sample = pd.read_parquet(source / "parcels.parquet").sort_values("internal_parcel_id").reset_index(drop=True)
        scope = pd.read_parquet(source / "scope.parquet")
        if len(sample) != 22802 or len(scope) != 43984 or not sample.internal_parcel_id.is_unique:
            raise ValueError("Area population mismatch")
        write_table(OUT / "selection.parquet", sample)
        write_table(OUT / "scope.parquet", scope)
        static = prepare_spatial(config, sample)
        baseline = read(ROOT / "data/analysis/observation_rules/observation_rules_20260906_v1/manifest.json")
        jobs = baseline["eo_scenes"]
        scenes = {s["id"]: s for s in read(source / "scene_manifest.json")["scenes"]}
        if len(jobs) != 762 or set(j["scene_id"] for j in jobs) != set(scenes):
            raise ValueError("Scene catalog incomplete")
        started = time.perf_counter()
        progress("extracting_cached_scenes", total=762, workers=workers)
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=init_worker, initargs=(config, True)) as pool:
            futures = [pool.submit(scene_job, (job, scenes[job["scene_id"]], spec["scope"]["bounds_wgs84"], spec["scope"]["basis_sha256"])) for job in jobs]
            for done, future in enumerate(as_completed(futures), 1):
                future.result()
                if done % 10 == 0 or done == 762:
                    progress("extracting_cached_scenes", completed=done, total=762, elapsed_seconds=round(time.perf_counter() - started, 1))
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=init_worker, initargs=(config, False)) as pool:
            for year in config["years"]:
                year_calculation(year, jobs, pool, config)
        seasons = pd.concat([pd.read_parquet(OUT / "seasons" / f"{y}.parquet") for y in config["years"]], ignore_index=True)
        identity = sample.set_index("internal_parcel_id")
        parcel_rows = []
        for identifier, part in seasons.groupby("internal_parcel_id", sort=True):
            record = identity.loc[identifier]
            parcel_rows.append({"internal_parcel_id": identifier, "cadastre_code": record.cadastre_code,
                                "area_official_m2": record.area_official_m2, **predominant(part, Policy(**base["policy"]).minimum_years)})
        raw_parcels = pd.DataFrame(parcel_rows)
        selected = select_parcel_categories(raw_parcels)
        write_table(OUT / "parcels_v3.parquet", raw_parcels)
        write_table(OUT / "parcels.parquet", selected)
        report = {"status": "area_calculation_complete_for_local_review", "eligible": len(selected), "population": len(scope),
                  "seasons": len(seasons), "types": selected.crop_type_candidate.value_counts().to_dict(),
                  "cycles": selected.loc[selected.crop_type_candidate.eq("annual"), "annual_cycle_candidate"].value_counts().to_dict(),
                  "owner_cycle_assignments": int(selected.owner_cycle_assignment_applied.sum()), "control_seasons_replayed": 600,
                  "raster_downloads": 0, "llm_calculations": 0, "accuracy_measured": False, "working_release_changed": False}
        write_json(OUT / "report.json", report)
        check(protected)
        check(pins)
        for relative in [*pins, "wp_core/phenology_transitions.py", "wp_core/observation_screening.py", "wp_core/classification_selection.py"]:
            destination = OUT / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        hashes = {p.relative_to(OUT).as_posix(): sha256(p) for p in OUT.rglob("*")
                  if p.is_file() and p.name not in ("run.lock", "progress.json", "complete.json")}
        write_json(OUT / "complete.json", {"outputs": hashes})
        progress("area_calculation_complete", **report)


if __name__ == "__main__":
    main()
