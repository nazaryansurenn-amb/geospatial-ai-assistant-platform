"""Append-only full-year Copernicus parcel observations for owner review."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
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
from shapely.geometry import box, shape
import requests

import build_lower_hrazdan_parcel_eo_timeseries as eo
from wp_core.parcel_identity import attach_parcel_identity
from wp_core.eo_radiometry import normalized_scene

SOURCE = ROOT / "data/analysis/parcel_eo/current_halo_v1/current_parcels.parquet"
OLD = ROOT / "data/analysis/parcel_eo/v5_full_halo_2021_2025"
OUT = ROOT / "data/analysis/parcel_eo/full_seasons_2021_2025_v2"
VERSION = "copernicus_full_seasons_2021_2025_v2"


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".writing.json")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def query_year(year, bounds):
    start = f"{year}-01-01T00:00:00Z" if year >= 2021 else "2020-10-01T00:00:00Z"
    collection = "sentinel-2-l2a" if year == 2022 else "sentinel-2-c1-l2a"
    body = {"collections": [collection], "bbox": list(bounds),
            "datetime": f"{start}/{year}-12-31T23:59:59Z", "limit": 100}
    session = requests.Session()
    url, method, payload = eo.EARTH_SEARCH_URL, "POST", body
    items, pages, exclusions = {}, set(), []
    while url:
        page_key = (url, json.dumps(payload, sort_keys=True))
        if page_key in pages:
            raise ValueError("STAC pagination loop")
        pages.add(page_key)
        for attempt in range(3):
            try:
                response = session.request(method, url, json=payload if method == "POST" else None, timeout=45)
                response.raise_for_status()
                page = response.json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        for item in page.get("features", []):
            if not all(asset in item.get("assets", {}) for asset in eo.REQUIRED_ASSETS):
                continue
            if not shape(item["geometry"]).covers(box(*bounds)):
                continue
            if float(item["properties"].get("eo:cloud_cover", 100)) <= 80:
                try:
                    items[item["id"]] = normalized_scene(item)
                except ValueError as error:
                    exclusions.append({"scene_id": item['id'], "reason": str(error)})
                    print(f"exclude {item['id']}: {error}", flush=True)
        link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if link:
            url, method = link["href"], link.get("method", "GET")
            payload = ({**body, **link.get("body", {})} if link.get("merge") else link.get("body"))
        else:
            url = None
    save_json(OUT / f"radiometry_exclusions_{year}.json", exclusions)
    return start, list(items.values())


def plan():
    parcels = gpd.read_parquet(SOURCE)
    geometry_hash = eo.sha256_file(SOURCE)
    if len(parcels) != 43984 or parcels.cadastre_code.duplicated().any() or not parcels.is_valid.all():
        raise ValueError("Canonical population changed or is invalid")
    result = {"version": VERSION, "geometry_sha256": geometry_hash, "parcel_count": len(parcels),
              "grid_resolution_m": 10, "public_release_approved": False, "years": {}}
    for year in range(2020, 2026):
        path = OUT / f"manifest_{year}.json"
        if path.exists():
            manifest = json.loads(path.read_text())
            if manifest["geometry_sha256"] != geometry_hash:
                raise ValueError("Different geometry in cached manifest")
        else:
            start, items = query_year(year, parcels.total_bounds)
            selected = eo._select_interval_scenes(items, season_start=start, interval_days=10)
            # Existing caches used a different container fingerprint. Keep them
            # intact; this branch re-extracts against the current immutable basis.
            by_id = {item["id"]: item for item in selected}
            scenes = sorted(by_id.values(), key=lambda item: item["properties"]["datetime"])
            manifest = {"version": VERSION, "year": year, "geometry_sha256": geometry_hash,
                        "available_scenes": len(items), "scenes": scenes,
                        "created_at": datetime.now(timezone.utc).isoformat()}
            save_json(path, manifest)
        cached = sum((OUT / "cache" / str(year) / f"{scene['id']}.parquet").exists()
                     for scene in manifest["scenes"])
        result["years"][str(year)] = {"available": manifest["available_scenes"],
                                      "selected": len(manifest["scenes"]), "reusable": cached}
        print(f"plan {year}: {result['years'][str(year)]}", flush=True)
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
    jobs = []
    for year in range(2020, 2026):
        manifest = json.loads((OUT / f"manifest_{year}.json").read_text())
        for scene in manifest["scenes"]:
            jobs.append((year, scene))
    print(f"grid {width}x{height}; {len(jobs)} scenes including reusable cache", flush=True)

    def process(job):
        year, scene = job
        path = OUT / "cache" / str(year) / f"{scene['id']}.parquet"
        source = path if path.exists() else None
        if source:
            table = pd.read_parquet(source)
            if (len(table) != len(parcels) or table.cadastre_code.duplicated().any()
                    or set(table.cadastre_code) != set(codes)
                    or set(table.parcel_geometry_version) != {catalog["geometry_sha256"]}):
                raise ValueError(f"Invalid cached scene {scene['id']}")
            return year, scene["id"], str(source.relative_to(ROOT)), "reused"
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
        if not target.exists():
            result.to_parquet(target, index=False, compression="zstd")
        catalog["years"][str(year)].update(rows=len(result), parcels=result.cadastre_code.nunique(),
            dates=result.observation_date.nunique(), sources=sorted(sources), sha256=eo.sha256_file(target))
    catalog.update(failures=failures, complete=not failures)
    save_json(OUT / "catalog.json", catalog)
    if failures:
        raise SystemExit(f"{len(failures)} scene failures; no complete-data claim")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 6), default=2)
    args = parser.parse_args()
    extract(args.workers) if args.extract else plan()
