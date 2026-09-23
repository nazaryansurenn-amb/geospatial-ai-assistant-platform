from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin

from road_exclusions import build_road_exclusion_metrics


PRODUCT_ROOT = Path(__file__).resolve().parents[1]

CADASTRE_SOURCE = PRODUCT_ROOT / "data" / "source" / "cadastre" / "parcels_wua.gpkg"
LAND_REVIEW_SOURCE = (
    PRODUCT_ROOT / "data" / "source" / "activity_2026"
    / "parcel_land_review.parquet"
)
HALO_SOURCE = PRODUCT_ROOT / "public" / "data" / "lower_hrazdan_halos.geojson"
GRID_METADATA_SOURCE = (
    PRODUCT_ROOT / "data" / "source" / "activity_2026"
    / "lower_hrazdan_eo_terrain_grid_2026.json"
)
GRID_CACHE_SOURCE = GRID_METADATA_SOURCE.with_suffix(".npz")
ROAD_SOURCE = PRODUCT_ROOT / "data" / "analysis" / "osm_roads_v1.geojson"

OUTPUT_DIR = PRODUCT_ROOT / "server_data" / "review"
OUTPUT_PATH = OUTPUT_DIR / "land_activity_2026_preview.json"
QUALITY_PATH = OUTPUT_DIR / "land_activity_2026_preview_quality.json"
ROAD_EXCLUSION_PATH = OUTPUT_DIR / "land_activity_2026_road_exclusions.parquet"
RASTER_DIR = PRODUCT_ROOT / "public" / "data" / "review"

ANALYSIS_VERSION = "lower_hrazdan_activity_road_exclusions_v3_5_2026"
METRIC_CRS = "EPSG:32638"
MINIMUM_OVERLAP_RATIO = 0.50
MIN_VALID_OBSERVATIONS = 4
MINIMUM_PIXEL_COUNT = 3
MINIMUM_USABLE_FRACTION = 0.40
MINIMUM_PARTIAL_ACTIVITY_FRACTION = 0.10
MINIMUM_ACTIVE_ACTIVITY_FRACTION = 0.55
MAX_HOUSEHOLD_PLOT_AREA_M2 = 5_000.0
MAX_EXTENDED_HOUSEHOLD_PLOT_AREA_M2 = 15_000.0
MIN_HOUSEHOLD_URBAN_CONTEXT_FRACTION = 0.30
MIN_HOUSEHOLD_URBAN_DOMINANT_FRACTION = 0.70
MIN_HOUSEHOLD_BUILT_FRACTION = 0.05
MIN_EXTENDED_HOUSEHOLD_URBAN_FRACTION = 0.85
MIN_EXTENDED_HOUSEHOLD_BUILT_FRACTION = 0.02
WORLDCOVER_BUILT_CLASS = 50
PARCEL_ID_NAMESPACE = "echmiadzin-cadastral-parcel-v1"
MAX_SAFE_JAVASCRIPT_INTEGER = (1 << 53) - 1

COLOR_STOPS = [
    (0.00, "#df3f4a"),
    (0.10, "#e96a4f"),
    (0.30, "#f0c64a"),
    (0.55, "#9ed56c"),
    (0.80, "#31c982"),
    (1.00, "#15965e"),
]
REVIEW_COLOR = "#9b5de5"
HOUSEHOLD_COLOR = "#d66d9e"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_parcel_id(cadastre_code: object) -> int:
    code = str(cadastre_code).strip()
    digest = hashlib.sha256(
        f"{PARCEL_ID_NAMESPACE}:public:{code}".encode("utf-8")
    ).digest()
    value = int.from_bytes(digest[:8], "big") & MAX_SAFE_JAVASCRIPT_INTEGER
    return value or 1


def count_pixels(labels: np.ndarray, mask: np.ndarray, size: int) -> np.ndarray:
    selected = mask & (labels > 0)
    return np.bincount(labels[selected], minlength=size + 1)[1:]


def fraction(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    result = np.zeros_like(numerator, dtype="float64")
    np.divide(numerator, denominator, out=result, where=denominator > 0)
    return result


def extract_activity_from_grid(
    parcels: gpd.GeoDataFrame,
    arrays: dict[str, np.ndarray],
    transform,
) -> pd.DataFrame:
    metric = parcels.to_crs(METRIC_CRS)
    labels = rasterize(
        (
            (geometry, index + 1)
            for index, geometry in enumerate(metric.geometry)
            if geometry is not None and not geometry.is_empty
        ),
        out_shape=arrays["valid_count"].shape,
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=False,
    )
    size = len(metric)
    total_pixels = count_pixels(labels, np.ones(labels.shape, dtype=bool), size)
    sufficient = arrays["valid_count"] >= MIN_VALID_OBSERVATIONS
    vegetation_fraction = np.zeros_like(arrays["valid_count"], dtype="float32")
    np.divide(
        arrays["vegetation_count"],
        arrays["valid_count"],
        out=vegetation_fraction,
        where=arrays["valid_count"] > 0,
    )
    active_pixels = (
        sufficient
        & (arrays["ndvi_max"] >= 0.45)
        & (arrays["vegetation_count"] >= 2)
        & (vegetation_fraction >= 0.15)
    )
    usable_fraction = fraction(count_pixels(labels, sufficient, size), total_pixels)
    active_fraction = fraction(count_pixels(labels, active_pixels, size), total_pixels)
    urban_fraction = fraction(
        count_pixels(labels, arrays["urban_mask"].astype(bool), size), total_pixels
    )
    built_fraction = fraction(
        count_pixels(labels, arrays["worldcover"] == WORLDCOVER_BUILT_CLASS, size),
        total_pixels,
    )
    return pd.DataFrame(
        {
            "cadastre_code": metric["cadastre_code"].astype(str).to_numpy(),
            "eo_pixel_count": total_pixels.astype("int32"),
            "raw_usable_fraction": np.round(usable_fraction, 4),
            "raw_active_fraction": np.round(active_fraction, 4),
            "osm_urban_fraction": np.round(urban_fraction, 4),
            "worldcover_built_fraction": np.round(built_fraction, 4),
        }
    )


def colorize(values: np.ndarray, visible: np.ndarray) -> np.ndarray:
    rgba = np.zeros((*values.shape, 4), dtype="uint8")
    stops = np.asarray([value for value, _ in COLOR_STOPS], dtype="float32")
    colors = np.asarray(
        [tuple(int(color[index : index + 2], 16) for index in (1, 3, 5)) for _, color in COLOR_STOPS],
        dtype="float32",
    )
    clipped = np.clip(values, 0.0, 1.0)
    for channel in range(3):
        rgba[..., channel] = np.interp(clipped, stops, colors[:, channel]).astype("uint8")
    rgba[..., 3] = np.where(visible, 220, 0).astype("uint8")
    return rgba


def assign_stage(parcels: gpd.GeoDataFrame, halos: gpd.GeoDataFrame) -> pd.DataFrame:
    metric = parcels.to_crs(METRIC_CRS).copy()
    halo_metric = halos.to_crs(METRIC_CRS)
    representatives = metric.geometry.representative_point()
    area = metric.geometry.area.to_numpy(dtype="float64")
    stage_scores: dict[str, np.ndarray] = {}
    stage_contains: dict[str, np.ndarray] = {}

    for row in halo_metric.itertuples(index=False):
        stage = str(row.stage)
        geometry = row.geometry
        stage_contains[stage] = representatives.within(geometry).to_numpy(dtype=bool)
        intersections = metric.geometry.intersection(geometry).area.to_numpy(dtype="float64")
        stage_scores[stage] = np.divide(
            intersections,
            area,
            out=np.zeros_like(intersections),
            where=area > 0,
        )

    stages = sorted(stage_scores)
    score_matrix = np.column_stack([stage_scores[stage] for stage in stages])
    contains_matrix = np.column_stack([stage_contains[stage] for stage in stages])
    qualifies = contains_matrix | (score_matrix >= MINIMUM_OVERLAP_RATIO)
    winning_index = score_matrix.argmax(axis=1)
    any_qualifies = qualifies.any(axis=1)
    assigned = np.full(len(metric), "outside", dtype=object)
    for index, stage in enumerate(stages):
        assigned[any_qualifies & (winning_index == index)] = stage

    return pd.DataFrame(
        {
            "cadastre_code": metric["cadastre_code"].astype(str).to_numpy(),
            "activity_stage": assigned,
            "stage_overlap_ratio": score_matrix.max(axis=1),
            "stage_assignment_ambiguous": qualifies.sum(axis=1) > 1,
        }
    )


def scope_summary(frame: pd.DataFrame) -> dict[str, object]:
    roads = frame[frame["road_excluded"]]
    ready = frame[frame["preview_state"].eq("ready") & ~frame["road_excluded"]]
    household = frame[frame["household_agriculture"] & ~frame["road_excluded"]]
    open_field = ready[~ready["household_agriculture"]]
    activity_review = frame[
        frame["preview_state"].eq("review")
        & ~frame["household_agriculture"]
        & ~frame["road_excluded"]
    ]
    return {
        "parcel_count": int(len(frame)),
        "ready_parcel_count": int(len(ready)),
        "open_field_parcel_count": int(len(open_field)),
        "household_parcel_count": int(len(household)),
        "review_parcel_count": int(len(frame) - len(ready)),
        "activity_review_parcel_count": int(len(activity_review)),
        "road_excluded_parcel_count": int(len(roads)),
        "road_excluded_area_ha": round(float(roads["area_ha"].sum()), 2),
        "activity_class_counts": {
            activity_class: int(open_field["activity_class"].eq(activity_class).sum())
            for activity_class in ("active", "partial", "no_current_activity")
        },
        "household_activity_review_parcel_count": int(
            household["preview_state"].eq("review").sum()
        ),
        "official_area_ha": round(float(frame["area_ha"].sum()), 2),
        "household_area_ha": round(float(household["area_ha"].sum()), 2),
        "observed_active_area_ha": round(
            float(open_field["observed_active_area_ha"].sum()), 2
        ),
    }


def write_raster(
    frame: gpd.GeoDataFrame,
    *,
    stage: str | None,
    activity_class: str | None = None,
    path: Path,
    transform,
    width: int,
    height: int,
) -> None:
    selected = frame[
        frame["preview_state"].eq("ready")
        & ~frame["household_agriculture"]
        & ~frame["road_excluded"]
    ].copy()
    if stage is not None:
        selected = selected[selected["activity_stage"].eq(stage)]
    if activity_class is not None:
        selected = selected[selected["activity_class"].eq(activity_class)]
    values = rasterize(
        (
            (geometry, float(activity_fraction))
            for geometry, activity_fraction in zip(
                selected.geometry,
                selected["activity_fraction"],
                strict=True,
            )
        ),
        out_shape=(height, width),
        transform=transform,
        fill=-1.0,
        dtype="float32",
        all_touched=False,
    )
    visible = values >= 0
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(colorize(np.where(visible, values, 0.0), visible), mode="RGBA").save(
        path,
        optimize=True,
    )


def write_review_raster(
    frame: gpd.GeoDataFrame,
    *,
    stage: str | None,
    path: Path,
    transform,
    width: int,
    height: int,
) -> None:
    selected = frame[
        frame["preview_state"].eq("review")
        & ~frame["household_agriculture"]
        & ~frame["road_excluded"]
    ].copy()
    if stage is not None:
        selected = selected[selected["activity_stage"].eq(stage)]
    mask = rasterize(
        ((geometry, 1) for geometry in selected.geometry),
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=False,
    )
    rgba = np.zeros((height, width, 4), dtype="uint8")
    color = tuple(int(REVIEW_COLOR[index : index + 2], 16) for index in (1, 3, 5))
    rgba[..., :3] = color
    rgba[..., 3] = np.where(mask > 0, 230, 0).astype("uint8")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)


def write_household_raster(
    frame: gpd.GeoDataFrame,
    *,
    stage: str | None,
    path: Path,
    transform,
    width: int,
    height: int,
) -> None:
    selected = frame[
        frame["household_agriculture"] & ~frame["road_excluded"]
    ].copy()
    if stage is not None:
        selected = selected[selected["activity_stage"].eq(stage)]
    mask = rasterize(
        ((geometry, 1) for geometry in selected.geometry),
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=False,
    )
    rgba = np.zeros((height, width, 4), dtype="uint8")
    color = tuple(int(HOUSEHOLD_COLOR[index : index + 2], 16) for index in (1, 3, 5))
    rgba[..., :3] = color
    rgba[..., 3] = np.where(mask > 0, 220, 0).astype("uint8")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)


def enforce_raster_display_priority(
    activity_path: Path,
    review_path: Path,
    household_path: Path,
) -> None:
    activity = np.asarray(Image.open(activity_path).convert("RGBA")).copy()
    review = np.asarray(Image.open(review_path).convert("RGBA")).copy()
    household = np.asarray(Image.open(household_path).convert("RGBA"))

    household_visible = household[..., 3] > 0
    review[..., 3][household_visible] = 0
    review_visible = review[..., 3] > 0
    activity[..., 3][household_visible | review_visible] = 0

    Image.fromarray(review, mode="RGBA").save(review_path, optimize=True)
    Image.fromarray(activity, mode="RGBA").save(activity_path, optimize=True)


def enforce_activity_class_raster_priority(
    class_paths: dict[str, Path],
    review_path: Path,
    household_path: Path,
) -> None:
    review = np.asarray(Image.open(review_path).convert("RGBA"))
    household = np.asarray(Image.open(household_path).convert("RGBA"))
    occupied = (review[..., 3] > 0) | (household[..., 3] > 0)

    for activity_class in ("active", "partial", "no_current_activity"):
        path = class_paths[activity_class]
        rgba = np.asarray(Image.open(path).convert("RGBA")).copy()
        rgba[..., 3][occupied] = 0
        occupied |= rgba[..., 3] > 0
        Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)


def main() -> None:
    required = [
        CADASTRE_SOURCE,
        LAND_REVIEW_SOURCE,
        HALO_SOURCE,
        GRID_METADATA_SOURCE,
        GRID_CACHE_SOURCE,
        ROAD_SOURCE,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing sources: {missing}")

    cadastre = gpd.read_file(
        CADASTRE_SOURCE,
        layer="parcels",
        columns=["cadastre_code", "area_official_m2", "geometry"],
    )
    if cadastre.crs is None or cadastre.crs.to_epsg() != 4326:
        raise ValueError(f"Expected cadastral EPSG:4326, found {cadastre.crs}")
    if cadastre["cadastre_code"].astype(str).duplicated().any():
        raise ValueError("Cadastral source contains duplicate codes")
    cadastre["parcel_id"] = cadastre["cadastre_code"].map(public_parcel_id).astype("int64")
    if cadastre["parcel_id"].duplicated().any():
        raise ValueError("Deterministic public parcel ID collision")

    halos = gpd.read_file(HALO_SOURCE)
    cadastre["cadastre_code"] = cadastre["cadastre_code"].astype(str)
    stage = assign_stage(cadastre, halos)
    parcels = cadastre.merge(stage, on="cadastre_code", how="left", validate="1:1")
    parcels = parcels[parcels["activity_stage"].ne("outside")].copy()
    if len(parcels) != 43_984:
        raise ValueError(f"Unexpected full-halo parcel count: {len(parcels)}")

    grid = json.loads(GRID_METADATA_SOURCE.read_text(encoding="utf-8"))
    scene_date_tokens = sorted(
        {
            part[:8]
            for scene_id in grid["selected_scene_ids"]
            for part in str(scene_id).split("_")
            if len(part) >= 8 and part[:8].isdigit()
        }
    )
    if not scene_date_tokens:
        raise ValueError("Grid metadata has no parseable Sentinel-2 scene dates")
    observation_start = (
        f"{scene_date_tokens[0][:4]}-{scene_date_tokens[0][4:6]}-"
        f"{scene_date_tokens[0][6:8]}"
    )
    observation_end = (
        f"{scene_date_tokens[-1][:4]}-{scene_date_tokens[-1][4:6]}-"
        f"{scene_date_tokens[-1][6:8]}"
    )
    width = int(grid["width"])
    height = int(grid["height"])
    minx, miny, maxx, maxy = [float(value) for value in grid["bounds"]]
    transform = from_origin(minx, maxy, 10.0, 10.0)
    with np.load(GRID_CACHE_SOURCE) as cache:
        arrays = {key: cache[key] for key in cache.files}
    activity = extract_activity_from_grid(parcels, arrays, transform)
    result = parcels.merge(activity, on="cadastre_code", how="left", validate="1:1")
    roads = gpd.read_file(ROAD_SOURCE)
    road_metrics, road_quality = build_road_exclusion_metrics(parcels, roads)
    road_metrics["analysis_version"] = ANALYSIS_VERSION
    ROAD_EXCLUSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    road_metrics.to_parquet(ROAD_EXCLUSION_PATH, index=False, compression="zstd")
    result = result.merge(road_metrics, on="cadastre_code", how="left", validate="1:1")

    land_review = pd.read_parquet(LAND_REVIEW_SOURCE)[
        ["cadastre_code", "greenhouse_review_candidate", "household_agriculture"]
    ].copy()
    land_review = land_review.rename(
        columns={"household_agriculture": "previous_household_agriculture"}
    )
    land_review["cadastre_code"] = land_review["cadastre_code"].astype(str)
    if land_review["cadastre_code"].duplicated().any():
        raise ValueError("Land-review source contains duplicate cadastral codes")
    result = result.merge(land_review, on="cadastre_code", how="left", validate="1:1")

    result["area_ha"] = result["area_official_m2"].astype(float) / 10_000.0
    result["activity_fraction"] = result["raw_active_fraction"].astype(float).clip(0, 1)
    result["observed_active_area_ha"] = result["area_ha"] * result["activity_fraction"]
    small_parcel = result["eo_pixel_count"].lt(MINIMUM_PIXEL_COUNT)
    low_observation_coverage = result["raw_usable_fraction"].lt(
        MINIMUM_USABLE_FRACTION
    )
    greenhouse_review = (
        result["greenhouse_review_candidate"].astype("boolean").fillna(False).astype(bool)
    )
    result["review_reason"] = np.select(
        [
            greenhouse_review,
            small_parcel & low_observation_coverage,
            small_parcel,
            low_observation_coverage,
        ],
        [
            "greenhouse_review",
            "small_parcel_and_observation_review",
            "small_parcel_review",
            "observation_coverage_review",
        ],
        default="ready",
    )
    result["preview_state"] = np.where(
        result["review_reason"].eq("ready"), "ready", "review"
    )
    result["activity_class"] = np.select(
        [
            result["activity_fraction"].ge(MINIMUM_ACTIVE_ACTIVITY_FRACTION),
            result["activity_fraction"].ge(MINIMUM_PARTIAL_ACTIVITY_FRACTION),
        ],
        ["active", "partial"],
        default="no_current_activity",
    )
    urban_size_household = (
        result["area_official_m2"].astype(float).le(MAX_HOUSEHOLD_PLOT_AREA_M2)
        & result["osm_urban_fraction"].ge(MIN_HOUSEHOLD_URBAN_CONTEXT_FRACTION)
        & (
            result["osm_urban_fraction"].ge(MIN_HOUSEHOLD_URBAN_DOMINANT_FRACTION)
            | result["worldcover_built_fraction"].ge(MIN_HOUSEHOLD_BUILT_FRACTION)
        )
    )
    extended_urban_household = (
        result["area_official_m2"]
        .astype(float)
        .between(
            MAX_HOUSEHOLD_PLOT_AREA_M2,
            MAX_EXTENDED_HOUSEHOLD_PLOT_AREA_M2,
            inclusive="right",
        )
        & result["osm_urban_fraction"].ge(
            MIN_EXTENDED_HOUSEHOLD_URBAN_FRACTION
        )
        & result["worldcover_built_fraction"].ge(
            MIN_EXTENDED_HOUSEHOLD_BUILT_FRACTION
        )
    )
    previous_household = (
        result["previous_household_agriculture"]
        .astype("boolean")
        .fillna(False)
        .astype(bool)
    )
    result["road_excluded"] = result["road_excluded"].fillna(False).astype(bool)
    result["household_agriculture"] = (
        (previous_household | urban_size_household | extended_urban_household)
        & ~result["road_excluded"]
    ).astype(bool)
    result["household_identification_source"] = np.select(
        [previous_household, urban_size_household, extended_urban_household],
        [
            "previous_household_preserved",
            "urban_size_rule",
            "extended_urban_built_rule",
        ],
        default="urban_size_rule_not_matched",
    )

    raster_frame = result.to_crs(METRIC_CRS)
    raster_files = {
        "lower_hrazdan": RASTER_DIR / "land_activity_2026_lower_hrazdan.png",
        "stage_1": RASTER_DIR / "land_activity_2026_stage_1.png",
        "stage_2": RASTER_DIR / "land_activity_2026_stage_2.png",
    }
    activity_class_raster_files = {
        activity_class: {
            scope: RASTER_DIR / f"land_activity_2026_{activity_class}_{scope}.png"
            for scope in ("lower_hrazdan", "stage_1", "stage_2")
        }
        for activity_class in ("active", "partial", "no_current_activity")
    }
    review_raster_files = {
        "lower_hrazdan": RASTER_DIR / "land_activity_2026_review_lower_hrazdan.png",
        "stage_1": RASTER_DIR / "land_activity_2026_review_stage_1.png",
        "stage_2": RASTER_DIR / "land_activity_2026_review_stage_2.png",
    }
    household_raster_files = {
        "lower_hrazdan": RASTER_DIR / "land_activity_2026_household_lower_hrazdan.png",
        "stage_1": RASTER_DIR / "land_activity_2026_household_stage_1.png",
        "stage_2": RASTER_DIR / "land_activity_2026_household_stage_2.png",
    }
    write_raster(
        raster_frame,
        stage=None,
        path=raster_files["lower_hrazdan"],
        transform=transform,
        width=width,
        height=height,
    )
    for stage_name in ("stage_1", "stage_2"):
        write_raster(
            raster_frame,
            stage=stage_name,
            path=raster_files[stage_name],
            transform=transform,
            width=width,
            height=height,
        )
    for activity_class, class_files in activity_class_raster_files.items():
        write_raster(
            raster_frame,
            stage=None,
            activity_class=activity_class,
            path=class_files["lower_hrazdan"],
            transform=transform,
            width=width,
            height=height,
        )
        for stage_name in ("stage_1", "stage_2"):
            write_raster(
                raster_frame,
                stage=stage_name,
                activity_class=activity_class,
                path=class_files[stage_name],
                transform=transform,
                width=width,
                height=height,
            )
    write_review_raster(
        raster_frame,
        stage=None,
        path=review_raster_files["lower_hrazdan"],
        transform=transform,
        width=width,
        height=height,
    )
    for stage_name in ("stage_1", "stage_2"):
        write_review_raster(
            raster_frame,
            stage=stage_name,
            path=review_raster_files[stage_name],
            transform=transform,
            width=width,
            height=height,
        )
    write_household_raster(
        raster_frame,
        stage=None,
        path=household_raster_files["lower_hrazdan"],
        transform=transform,
        width=width,
        height=height,
    )
    for stage_name in ("stage_1", "stage_2"):
        write_household_raster(
            raster_frame,
            stage=stage_name,
            path=household_raster_files[stage_name],
            transform=transform,
            width=width,
            height=height,
        )
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        enforce_raster_display_priority(
            raster_files[scope],
            review_raster_files[scope],
            household_raster_files[scope],
        )
        enforce_activity_class_raster_priority(
            {
                activity_class: class_files[scope]
                for activity_class, class_files in activity_class_raster_files.items()
            },
            review_raster_files[scope],
            household_raster_files[scope],
        )

    transformer = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)
    west, south = transformer.transform(minx, miny)
    east, north = transformer.transform(maxx, maxy)
    raster_coordinates = [
        [west, north],
        [east, north],
        [east, south],
        [west, south],
    ]

    summaries = {
        "lower_hrazdan": scope_summary(result),
        "stage_1": scope_summary(result[result["activity_stage"].eq("stage_1")]),
        "stage_2": scope_summary(result[result["activity_stage"].eq("stage_2")]),
    }
    review_reason_counts = {
        key: int(value)
        for key, value in sorted(
            result.loc[result["preview_state"].eq("review"), "review_reason"]
            .value_counts()
            .to_dict()
            .items()
        )
    }
    records = [
        [
            int(row.parcel_id),
            str(row.cadastre_code),
            str(row.activity_stage),
            round(float(row.activity_fraction), 4),
            round(float(row.observed_active_area_ha), 4),
            str(row.preview_state),
            bool(row.household_agriculture),
            str(row.activity_class),
            bool(row.road_excluded),
        ]
        for row in result.itertuples(index=False)
    ]
    payload = {
        "analysis_version": ANALYSIS_VERSION,
        "status": "draft_owner_review",
        "analysis_year": 2026,
        "latest_parcel_observation_date": observation_end,
        "pixel_activity_window_end": observation_end,
        "season_complete": False,
        "public_release_approved": False,
        "raster_coordinates": raster_coordinates,
        "rasters": {
            key: f"/data/review/{path.name}" for key, path in raster_files.items()
        },
        "activity_class_rasters": {
            activity_class: {
                key: f"/data/review/{path.name}" for key, path in class_files.items()
            }
            for activity_class, class_files in activity_class_raster_files.items()
        },
        "review_rasters": {
            key: f"/data/review/{path.name}"
            for key, path in review_raster_files.items()
        },
        "household_rasters": {
            key: f"/data/review/{path.name}"
            for key, path in household_raster_files.items()
        },
        "summaries": summaries,
        "record_schema": [
            "parcel_id",
            "cadastre_code",
            "stage",
            "activity_fraction",
            "observed_active_area_ha",
            "preview_state",
            "household_agriculture",
            "activity_class",
            "road_excluded",
        ],
        "records": records,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    quality = {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "draft_owner_review",
        "spatial_unit": "immutable_cadastral_parcel",
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "source_activity_method": "10 m Sentinel-2 seasonal active-pixel share",
        "pixel_activity_window": [observation_start, observation_end],
        "season_complete": False,
        "final_class_thresholds_assigned": False,
        "public_release_approved": False,
        "stage_assignment_rule": (
            "representative point inside analytical outline or overlap ratio >= 0.50; "
            "maximum overlap wins"
        ),
        "ambiguous_stage_assignment_count": int(result["stage_assignment_ambiguous"].sum()),
        "full_halo_cadastral_coverage": True,
        "full_halo_parcel_count": int(len(result)),
        "review_reason_counts": review_reason_counts,
        "summaries": summaries,
        "color_stops": COLOR_STOPS,
        "activity_class_thresholds": {
            "active_minimum_fraction": MINIMUM_ACTIVE_ACTIVITY_FRACTION,
            "partial_minimum_fraction": MINIMUM_PARTIAL_ACTIVITY_FRACTION,
            "no_current_activity_maximum_fraction_exclusive": MINIMUM_PARTIAL_ACTIVITY_FRACTION,
        },
        "review_color": REVIEW_COLOR,
        "household_color": HOUSEHOLD_COLOR,
        "household_rule": {
            "maximum_plot_area_m2": MAX_HOUSEHOLD_PLOT_AREA_M2,
            "maximum_extended_plot_area_m2": MAX_EXTENDED_HOUSEHOLD_PLOT_AREA_M2,
            "minimum_osm_urban_context_fraction": MIN_HOUSEHOLD_URBAN_CONTEXT_FRACTION,
            "minimum_osm_urban_dominant_fraction": MIN_HOUSEHOLD_URBAN_DOMINANT_FRACTION,
            "minimum_worldcover_built_fraction": MIN_HOUSEHOLD_BUILT_FRACTION,
            "minimum_extended_osm_urban_fraction": MIN_EXTENDED_HOUSEHOLD_URBAN_FRACTION,
            "minimum_extended_worldcover_built_fraction": MIN_EXTENDED_HOUSEHOLD_BUILT_FRACTION,
            "scope": "all cadastral parcels inside the Lower Hrazdan analytical outlines",
            "activity_required": False,
            "interpretation": "deterministic household-context candidate based on parcel size and urban masks",
            "previous_household_preserved_count": int(
                (
                    result["household_identification_source"].eq(
                        "previous_household_preserved"
                    )
                    & result["household_agriculture"]
                ).sum()
            ),
            "urban_size_rule_household_count": int(
                result["household_identification_source"]
                .eq("urban_size_rule")
                .sum()
            ),
            "extended_urban_built_rule_household_count": int(
                result["household_identification_source"]
                .eq("extended_urban_built_rule")
                .sum()
            ),
        },
        "road_exclusion": {
            **road_quality,
            "rule": (
                "exclude cadastral road footprints when mapped roads dominate the parcel, "
                "or when parcel shape and mapped road alignment jointly identify a road "
                "corridor; shape-only cases remain review candidates"
            ),
            "road_review_candidate_count": int(
                result["road_review_candidate"].fillna(False).sum()
            ),
        },
        "sources": {
            "cadastre": {"path": str(CADASTRE_SOURCE), "sha256": sha256(CADASTRE_SOURCE)},
            "land_review": {"path": str(LAND_REVIEW_SOURCE), "sha256": sha256(LAND_REVIEW_SOURCE)},
            "halos": {"path": str(HALO_SOURCE), "sha256": sha256(HALO_SOURCE)},
            "eo_grid": {"path": str(GRID_CACHE_SOURCE), "sha256": sha256(GRID_CACHE_SOURCE)},
            "eo_grid_metadata": {
                "path": str(GRID_METADATA_SOURCE),
                "sha256": sha256(GRID_METADATA_SOURCE),
            },
            "osm_roads": {"path": str(ROAD_SOURCE), "sha256": sha256(ROAD_SOURCE)},
        },
        "output": {
            "path": str(OUTPUT_PATH.relative_to(PRODUCT_ROOT)),
            "sha256": sha256(OUTPUT_PATH),
            "raster_sha256": {
                key: sha256(path) for key, path in raster_files.items()
            },
            "activity_class_raster_sha256": {
                activity_class: {
                    key: sha256(path) for key, path in class_files.items()
                }
                for activity_class, class_files in activity_class_raster_files.items()
            },
            "review_raster_sha256": {
                key: sha256(path) for key, path in review_raster_files.items()
            },
            "household_raster_sha256": {
                key: sha256(path) for key, path in household_raster_files.items()
            },
            "road_exclusions": {
                "path": str(ROAD_EXCLUSION_PATH.relative_to(PRODUCT_ROOT)),
                "sha256": sha256(ROAD_EXCLUSION_PATH),
            },
        },
    }
    QUALITY_PATH.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(quality, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
