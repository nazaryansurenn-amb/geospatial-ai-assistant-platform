"""Append-only per-year daily/events builder that reuses the transitions_area_20260906_v1 code path.

The pinned area calculation (run_transitions_area.py) built daily/<year>.parquet and
events/<year>.parquet from native-band COG windows cached under
data/cache/copernicus/<bounds-hash>/ (wp_core.data_collector.eo.cache_band), measured with
run_observation_screening.supplement on the exact-area 10/20 m weights, de-duplicated per
parcel/date and classified with wp_core.phenology_transitions.classify_year through
run_transitions_area.classify_batch. This script imports those functions unchanged and only
changes the year and the output folder. Nothing in the existing area folder is written.

Modes:
  --repro 2025 --out <dir>   rebuild a year from the existing local raster cache (no network)
  --year 2026 --out <dir> --raster-root <dir> [--download --budget-gb N]
"""
from __future__ import annotations

import os
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = "1"
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import multiprocessing
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd

import run_transitions_area as rta
import run_observation_screening as ros
from release_tools import sha256
from run_observation_analysis import write_json, write_table, now
from wp_core.data_collector.eo import cache_band

AREA = rta.OUT  # existing area folder: read-only source of selection, weights and spatial tables
CONFIG = rta.read(ROOT / rta.CONFIG)
SPEC = rta.read(ROOT / CONFIG["input"] / "specification.json")
SUPPLEMENT_BANDS = ("red", "nir", "scl", "blue", "nir08", "rededge1", "swir16")
_RUN = None


def init(raster_root, overlay):
    global _RUN
    rta.init_worker(CONFIG, True)
    _RUN = (Path(raster_root), overlay)


def scene_task(job, scene, scenes_dir):
    config, sample, grids, _, _, _ = rta._CONTEXT
    raster_root, overlay = _RUN
    path = Path(scenes_dir) / (job["scene_id"] + ".parquet")
    if path.exists() and path.with_suffix(".json").exists():
        if sha256(path) != rta.read(path.with_suffix(".json"))["sha256"]:
            raise ValueError("Scene checkpoint changed")
        return job["scene_id"], "cached"
    started = time.perf_counter()
    frame, pins = ros.supplement(raster_root, scene, job, SPEC["scope"]["bounds_wgs84"], grids, sample,
                                 SPEC["scope"]["basis_sha256"])
    if overlay:  # identical to run_transitions_area.scene_job (only possible where control scenes exist)
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
    write_json(path.with_suffix(".json"), {"sha256": sha256(path), "raster_sha256": pins, "rows": len(frame),
                                           "elapsed_seconds": round(time.perf_counter() - started, 3)})
    return job["scene_id"], "new"


def build_daily(year, jobs, scenes_dir, out):
    """Same de-duplication as run_transitions_area.year_calculation."""
    CORE = ros.CORE
    blocks = [pd.read_parquet(Path(scenes_dir) / (j["scene_id"] + ".parquet"), columns=rta.DAILY_COLUMNS)
              for j in jobs if int(j["date"][:4]) == year]
    raw = pd.concat(blocks, ignore_index=True)
    raw["support"] = raw[[k + "_valid_fraction" for k in CORE]].min(axis=1, skipna=False)
    raw["finite"] = np.isfinite(raw[[k + "_median" for k in CORE]]).all(axis=1)
    daily = raw.sort_values(["internal_parcel_id", "observation_date", "finite", "support", "scene_id"],
                            ascending=[True, True, False, False, True]).drop_duplicates(["internal_parcel_id", "observation_date"]).copy()
    daily["year"] = year
    path = out / "daily" / f"{year}.parquet"
    write_table(path, daily)
    write_json(path.with_suffix(".json"), {"sha256": sha256(path), "rows": len(daily)})
    return daily


def classify(daily, pool):
    grouped = iter(daily.groupby("internal_parcel_id", sort=True))
    pending, rows, events, exhausted = set(), [], [], False
    while pending or not exhausted:
        while len(pending) < CONFIG["maximum_workers"] * 2 and not exhausted:
            batch = []
            for _ in range(80):
                try:
                    identifier, part = next(grouped)
                    batch.append((identifier, part.reset_index(drop=True)))
                except StopIteration:
                    exhausted = True
                    break
            if batch:
                pending.add(pool.submit(rta.classify_batch, batch))
        if pending:
            future = next(as_completed(pending))
            pending.remove(future)
            seasons, found = future.result()
            rows.extend(seasons)
            events.extend(found)
    seasons = pd.DataFrame(rows).sort_values("internal_parcel_id").reset_index(drop=True)
    if len(seasons) != CONFIG["expected_eligible"] or not seasons.internal_parcel_id.is_unique:
        raise ValueError("Incomplete parcel season")
    return seasons, pd.DataFrame(events)


def received_bytes():
    """Machine-wide received bytes (Windows adapter counters); an upper bound on this step's transfer."""
    import subprocess
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-NetAdapterStatistics | Measure-Object -Property ReceivedBytes -Sum).Sum"],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    return int(out or 0)


def download(scenes, raster_root, budget_gb):
    store = SimpleNamespace(root=Path(raster_root).resolve(), config={"minimum_free_gb": 40, "maximum_cache_gb": 150})
    store.root.mkdir(parents=True, exist_ok=True)
    stored, start = 0, received_bytes()
    for i, scene in enumerate(scenes, 1):
        for name in SUPPLEMENT_BANDS:
            path = cache_band(store, scene, name, SPEC["scope"]["bounds_wgs84"])
            stored += path.stat().st_size
        received = received_bytes() - start
        projected = received / i * len(scenes)
        print(json.dumps({"state": "downloading", "scene": i, "total": len(scenes), "stored_gb": round(stored / 1e9, 3),
                          "received_gb": round(received / 1e9, 3), "projected_gb": round(projected / 1e9, 3)}), flush=True)
        if received > budget_gb * 1e9 or (i >= 5 and projected > budget_gb * 1e9):
            raise SystemExit(f"Transfer budget {budget_gb} GB would be exceeded (received {received/1e9:.2f} GB "
                             f"after {i} scenes, projected {projected/1e9:.2f} GB)")
    return stored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repro", type=int)
    parser.add_argument("--year", type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument("--raster-root")
    parser.add_argument("--scene-manifest")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--budget-gb", type=float, default=4.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--observed-through", default="2026-11-01")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if out.is_relative_to(AREA.resolve()) or (out.exists() and any(out.iterdir()) and not (out / "scenes").exists()):
        raise ValueError("Output must be a new append-only folder")
    workers = min(args.workers, CONFIG["maximum_workers"])
    if args.repro:
        year, raster_root, overlay = args.repro, ROOT, True
        jobs = [j for j in rta.read(ROOT / "data/analysis/observation_rules/observation_rules_20260906_v1/manifest.json")["eo_scenes"]
                if j["date"].startswith(str(year))]
        scenes = {s["id"]: s for s in rta.read(ROOT / CONFIG["input"] / "scene_manifest.json")["scenes"]}
    else:
        year, raster_root, overlay = args.year, Path(args.raster_root).resolve(), False
        if raster_root == ROOT.resolve() or raster_root.is_relative_to((ROOT / "data/cache/copernicus").resolve()):
            raise ValueError("2026 rasters must go to a new cache root, never the existing cache")
        manifest = rta.read(Path(args.scene_manifest))
        scenes = {s["id"]: s for s in manifest["scenes"] if s["properties"]["datetime"].startswith(str(year))}
        jobs = [{"scene_id": s["id"], "date": pd.Timestamp(s["properties"]["datetime"]).tz_convert("UTC").strftime("%Y-%m-%d")}
                for s in sorted(scenes.values(), key=lambda s: s["properties"]["datetime"])]
        if args.download:
            download([scenes[j["scene_id"]] for j in jobs], raster_root, args.budget_gb)
    out.mkdir(parents=True, exist_ok=True)
    scenes_dir = out / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    started = time.perf_counter()
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=init, initargs=(str(raster_root), overlay)) as pool:
        futures = [pool.submit(scene_task, j, scenes[j["scene_id"]], str(scenes_dir)) for j in jobs]
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            if done % 20 == 0 or done == len(futures):
                print(json.dumps({"state": "scenes", "done": done, "total": len(futures),
                                  "elapsed_s": round(time.perf_counter() - started, 1)}), flush=True)
    daily = build_daily(year, jobs, scenes_dir, out)
    print(json.dumps({"state": "daily", "rows": len(daily)}), flush=True)
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=init, initargs=(str(raster_root), overlay)) as pool:
        seasons, events = classify(daily, pool)
    write_table(out / "seasons" / f"{year}.parquet", seasons)
    write_table(out / "events" / f"{year}.parquet", events)
    write_json(out / "build.json", {"year": year, "scenes": len(jobs), "daily_rows": len(daily), "events": len(events),
                                    "seasons": len(seasons), "control_overlay": overlay, "finished_at": now(),
                                    "elapsed_seconds": round(time.perf_counter() - started, 1)})
    if not args.repro:
        dates = sorted(daily.observation_date.unique())
        write_json(out / "scene_manifest.json", manifest)
        write_json(out / "manifest.json", {
            "version": out.name, "year": year, "observed_through": args.observed_through,
            "note": "Partial year: observed through " + args.observed_through + "; events near the end are right-censored (cut short).",
            "method": "same code path as transitions_area_20260906_v1 (run_observation_screening.supplement, "
                      "run_transitions_area daily de-duplication and classify_batch); verified by exact 2025 rebuild",
            "scene_discovery": "wp_core.data_collector.eo.discover (all dates, no scene cloud cutoff, one product per acquisition)",
            "scenes": len(jobs), "dates": len(dates), "first_date": dates[0], "last_date": dates[-1],
            "raster_cache_root": raster_root.relative_to(ROOT.resolve()).as_posix() if raster_root.is_relative_to(ROOT.resolve()) else str(raster_root),
            "bands_cached": list(SUPPLEMENT_BANDS), "control_overlay": False,
            "control_overlay_note": "2025 check showed the 120-parcel control overlay is value-identical to fresh measurement",
            "daily_rows": len(daily), "events": len(events), "seasons": len(seasons),
            "policy": rta.read(ROOT / CONFIG["baseline"] / "manifest.json")["policy"],
            "transition_policy": rta.read(ROOT / CONFIG["baseline"] / "manifest.json")["transition_policy"],
            "code_sha256": {p: sha256(ROOT / p) for p in ("scripts/build_transitions_area_2026.py", "run_transitions_area.py",
                            "run_observation_screening.py", "wp_core/phenology_transitions.py",
                            "wp_core/observation_screening.py", "wp_core/data_collector/eo.py")},
            "outputs_sha256": {p: sha256(out / p) for p in (f"daily/{year}.parquet", f"events/{year}.parquet", f"seasons/{year}.parquet")},
            "public_release_approved": False, "created_at": now()})
    print(json.dumps({"state": "complete", "events": len(events), "seasons": len(seasons)}), flush=True)


if __name__ == "__main__":
    main()
