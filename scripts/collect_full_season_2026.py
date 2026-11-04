"""Append-only 2026 Copernicus parcel observations, built like full_seasons_2021_2025_v2.

Reuses the pinned collector unchanged (scripts/collect_use_type_full_seasons.py and
scripts/build_lower_hrazdan_parcel_eo_timeseries.py): same STAC query, cloud and
coverage filters, radiometry normalisation (wp_core.eo_radiometry.normalized_scene),
10 m grid, fractional sampling, 10-day interval scene selection and preflight.
Only the year (2026) and the output folder differ. Only the area bounding box is
sent to the public STAC/COG endpoints; cadastral codes never leave the PC.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize

import build_lower_hrazdan_parcel_eo_timeseries as eo
import collect_use_type_full_seasons as full
from wp_core.parcel_identity import attach_parcel_identity

YEAR = 2026
SOURCE = full.SOURCE
OUT = ROOT / "data/analysis/parcel_eo/full_season_2026_v1"
VERSION = "copernicus_full_season_2026_v1"
OBSERVED_THROUGH = "2026-11-01"
save_json = full.save_json


def _guard(path: Path):
    if not path.resolve().is_relative_to(OUT.resolve()):
        raise ValueError("Refusing to write outside " + str(OUT))


def plan():
    OUT.mkdir(parents=True, exist_ok=True)
    # The pinned query writes radiometry_exclusions_<year>.json into its module OUT;
    # point it at the new append-only folder (the pinned file itself is unchanged).
    full.OUT = OUT
    parcels = gpd.read_parquet(SOURCE)
    geometry_hash = eo.sha256_file(SOURCE)
    if len(parcels) != 43984 or parcels.cadastre_code.duplicated().any() or not parcels.is_valid.all():
        raise ValueError("Canonical population changed or is invalid")
    result = {"version": VERSION, "geometry_sha256": geometry_hash, "parcel_count": len(parcels),
              "grid_resolution_m": 10, "public_release_approved": False, "years": {},
              "observed_through": OBSERVED_THROUGH,
              "method_source": "scripts/collect_use_type_full_seasons.py (unchanged, imported)"}
    path = OUT / f"manifest_{YEAR}.json"
    if path.exists():
        manifest = json.loads(path.read_text())
        if manifest["geometry_sha256"] != geometry_hash:
            raise ValueError("Different geometry in cached manifest")
    else:
        start, items = full.query_year(YEAR, parcels.total_bounds)
        selected = eo._select_interval_scenes(items, season_start=start, interval_days=10)
        by_id = {item["id"]: item for item in selected}
        scenes = sorted(by_id.values(), key=lambda item: item["properties"]["datetime"])
        dates = sorted(s["properties"]["datetime"][:10] for s in items)
        manifest = {"version": VERSION, "year": YEAR, "geometry_sha256": geometry_hash,
                    "available_scenes": len(items), "scenes": scenes,
                    "observed_through": OBSERVED_THROUGH,
                    "available_date_range": [dates[0], dates[-1]] if dates else None,
                    "season_window": f"{start}/{YEAR}-12-31T23:59:59Z (partial year)",
                    "created_at": datetime.now(timezone.utc).isoformat()}
        _guard(path)
        save_json(path, manifest)
    cached = sum((OUT / "cache" / str(YEAR) / f"{scene['id']}.parquet").exists() for scene in manifest["scenes"])
    result["years"][str(YEAR)] = {"available": manifest["available_scenes"],
                                  "selected": len(manifest["scenes"]), "reusable": cached}
    print(f"plan {YEAR}: {result['years'][str(YEAR)]}", flush=True)
    save_json(OUT / "catalog.json", result)
    return result


def extract(workers=2):
    catalog = plan()
    parcels = gpd.read_parquet(SOURCE)
    metric = parcels.to_crs(eo.GRID_CRS)
    transform, width, height, _ = eo._grid_from_bounds(metric.total_bounds)
    labels = rasterize(((geom, i + 1) for i, geom in enumerate(metric.geometry)),
                       out_shape=(height, width), transform=transform, fill=0, dtype="int32")
    counts = np.bincount(labels[labels > 0], minlength=len(parcels) + 1)[1:].astype("int32")
    fractional = eo._fractional_sampling_plan(metric, transform=transform, width=width,
                                             height=height, parcel_pixel_count=counts)
    codes = parcels.cadastre_code.astype(str).tolist()
    manifest = json.loads((OUT / f"manifest_{YEAR}.json").read_text())
    jobs = [(YEAR, scene) for scene in manifest["scenes"]]
    print(f"grid {width}x{height}; {len(jobs)} scenes including reusable cache", flush=True)

    def process(job):
        year, scene = job
        path = OUT / "cache" / str(year) / f"{scene['id']}.parquet"
        _guard(path)
        if path.exists():
            table = pd.read_parquet(path)
            if (len(table) != len(parcels) or table.cadastre_code.duplicated().any()
                    or set(table.cadastre_code) != set(codes)
                    or set(table.parcel_geometry_version) != {catalog["geometry_sha256"]}):
                raise ValueError(f"Invalid cached scene {scene['id']}")
            return year, scene["id"], str(path.relative_to(ROOT)), "reused"
        started = time.monotonic()
        with rasterio.Env(GDAL_HTTP_TIMEOUT="45", GDAL_HTTP_CONNECTTIMEOUT="15"):
            table = eo._build_scene_table(scene=scene, cadastre_codes=codes, labels=labels,
                parcel_pixel_count=counts, transform=transform, width=width, height=height,
                parcel_geometry_version=catalog["geometry_sha256"], analysis_version=VERSION,
                fractional_plan=fractional)
        table = attach_parcel_identity(table)
        reliable = table.valid_fraction.ge(.6) & table.surface_water_fraction.lt(.2)
        saturation = table.loc[reliable, "ndvi_mean"].ge(.995).mean()
        if saturation > .01:
            raise ValueError(f"Radiometric preflight failed: saturated NDVI fraction {saturation:.3f}")
        table["radiometry_rule"] = scene["radiometry_rule"]
        if set(table.internal_parcel_id) != set(parcels.internal_parcel_id):
            raise ValueError("Parcel identity mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".writing.parquet")
        table.to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(path)
        return year, scene["id"], str(path.relative_to(ROOT)), f"new {time.monotonic()-started:.1f}s"

    paths, failures = {}, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process, job): job for job in jobs}
        for i, future in enumerate(as_completed(futures), 1):
            year, scene = futures[future]
            try:
                y, scene_id, path, message = future.result()
                paths.setdefault(y, []).append(path)
                print(f"{i}/{len(jobs)} {scene_id} {message}", flush=True)
            except Exception as error:
                failures.append({"year": year, "scene": scene["id"], "error": str(error)})
                print(f"FAILED {scene['id']}: {error}", flush=True)
            save_json(OUT / "progress.json", {"finished": i, "total": len(jobs), "failures": failures})
    if failures:
        catalog.update(failures=failures, complete=False)
        save_json(OUT / "catalog.json", catalog)
        raise SystemExit(f"{len(failures)} scene failures; annual outputs withheld until resume succeeds")
    for year, sources in sorted(paths.items()):
        result = pd.concat([pd.read_parquet(ROOT / p) for p in sorted(sources)], ignore_index=True)
        result["analysis_version"] = VERSION
        result["observation_date"] = pd.to_datetime(result.observation_date)
        result = result.sort_values(["cadastre_code", "observation_date", "scene_id"])
        if result.duplicated(["internal_parcel_id", "observation_date", "scene_id"]).any():
            raise ValueError("Duplicate scene observations")
        target = OUT / f"observations_{year}.parquet"
        _guard(target)
        if not target.exists():
            result.to_parquet(target, index=False, compression="zstd")
        catalog["years"][str(year)].update(rows=len(result), parcels=result.cadastre_code.nunique(),
            dates=result.observation_date.nunique(), sources=sorted(sources), sha256=eo.sha256_file(target),
            first_date=str(result.observation_date.min().date()), last_date=str(result.observation_date.max().date()))
    catalog.update(failures=failures, complete=not failures)
    save_json(OUT / "catalog.json", catalog)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 6), default=2)
    args = parser.parse_args()
    extract(args.workers) if args.extract else plan()
