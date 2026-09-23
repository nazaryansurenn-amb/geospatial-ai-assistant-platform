from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.enums import Resampling
from rasterio.features import rasterize
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_core.http_client import build_retry_session
from wp_core.parcel_eo import (
    MAX_VALID_SURFACE_REFLECTANCE,
    MIN_VALID_SURFACE_REFLECTANCE,
    PARCEL_EO_SCHEMA_VERSION,
    add_gap_safe_temporal_features,
    aggregate_optical_scene_by_parcel,
    validate_parcel_eo_timeseries,
)
from wp_core.parcel_identity import attach_parcel_identity
from wp_core.eo_access import (
    EARTH_SEARCH_COLLECTION,
    EARTH_SEARCH_URL,
    GRID_CRS,
    _asset_scale_offset,
    _grid_from_bounds,
    _read_asset_to_grid,
    sha256_file,
)


PARCEL_SOURCE = (
    ROOT
    / "data" / "analysis" / "parcel_eo" / "current_halo_v1" / "current_parcels.parquet"
)
OUTPUT_DIR = ROOT / "data" / "analysis" / "parcel_eo" / "standalone_drafts"
CACHE_DIR = OUTPUT_DIR / "cache"
DEFAULT_ANALYSIS_YEAR = 2026
SCENE_INTERVAL_DAYS = 10
MAX_SCENE_CLOUD_PERCENT = 50.0
REQUIRED_ASSETS = ("red", "green", "blue", "nir", "swir16", "scl")
LEGACY_EARTH_SEARCH_COLLECTION = "sentinel-2-l2a"
SCENE_SELECTION_VERSION = "coverage_first_v1"
PIXEL_AREA_M2 = 100.0


def _analysis_version(year: int, dataset_id: str | None = None) -> str:
    suffix = f"_{dataset_id}" if dataset_id else ""
    return f"lower_hrazdan_parcel_eo_{SCENE_SELECTION_VERSION}{suffix}_{year}"


def _collection_for_year(year: int) -> str:
    return LEGACY_EARTH_SEARCH_COLLECTION if year <= 2022 else EARTH_SEARCH_COLLECTION


def _season_bounds(year: int) -> tuple[str, str]:
    return f"{year}-04-01T00:00:00Z", f"{year}-09-30T23:59:59Z"


def _output_path(year: int, output_dir: Path = OUTPUT_DIR) -> Path:
    return output_dir / f"lower_hrazdan_parcel_eo_observations_{year}.parquet"


def _manifest_path(year: int, output_dir: Path = OUTPUT_DIR) -> Path:
    return output_dir / f"lower_hrazdan_parcel_eo_scene_manifest_{year}.json"


def _quality_path(year: int, output_dir: Path = OUTPUT_DIR) -> Path:
    return output_dir / f"lower_hrazdan_parcel_eo_quality_{year}.json"


def _query_items(
    bbox_wgs84: Iterable[float],
    *,
    start: str,
    end: str,
    max_cloud_percent: float,
    collection: str = EARTH_SEARCH_COLLECTION,
) -> list[dict[str, Any]]:
    payload = {
        "collections": [collection],
        "bbox": [float(value) for value in bbox_wgs84],
        "datetime": f"{start}/{end}",
        "query": {"eo:cloud_cover": {"lt": float(max_cloud_percent)}},
        "limit": 500,
    }
    response = build_retry_session().post(EARTH_SEARCH_URL, json=payload, timeout=90)
    response.raise_for_status()
    items = list(response.json().get("features", []))
    usable = [item for item in items if all(key in item.get("assets", {}) for key in REQUIRED_ASSETS)]
    if not usable:
        raise RuntimeError("No Sentinel-2 scenes with all required optical assets were found")
    return usable


def _scene_manifest_record(scene: dict[str, Any]) -> dict[str, Any]:
    properties = scene.get("properties", {})
    return {
        "scene_id": str(scene["id"]),
        "datetime": properties.get("datetime"),
        "mgrs_tile": properties.get("grid:code"),
        "cloud_cover_percent": properties.get("eo:cloud_cover"),
        "nodata_pixel_percent": properties.get("s2:nodata_pixel_percentage"),
        "assets": {
            name: {
                "href": scene["assets"][name]["href"],
                "scale": _asset_scale_offset(scene["assets"][name])[0]
                if name != "scl"
                else 1.0,
                "offset": _asset_scale_offset(scene["assets"][name])[1]
                if name != "scl"
                else 0.0,
            }
            for name in REQUIRED_ASSETS
        },
    }


def _write_manifest(
    *,
    year: int,
    season_start: str,
    season_end: str,
    available_count: int,
    scenes: list[dict[str, Any]],
    parcel_geometry_version: str,
    collection: str,
    output_dir: Path = OUTPUT_DIR,
    analysis_version: str | None = None,
    selection_source: str = "fresh_stac_query",
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "analysis_version": analysis_version or _analysis_version(year),
        "source_program": "Copernicus Sentinel-2 L2A",
        "access_provider": "AWS Earth Search / Element 84",
        "collection": collection,
        "analysis_year": year,
        "season": [season_start, season_end],
        "selection_interval_days": SCENE_INTERVAL_DAYS,
        "scene_selection_version": SCENE_SELECTION_VERSION,
        "scene_selection_priority": [
            "minimum_scene_nodata_pixel_percentage",
            "minimum_scene_cloud_cover_percentage",
            "stable_scene_id_tiebreak",
        ],
        "maximum_scene_cloud_percent": MAX_SCENE_CLOUD_PERCENT,
        "available_scene_count": int(available_count),
        "selected_scene_count": len(scenes),
        "parcel_geometry_version": parcel_geometry_version,
        "selection_source": selection_source,
        "selected_scenes": [_scene_manifest_record(scene) for scene in scenes],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "public_release_approved": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _manifest_path(year, output_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _manifest_scenes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = []
    for record in payload.get("selected_scenes", []):
        scenes.append(
            {
                "id": record["scene_id"],
                "properties": {
                    "datetime": record["datetime"],
                    "grid:code": record.get("mgrs_tile"),
                    "eo:cloud_cover": record.get("cloud_cover_percent"),
                    "s2:nodata_pixel_percentage": record.get("nodata_pixel_percent"),
                },
                "assets": {
                    name: dict(record["assets"][name]) for name in REQUIRED_ASSETS
                },
            }
        )
    return scenes


def _reflectance(scene: dict[str, Any], name: str, transform, width: int, height: int) -> np.ndarray:
    asset = scene["assets"][name]
    raw = _read_asset_to_grid(
        asset["href"],
        transform,
        width,
        height,
        resampling=Resampling.bilinear,
    )
    if "scale" in asset or "offset" in asset:
        scale = float(asset.get("scale", 1.0))
        offset = float(asset.get("offset", 0.0))
    else:
        scale, offset = _asset_scale_offset(asset)
    return raw.astype("float32") * scale + offset


def _fractional_sampling_plan(
    metric: gpd.GeoDataFrame,
    *,
    transform,
    width: int,
    height: int,
    parcel_pixel_count: np.ndarray,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Return intersecting 10 m pixels for parcels with no pixel centre.

    The plan preserves the cadastral geometry and stores only temporary raster
    positions plus exact polygon/pixel overlap areas for the current calculation.
    """

    plan: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    inverse = ~transform
    for parcel_index in np.flatnonzero(parcel_pixel_count == 0):
        geometry = metric.geometry.iloc[int(parcel_index)]
        if geometry is None or geometry.is_empty:
            continue
        minx, miny, maxx, maxy = geometry.bounds
        left, bottom = inverse * (minx, miny)
        right, top = inverse * (maxx, maxy)
        col_start = max(0, int(np.floor(min(left, right))))
        col_stop = min(width, int(np.ceil(max(left, right))))
        row_start = max(0, int(np.floor(min(top, bottom))))
        row_stop = min(height, int(np.ceil(max(top, bottom))))
        flat_indices: list[int] = []
        overlap_areas: list[float] = []
        for row in range(row_start, row_stop):
            pixel_top = transform.f + row * transform.e
            pixel_bottom = pixel_top + transform.e
            for column in range(col_start, col_stop):
                pixel_left = transform.c + column * transform.a
                pixel_right = pixel_left + transform.a
                pixel = box(
                    min(pixel_left, pixel_right),
                    min(pixel_bottom, pixel_top),
                    max(pixel_left, pixel_right),
                    max(pixel_bottom, pixel_top),
                )
                overlap_area = float(geometry.intersection(pixel).area)
                if overlap_area <= 0:
                    continue
                flat_indices.append(row * width + column)
                overlap_areas.append(overlap_area)
        if flat_indices:
            plan[int(parcel_index)] = (
                np.asarray(flat_indices, dtype="int64"),
                np.asarray(overlap_areas, dtype="float64"),
            )
    return plan


def _safe_index(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    result = np.full(numerator.shape, np.nan, dtype="float64")
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (np.abs(denominator) > 1e-6)
    np.divide(numerator, denominator, out=result, where=valid)
    return np.clip(result, -1.0, 1.0)


def _apply_fractional_sampling(
    table: pd.DataFrame,
    *,
    plan: dict[int, tuple[np.ndarray, np.ndarray]],
    red: np.ndarray,
    green: np.ndarray,
    blue: np.ndarray,
    nir: np.ndarray,
    swir16: np.ndarray,
    scl: np.ndarray,
) -> pd.DataFrame:
    result = table.copy()
    result["sampling_method"] = "pixel_center_10m"
    result["spatial_support_m2"] = (
        result["parcel_pixel_count"].astype(float) * PIXEL_AREA_M2
    )
    result["spatial_support_pixel_count"] = result["parcel_pixel_count"].astype("int32")
    if not plan:
        return result

    red_flat = red.ravel()
    green_flat = green.ravel()
    blue_flat = blue.ravel()
    nir_flat = nir.ravel()
    swir_flat = swir16.ravel()
    scl_flat = scl.ravel().astype("int16")
    for parcel_index, (flat_indices, weights) in plan.items():
        red_values = red_flat[flat_indices]
        green_values = green_flat[flat_indices]
        blue_values = blue_flat[flat_indices]
        nir_values = nir_flat[flat_indices]
        swir_values = swir_flat[flat_indices]
        scl_values = scl_flat[flat_indices]
        reflectance_valid = np.logical_and.reduce(
            [
                np.isfinite(values)
                & (values >= MIN_VALID_SURFACE_REFLECTANCE)
                & (values <= MAX_VALID_SURFACE_REFLECTANCE)
                for values in (
                    red_values,
                    green_values,
                    blue_values,
                    nir_values,
                    swir_values,
                )
            ]
        )
        valid = reflectance_valid & np.isin(scl_values, (4, 5, 6, 7))
        valid_weight = float(weights[valid].sum())
        total_weight = float(weights.sum())
        result.at[parcel_index, "sampling_method"] = "fractional_10m_overlap"
        result.at[parcel_index, "spatial_support_m2"] = round(total_weight, 4)
        result.at[parcel_index, "spatial_support_pixel_count"] = int(len(flat_indices))
        result.at[parcel_index, "valid_fraction"] = round(
            valid_weight / total_weight if total_weight > 0 else np.nan,
            4,
        )
        result.at[parcel_index, "quality_status"] = "small_parcel_review"
        if valid_weight <= 0:
            continue

        ndvi = _safe_index(nir_values - red_values, nir_values + red_values)
        ndmi = _safe_index(nir_values - swir_values, nir_values + swir_values)
        bsi = _safe_index(
            (swir_values + red_values) - (nir_values + blue_values),
            (swir_values + red_values) + (nir_values + blue_values),
        )
        ndwi = _safe_index(green_values - nir_values, green_values + nir_values)

        def weighted_mean(values: np.ndarray) -> float:
            selected = valid & np.isfinite(values)
            selected_weight = float(weights[selected].sum())
            if selected_weight <= 0:
                return np.nan
            return float(np.average(values[selected], weights=weights[selected]))

        result.at[parcel_index, "valid_pixel_count"] = int(valid.sum())
        result.at[parcel_index, "ndvi_mean"] = round(weighted_mean(ndvi), 4)
        result.at[parcel_index, "ndmi_mean"] = round(weighted_mean(ndmi), 4)
        result.at[parcel_index, "bsi_mean"] = round(weighted_mean(bsi), 4)
        result.at[parcel_index, "ndwi_mean"] = round(weighted_mean(ndwi), 4)
        result.at[parcel_index, "vegetation_fraction"] = round(
            float(weights[valid & (scl_values == 4) & (ndvi >= 0.30)].sum())
            / valid_weight,
            4,
        )
        result.at[parcel_index, "bare_fraction"] = round(
            float(weights[valid & (scl_values == 5) & (ndvi <= 0.25)].sum())
            / valid_weight,
            4,
        )
        result.at[parcel_index, "surface_water_fraction"] = round(
            float(weights[valid & (scl_values == 6)].sum()) / valid_weight,
            4,
        )
    return result


def _scene_cache_path(
    scene_id: str,
    year: int,
    cache_dir: Path = CACHE_DIR,
) -> Path:
    safe = "".join(character for character in scene_id if character.isalnum() or character in "-_")
    return cache_dir / str(year) / f"{safe}.parquet"


def _legacy_scene_cache_path(scene_id: str) -> Path:
    safe = "".join(character for character in scene_id if character.isalnum() or character in "-_")
    return CACHE_DIR / f"{safe}.parquet"


def _build_scene_table(
    *,
    scene: dict[str, Any],
    cadastre_codes: list[str],
    labels: np.ndarray,
    parcel_pixel_count: np.ndarray,
    transform,
    width: int,
    height: int,
    parcel_geometry_version: str,
    analysis_version: str,
    fractional_plan: dict[int, tuple[np.ndarray, np.ndarray]] | None = None,
) -> pd.DataFrame:
    red = _reflectance(scene, "red", transform, width, height)
    green = _reflectance(scene, "green", transform, width, height)
    blue = _reflectance(scene, "blue", transform, width, height)
    nir = _reflectance(scene, "nir", transform, width, height)
    swir16 = _reflectance(scene, "swir16", transform, width, height)
    scl = _read_asset_to_grid(
        scene["assets"]["scl"]["href"],
        transform,
        width,
        height,
        resampling=Resampling.nearest,
    ).astype("uint8")
    table = aggregate_optical_scene_by_parcel(
        cadastre_codes=cadastre_codes,
        labels=labels,
        parcel_pixel_count=parcel_pixel_count,
        red=red,
        green=green,
        blue=blue,
        nir=nir,
        swir16=swir16,
        scl=scl,
    )
    table = _apply_fractional_sampling(
        table,
        plan=fractional_plan or {},
        red=red,
        green=green,
        blue=blue,
        nir=nir,
        swir16=swir16,
        scl=scl,
    )
    properties = scene.get("properties", {})
    observation_date = pd.Timestamp(properties["datetime"]).date().isoformat()
    table.insert(0, "schema_version", PARCEL_EO_SCHEMA_VERSION)
    table.insert(1, "analysis_version", analysis_version)
    table.insert(3, "parcel_geometry_version", parcel_geometry_version)
    table.insert(4, "observation_date", observation_date)
    table.insert(5, "scene_id", str(scene["id"]))
    table.insert(6, "source_program", "Copernicus Sentinel-2 L2A")
    table.insert(7, "access_provider", "AWS Earth Search / Element 84")
    table["scene_cloud_cover_percent"] = properties.get("eo:cloud_cover")
    table["public_release_approved"] = False
    return table


def _select_interval_scenes(
    items: list[dict[str, Any]],
    *,
    season_start: str,
    interval_days: int,
) -> list[dict[str, Any]]:
    start = pd.Timestamp(season_start)
    selected: dict[int, tuple[tuple[float, float, str], dict[str, Any]]] = {}
    for item in items:
        properties = item.get("properties", {})
        timestamp = pd.Timestamp(properties.get("datetime"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        period = int((timestamp - start).days // interval_days)
        cloud_value = properties.get("eo:cloud_cover")
        nodata_value = properties.get("s2:nodata_pixel_percentage")
        cloud = float(100.0 if cloud_value is None else cloud_value)
        nodata = float(100.0 if nodata_value is None else nodata_value)
        score = (nodata, cloud, str(item.get("id", "")))
        if period not in selected or score < selected[period][0]:
            selected[period] = (score, item)
    return [selected[key][1] for key in sorted(selected)]


def build_timeseries(
    *,
    year: int = DEFAULT_ANALYSIS_YEAR,
    refresh: bool = False,
    reuse_manifest: bool = False,
    parcel_source: Path = PARCEL_SOURCE,
    output_dir: Path = OUTPUT_DIR,
    dataset_id: str | None = None,
    scene_manifest_source: Path | None = None,
) -> dict[str, Any]:
    if year < 2017 or year > datetime.now(timezone.utc).year:
        raise ValueError("Sentinel-2 parcel analysis year must be between 2017 and the current year")
    season_start, season_end = _season_bounds(year)
    analysis_version = _analysis_version(year, dataset_id)
    collection = _collection_for_year(year)
    cache_dir = output_dir / "cache"
    output_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / str(year)).mkdir(parents=True, exist_ok=True)
    if parcel_source.suffix.lower() in {".parquet", ".geoparquet"}:
        parcels = gpd.read_parquet(parcel_source)
    else:
        parcels = gpd.read_file(parcel_source)
    if parcels.crs is None:
        raise ValueError("Canonical parcel source has no CRS")
    parcels = parcels.to_crs("EPSG:4326")
    if parcels["cadastre_code"].duplicated().any():
        raise ValueError("Canonical parcel source contains duplicate cadastral codes")
    parcel_geometry_version = sha256_file(parcel_source)

    if reuse_manifest:
        manifest = json.loads(
            _manifest_path(year, output_dir).read_text(encoding="utf-8")
        )
        if manifest.get("parcel_geometry_version") != parcel_geometry_version:
            raise ValueError("Stored scene manifest targets another parcel geometry version")
        if int(manifest.get("analysis_year", 0)) != year:
            raise ValueError("Stored scene manifest targets another analysis year")
        scenes = _manifest_scenes(manifest)
    elif scene_manifest_source is not None:
        source_manifest = json.loads(scene_manifest_source.read_text(encoding="utf-8"))
        if int(source_manifest.get("analysis_year", 0)) != year:
            raise ValueError("Source scene manifest targets another analysis year")
        if source_manifest.get("collection") != collection:
            raise ValueError("Source scene manifest targets another Copernicus collection")
        scenes = _manifest_scenes(source_manifest)
        manifest = _write_manifest(
            year=year,
            season_start=season_start,
            season_end=season_end,
            available_count=int(source_manifest.get("available_scene_count", len(scenes))),
            scenes=scenes,
            parcel_geometry_version=parcel_geometry_version,
            collection=collection,
            output_dir=output_dir,
            analysis_version=analysis_version,
            selection_source=f"reused_reviewed_manifest:{scene_manifest_source.as_posix()}",
        )
    else:
        items = _query_items(
            parcels.total_bounds,
            start=season_start,
            end=season_end,
            max_cloud_percent=MAX_SCENE_CLOUD_PERCENT,
            collection=collection,
        )
        scenes = _select_interval_scenes(
            items,
            season_start=season_start,
            interval_days=SCENE_INTERVAL_DAYS,
        )
        manifest = _write_manifest(
            year=year,
            season_start=season_start,
            season_end=season_end,
            available_count=len(items),
            scenes=scenes,
            parcel_geometry_version=parcel_geometry_version,
            collection=collection,
            output_dir=output_dir,
            analysis_version=analysis_version,
        )

    metric = parcels.to_crs(GRID_CRS)
    transform, width, height, _ = _grid_from_bounds(metric.total_bounds)
    labels = rasterize(
        (
            (geometry, index + 1)
            for index, geometry in enumerate(metric.geometry)
            if geometry is not None and not geometry.is_empty
        ),
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=False,
    )
    parcel_pixel_count = np.bincount(
        labels[labels > 0], minlength=len(metric) + 1
    )[1:].astype("int32")
    fractional_plan = _fractional_sampling_plan(
        metric,
        transform=transform,
        width=width,
        height=height,
        parcel_pixel_count=parcel_pixel_count,
    )
    cadastre_codes = parcels["cadastre_code"].astype(str).tolist()

    scene_tables: list[pd.DataFrame] = []
    for index, scene in enumerate(scenes, start=1):
        cache_path = _scene_cache_path(str(scene["id"]), year, cache_dir)
        legacy_cache_path = _legacy_scene_cache_path(str(scene["id"]))
        if year == DEFAULT_ANALYSIS_YEAR and legacy_cache_path.exists() and not cache_path.exists():
            cache_path = legacy_cache_path
        if cache_path.exists() and not refresh:
            table = pd.read_parquet(cache_path)
            reusable = (
                len(table) == len(parcels)
                and set(table["parcel_geometry_version"].astype(str))
                == {parcel_geometry_version}
            )
            if not reusable:
                table = _build_scene_table(
                    scene=scene,
                    cadastre_codes=cadastre_codes,
                    labels=labels,
                    parcel_pixel_count=parcel_pixel_count,
                    transform=transform,
                    width=width,
                    height=height,
                    parcel_geometry_version=parcel_geometry_version,
                    analysis_version=analysis_version,
                    fractional_plan=fractional_plan,
                )
                table.to_parquet(cache_path, index=False, compression="zstd")
        else:
            table = _build_scene_table(
                scene=scene,
                cadastre_codes=cadastre_codes,
                labels=labels,
                parcel_pixel_count=parcel_pixel_count,
                transform=transform,
                width=width,
                height=height,
                parcel_geometry_version=parcel_geometry_version,
                analysis_version=analysis_version,
                fractional_plan=fractional_plan,
            )
            table.to_parquet(cache_path, index=False, compression="zstd")
        table = attach_parcel_identity(table)
        table["schema_version"] = PARCEL_EO_SCHEMA_VERSION
        table["analysis_version"] = analysis_version
        scene_tables.append(table)
        print(f"Parcel EO scene {index}/{len(scenes)}: {scene['id']}", flush=True)

    observations = pd.concat(scene_tables, ignore_index=True)
    observations = add_gap_safe_temporal_features(observations, maximum_gap_days=21)
    output_path = _output_path(year, output_dir)
    observations.to_parquet(output_path, index=False, compression="zstd")
    validation = validate_parcel_eo_timeseries(observations)
    quality = {
        "schema_version": 1,
        "analysis_version": analysis_version,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_program": "Copernicus Sentinel-2 L2A",
        "access_provider": "AWS Earth Search / Element 84",
        "collection": collection,
        "parcel_source": str(parcel_source),
        "parcel_source_sha256": parcel_geometry_version,
        "analysis_year": year,
        "period": [season_start, season_end],
        "scene_count": len(scenes),
        "grid_resolution_m": 10,
        "pixel_center_parcel_count": int((parcel_pixel_count > 0).sum()),
        "fractional_overlap_parcel_count": int(len(fractional_plan)),
        "unresolved_spatial_support_parcel_count": int(
            (parcel_pixel_count == 0).sum() - len(fractional_plan)
        ),
        "calculation_grid_is_not_a_public_land_unit": True,
        "legacy_250m_weekly_grid_used": False,
        "public_release_approved": False,
        **validation,
    }
    _quality_path(year, output_dir).write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return quality


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=DEFAULT_ANALYSIS_YEAR)
    parser.add_argument("--refresh", action="store_true", help="Rebuild every scene cache")
    parser.add_argument("--parcel-source", type=Path, default=PARCEL_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dataset-id")
    parser.add_argument("--scene-manifest-source", type=Path)
    parser.add_argument(
        "--reuse-manifest",
        action="store_true",
        help="Reuse the stored scene selection but still read its public Copernicus assets",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            build_timeseries(
                year=args.year,
                refresh=args.refresh,
                reuse_manifest=args.reuse_manifest,
                parcel_source=args.parcel_source,
                output_dir=args.output_dir,
                dataset_id=args.dataset_id,
                scene_manifest_source=args.scene_manifest_source,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
