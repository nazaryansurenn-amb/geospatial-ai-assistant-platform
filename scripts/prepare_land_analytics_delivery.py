from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import mapbox_vector_tile
import pandas as pd
import shapely
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
from pyproj import Transformer
from shapely.geometry import box


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_PARCELS_PATH = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "current_halo_v1"
    / "current_parcels.parquet"
)
CURRENT_PARCELS_QUALITY_PATH = CURRENT_PARCELS_PATH.with_name(
    "current_parcels_quality.json"
)
ACTIVITY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview.json"
)
HISTORY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_history_2021_2025_preview.json"
)
CROP_TYPE_V1_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_2021_2025_preview.json"
)
CROP_TYPE_V2_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_2021_2025_preview.json"
)
ANNUAL_CYCLES_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "annual_cycles_v1_2021_2025_preview.json"
)

DELIVERY_PROFILE = os.getenv("LAND_ANALYTICS_PROFILE", "v2")
if DELIVERY_PROFILE == "use_type_v2":
    DELIVERY_VERSION = "lower_hrazdan_land_analytics_mvt_v3_2026_09_05"
    DELIVERY_SLUG = "v3_2026_09_05"
    CROP_TYPE_PATH = CROP_TYPE_V2_PATH
    CYCLE_PATH: Path | None = ANNUAL_CYCLES_PATH
else:
    DELIVERY_VERSION = "lower_hrazdan_land_analytics_mvt_v2_2026_09_05"
    DELIVERY_SLUG = "v2_2026_09_05"
    CROP_TYPE_PATH = CROP_TYPE_V1_PATH
    CYCLE_PATH = None
requested_slug = os.getenv("LAND_ANALYTICS_DELIVERY_SLUG")
if requested_slug:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", requested_slug):
        raise ValueError("Invalid analytical delivery slug")
    DELIVERY_SLUG = requested_slug
    DELIVERY_VERSION = f"lower_hrazdan_land_analytics_mvt_{DELIVERY_SLUG}"
PUBLIC_ROOT = PRODUCT_ROOT / "public" / "data" / "land_analytics"
TILE_OUTPUT = PUBLIC_ROOT / DELIVERY_SLUG
MANIFEST_PATH = PUBLIC_ROOT / (
    "manifest_v3_2026_09_05.json" if CYCLE_PATH else "manifest.json"
)
SUMMARY_PATH = PRODUCT_ROOT / "server_data" / (
    "land_analytics_summary_v3_2026_09_05.json"
    if CYCLE_PATH
    else "land_analytics_summary.json"
)
INDEX_PATH = PRODUCT_ROOT / "server_data" / f"land_analytics_{DELIVERY_SLUG}.sqlite3"
if requested_slug:
    MANIFEST_PATH = PUBLIC_ROOT / f"manifest_{DELIVERY_SLUG}.json"
    SUMMARY_PATH = PRODUCT_ROOT / "server_data" / f"land_analytics_summary_{DELIVERY_SLUG}.json"
VERSION_ARCHIVE_ROOT = (
    PRODUCT_ROOT / "server_data" / "review" / "versions" / "land_analytics_delivery"
)

EXPECTED_PARCELS = 43_984
EXPECTED_HISTORY_PARCELS = 22_642 if DELIVERY_PROFILE == "use_type_v2" else 22_802
EXPECTED_CROP_TYPE_PARCELS = 22_642 if DELIVERY_PROFILE == "use_type_v2" else 18_303
MIN_ZOOM = 10
MAX_ZOOM = 15
EXPECTED_TILE_COUNTS = {10: 4, 11: 5, 12: 10, 13: 27, 14: 77, 15: 247}
MVT_EXTENT = 4096
WEB_MERCATOR_LIMIT = 20_037_508.342789244
LAYER_NAME = "land_analytics"
TILE_PROPERTIES = [
    "stage",
    "activity_class",
    "activity_fraction_bp",
    "activity_state",
    "household",
    "road_excluded",
    "history_class",
    "crop_type",
    "annual_cycle",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tile_width_m(zoom: int) -> float:
    return 2 * WEB_MERCATOR_LIMIT / (2**zoom)


def tile_bounds_3857(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    width = tile_width_m(zoom)
    minx = -WEB_MERCATOR_LIMIT + x * width
    maxx = minx + width
    maxy = WEB_MERCATOR_LIMIT - y * width
    miny = maxy - width
    return minx, miny, maxx, maxy


def tile_range(
    bounds: tuple[float, float, float, float], zoom: int
) -> tuple[range, range]:
    minx, miny, maxx, maxy = bounds
    width = tile_width_m(zoom)
    limit = 2**zoom - 1
    x0 = max(0, math.floor((minx + WEB_MERCATOR_LIMIT) / width))
    x1 = min(limit, math.floor((maxx + WEB_MERCATOR_LIMIT) / width))
    y0 = max(0, math.floor((WEB_MERCATOR_LIMIT - maxy) / width))
    y1 = min(limit, math.floor((WEB_MERCATOR_LIMIT - miny) / width))
    return range(x0, x1 + 1), range(y0, y1 + 1)


def _records(payload: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(payload["records"], columns=payload["record_schema"])


def load_delivery_frame() -> tuple[gpd.GeoDataFrame, dict, dict, dict, dict | None, dict]:
    required = [
        CURRENT_PARCELS_PATH,
        CURRENT_PARCELS_QUALITY_PATH,
        ACTIVITY_PATH,
        HISTORY_PATH,
        CROP_TYPE_PATH,
    ]
    if CYCLE_PATH:
        required.append(CYCLE_PATH)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing land delivery inputs: {missing}")

    geometry_quality = json.loads(
        CURRENT_PARCELS_QUALITY_PATH.read_text(encoding="utf-8")
    )
    activity = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    crop_type = json.loads(CROP_TYPE_PATH.read_text(encoding="utf-8"))
    cycles = json.loads(CYCLE_PATH.read_text(encoding="utf-8")) if CYCLE_PATH else None
    parcels = gpd.read_parquet(CURRENT_PARCELS_PATH)

    if len(parcels) != EXPECTED_PARCELS:
        raise ValueError(f"Expected {EXPECTED_PARCELS} parcels, found {len(parcels)}")
    if parcels.crs is None or parcels.crs.to_epsg() != 4326:
        raise ValueError(f"Expected parcel EPSG:4326, found {parcels.crs}")
    if parcels["cadastre_code"].isna().any() or parcels["cadastre_code"].duplicated().any():
        raise ValueError("Current parcels require complete unique cadastral codes")
    if parcels.geometry.isna().any() or parcels.geometry.is_empty.any():
        raise ValueError("Current parcels contain missing geometry")
    if not parcels.geometry.is_valid.all():
        raise ValueError("Current parcels contain invalid geometry")

    activity_records = _records(activity)
    history_records = _records(history)
    crop_type_records = _records(crop_type)
    cycle_records = _records(cycles) if cycles else pd.DataFrame()
    activity_records["cadastre_code"] = activity_records["cadastre_code"].astype(str)
    history_records["cadastre_code"] = history_records["cadastre_code"].astype(str)
    crop_type_records["cadastre_code"] = crop_type_records["cadastre_code"].astype(str)
    if cycles:
        cycle_records["cadastre_code"] = cycle_records["cadastre_code"].astype(str)
    if len(activity_records) != EXPECTED_PARCELS:
        raise ValueError(f"Unexpected activity coverage: {len(activity_records)}")
    if len(history_records) != EXPECTED_HISTORY_PARCELS:
        raise ValueError(f"Unexpected history coverage: {len(history_records)}")
    if len(crop_type_records) != EXPECTED_CROP_TYPE_PARCELS:
        raise ValueError(f"Unexpected crop-type coverage: {len(crop_type_records)}")
    if activity_records["cadastre_code"].duplicated().any():
        raise ValueError("Activity records contain duplicate cadastral codes")
    if history_records["cadastre_code"].duplicated().any():
        raise ValueError("History records contain duplicate cadastral codes")
    if crop_type_records["cadastre_code"].duplicated().any():
        raise ValueError("Crop-type records contain duplicate cadastral codes")
    if cycles and cycle_records["cadastre_code"].duplicated().any():
        raise ValueError("Annual-cycle records contain duplicate cadastral codes")

    activity_columns = [
        "parcel_id",
        "cadastre_code",
        "stage",
        "activity_fraction",
        "observed_active_area_ha",
        "preview_state",
        "household_agriculture",
        "activity_class",
        "road_excluded",
    ]
    frame = parcels[
        [
            "public_parcel_id",
            "cadastre_code",
            "area_official_m2",
            "stage",
            "preview_state",
            "household_agriculture",
            "road_excluded",
            "geometry",
        ]
    ].merge(
        activity_records[activity_columns],
        on="cadastre_code",
        how="inner",
        validate="1:1",
        suffixes=("_geometry", "_activity"),
    )
    if len(frame) != EXPECTED_PARCELS:
        raise ValueError("Activity records do not cover the immutable parcel geometry")
    if not (
        frame["public_parcel_id"].astype("int64")
        == frame["parcel_id"].astype("int64")
    ).all():
        raise ValueError("Public parcel identity differs between geometry and activity")
    for field in ("stage", "preview_state", "household_agriculture", "road_excluded"):
        if not (frame[f"{field}_geometry"] == frame[f"{field}_activity"]).all():
            raise ValueError(f"Current parcel context differs for {field}")

    history_columns = [
        "cadastre_code",
        "history_class",
        "annual_state_codes",
        "profile_year_count",
        "used_year_count",
        "source_year_count",
    ]
    frame = frame.merge(
        history_records[history_columns],
        on="cadastre_code",
        how="left",
        validate="1:1",
    )
    frame["history_class"] = frame["history_class"].fillna("").astype(str)
    for field in ("profile_year_count", "used_year_count", "source_year_count"):
        frame[field] = frame[field].fillna(0).astype("int16")
    frame["annual_state_codes"] = frame["annual_state_codes"].map(
        lambda value: value if isinstance(value, list) else []
    )
    crop_type_columns = [
        "parcel_id",
        "cadastre_code",
        "stage",
        "crop_type_candidate",
        "profile_year_count",
    ]
    if "crop_type_year_codes" in crop_type_records.columns:
        crop_type_columns.append("crop_type_year_codes")
    crop_type_delivery = crop_type_records[crop_type_columns].rename(
        columns={
            "parcel_id": "parcel_id_crop_type",
            "stage": "stage_crop_type",
            "profile_year_count": "profile_year_count_crop_type",
        }
    )
    frame = frame.merge(
        crop_type_delivery,
        on="cadastre_code",
        how="left",
        validate="1:1",
    )
    matched_crop_type = frame["crop_type_candidate"].notna()
    if not (
        frame.loc[matched_crop_type, "public_parcel_id"].astype("int64")
        == frame.loc[matched_crop_type, "parcel_id_crop_type"].astype("int64")
    ).all():
        raise ValueError("Public parcel identity differs for crop-type records")
    if not (
        frame.loc[matched_crop_type, "stage_activity"]
        == frame.loc[matched_crop_type, "stage_crop_type"]
    ).all():
        raise ValueError("Crop-type stage differs from current parcel context")
    frame["crop_type_candidate"] = frame["crop_type_candidate"].fillna("").astype(str)
    frame["crop_profile_year_count"] = (
        frame.pop("profile_year_count_crop_type").fillna(0).astype("int16")
    )
    if "crop_type_year_codes" not in frame.columns:
        frame["crop_type_year_codes"] = [[] for _ in range(len(frame))]
    frame["crop_type_year_codes"] = frame["crop_type_year_codes"].map(
        lambda value: value if isinstance(value, list) else []
    )
    frame = frame.drop(columns=["parcel_id_crop_type", "stage_crop_type"])
    if cycles:
        cycle_columns = [
            "parcel_id",
            "cadastre_code",
            "stage",
            "annual_cycle",
            "assessed_year_count",
            "annual_cycle_year_codes",
        ]
        cycle_delivery = cycle_records[cycle_columns].rename(
            columns={
                "parcel_id": "parcel_id_cycle",
                "stage": "stage_cycle",
            }
        )
        frame = frame.merge(cycle_delivery, on="cadastre_code", how="left", validate="1:1")
        matched_cycles = frame["annual_cycle"].notna()
        if not (
            frame.loc[matched_cycles, "public_parcel_id"].astype("int64")
            == frame.loc[matched_cycles, "parcel_id_cycle"].astype("int64")
        ).all():
            raise ValueError("Public parcel identity differs for annual-cycle records")
        if not (
            frame.loc[matched_cycles, "stage_activity"]
            == frame.loc[matched_cycles, "stage_cycle"]
        ).all():
            raise ValueError("Annual-cycle stage differs from current parcel context")
        frame = frame.drop(columns=["parcel_id_cycle", "stage_cycle"])
    else:
        frame["annual_cycle"] = ""
        frame["assessed_year_count"] = 0
        frame["annual_cycle_year_codes"] = [[] for _ in range(len(frame))]
    frame["annual_cycle"] = frame["annual_cycle"].fillna("").astype(str)
    frame["assessed_year_count"] = frame["assessed_year_count"].fillna(0).astype("int16")
    frame["annual_cycle_year_codes"] = frame["annual_cycle_year_codes"].map(
        lambda value: value if isinstance(value, list) else []
    )
    frame["stage"] = frame.pop("stage_activity").astype(str)
    frame["preview_state"] = frame.pop("preview_state_activity").astype(str)
    frame["household_agriculture"] = frame.pop(
        "household_agriculture_activity"
    ).astype(bool)
    frame["road_excluded"] = frame.pop("road_excluded_activity").astype(bool)
    frame["activity_fraction"] = frame["activity_fraction"].astype(float)
    frame["observed_active_area_ha"] = frame["observed_active_area_ha"].astype(float)
    frame["activity_class"] = frame["activity_class"].astype(str)
    frame = gpd.GeoDataFrame(frame, geometry="geometry", crs=parcels.crs)
    frame = frame.sort_values("cadastre_code").reset_index(drop=True)
    return frame.to_crs("EPSG:3857"), activity, history, crop_type, cycles, geometry_quality


def tile_properties(row) -> dict[str, object]:
    return {
        "stage": str(row.stage),
        "activity_class": str(row.activity_class),
        "activity_fraction_bp": int(round(float(row.activity_fraction) * 10_000)),
        "activity_state": str(row.preview_state),
        "household": bool(row.household_agriculture),
        "road_excluded": bool(row.road_excluded),
        "history_class": str(row.history_class),
        "crop_type": str(row.crop_type_candidate),
        "annual_cycle": str(row.annual_cycle),
    }


def encode_tile(
    frame: gpd.GeoDataFrame,
    spatial_index,
    zoom: int,
    x: int,
    y: int,
) -> bytes | None:
    bounds = tile_bounds_3857(zoom, x, y)
    indexes = spatial_index.query(box(*bounds), predicate="intersects")
    if not len(indexes):
        return None

    candidates = frame.iloc[indexes]
    buffer_m = tile_width_m(zoom) * 8 / MVT_EXTENT
    clip_box = box(
        bounds[0] - buffer_m,
        bounds[1] - buffer_m,
        bounds[2] + buffer_m,
        bounds[3] + buffer_m,
    )
    geometries = shapely.intersection(candidates.geometry.values, clip_box)
    # Low-zoom display geometry is generalized only inside the delivery tiles.
    tolerance = {10: 30.0, 11: 15.0, 12: 7.5, 13: 3.0}.get(zoom, 0.0)
    if tolerance:
        geometries = shapely.simplify(
            geometries, tolerance=tolerance, preserve_topology=True
        )

    features = []
    for row, geometry in zip(candidates.itertuples(index=False), geometries, strict=True):
        if geometry is None or geometry.is_empty:
            continue
        features.append(
            {
                "id": int(row.public_parcel_id),
                "geometry": geometry,
                "properties": tile_properties(row),
            }
        )
    if not features:
        return None

    return mapbox_vector_tile.encode(
        {"name": LAYER_NAME, "features": features},
        default_options={
            "quantize_bounds": bounds,
            "extents": MVT_EXTENT,
            "on_invalid_geometry": on_invalid_geometry_make_valid,
        },
    )


def build_tiles(frame: gpd.GeoDataFrame, build_id: str) -> dict[str, object]:
    if TILE_OUTPUT.exists():
        raise FileExistsError(
            f"Versioned tile output already exists and will not be overwritten: {TILE_OUTPUT}"
        )
    build_dir = PUBLIC_ROOT / f".{DELIVERY_SLUG}.building-{build_id}"
    build_dir.mkdir(parents=True, exist_ok=False)
    bounds_3857 = tuple(float(value) for value in frame.total_bounds)
    spatial_index = frame.sindex
    tile_counts: dict[str, int] = {}
    tile_bytes: dict[str, int] = {}

    for zoom in range(MIN_ZOOM, MAX_ZOOM + 1):
        x_range, y_range = tile_range(bounds_3857, zoom)
        count = 0
        byte_count = 0
        for x in x_range:
            for y in y_range:
                payload = encode_tile(frame, spatial_index, zoom, x, y)
                if not payload:
                    continue
                target = build_dir / str(zoom) / str(x) / f"{y}.pbf"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                count += 1
                byte_count += len(payload)
        tile_counts[str(zoom)] = count
        tile_bytes[str(zoom)] = byte_count
        print(f"zoom {zoom}: {count} tiles, {byte_count / (1024**2):.2f} MiB")
    build_dir.rename(TILE_OUTPUT)

    to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    west, south = to_wgs84.transform(bounds_3857[0], bounds_3857[1])
    east, north = to_wgs84.transform(bounds_3857[2], bounds_3857[3])
    return {
        "tile_counts": tile_counts,
        "tile_bytes": tile_bytes,
        "total_tiles": sum(tile_counts.values()),
        "total_bytes": sum(tile_bytes.values()),
        "bounds_wgs84": [west, south, east, north],
    }


def inspect_existing_tiles(frame: gpd.GeoDataFrame) -> dict[str, object]:
    tile_counts: dict[str, int] = {}
    tile_bytes: dict[str, int] = {}
    for zoom in range(MIN_ZOOM, MAX_ZOOM + 1):
        files = list((TILE_OUTPUT / str(zoom)).rglob("*.pbf"))
        if len(files) != EXPECTED_TILE_COUNTS[zoom]:
            raise ValueError(
                f"Incomplete existing tile level {zoom}: {len(files)} files"
            )
        tile_counts[str(zoom)] = len(files)
        tile_bytes[str(zoom)] = sum(path.stat().st_size for path in files)

    bounds_3857 = tuple(float(value) for value in frame.total_bounds)
    to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    west, south = to_wgs84.transform(bounds_3857[0], bounds_3857[1])
    east, north = to_wgs84.transform(bounds_3857[2], bounds_3857[3])
    return {
        "tile_counts": tile_counts,
        "tile_bytes": tile_bytes,
        "total_tiles": sum(tile_counts.values()),
        "total_bytes": sum(tile_bytes.values()),
        "bounds_wgs84": [west, south, east, north],
    }


def _archive_existing(build_id: str) -> None:
    existing = [path for path in (MANIFEST_PATH, SUMMARY_PATH, INDEX_PATH) if path.exists()]
    if not existing:
        return
    archive = VERSION_ARCHIVE_ROOT / build_id
    archive.mkdir(parents=True, exist_ok=False)
    for path in existing:
        shutil.copy2(path, archive / path.name)


def build_index(frame: gpd.GeoDataFrame) -> None:
    temporary = INDEX_PATH.with_suffix(".building.sqlite3")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE parcel_analytics (
                cadastre_code TEXT PRIMARY KEY,
                activity_stage TEXT NOT NULL,
                activity_fraction REAL NOT NULL,
                observed_active_area_ha REAL NOT NULL,
                activity_state TEXT NOT NULL,
                household INTEGER NOT NULL,
                activity_class TEXT NOT NULL,
                road_excluded INTEGER NOT NULL,
                history_class TEXT,
                annual_state_codes TEXT,
                profile_year_count INTEGER,
                used_year_count INTEGER,
                source_year_count INTEGER,
                crop_type TEXT,
                crop_profile_year_count INTEGER,
                crop_type_year_codes TEXT,
                annual_cycle TEXT,
                annual_cycle_year_codes TEXT,
                cycle_assessed_year_count INTEGER
            ) WITHOUT ROWID;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        rows = (
            (
                str(row.cadastre_code),
                str(row.stage),
                float(row.activity_fraction),
                float(row.observed_active_area_ha),
                str(row.preview_state),
                int(bool(row.household_agriculture)),
                str(row.activity_class),
                int(bool(row.road_excluded)),
                str(row.history_class) or None,
                json.dumps(list(row.annual_state_codes), separators=(",", ":"))
                if row.history_class
                else None,
                int(row.profile_year_count) if row.history_class else None,
                int(row.used_year_count) if row.history_class else None,
                int(row.source_year_count) if row.history_class else None,
                str(row.crop_type_candidate) or None,
                int(row.crop_profile_year_count) if row.crop_type_candidate else None,
                json.dumps(list(row.crop_type_year_codes), separators=(",", ":"))
                if row.crop_type_candidate
                else None,
                str(row.annual_cycle) or None,
                json.dumps(list(row.annual_cycle_year_codes), separators=(",", ":"))
                if row.annual_cycle
                else None,
                int(row.assessed_year_count) if row.annual_cycle else None,
            )
            for row in frame.itertuples(index=False)
        )
        connection.executemany(
            "INSERT INTO parcel_analytics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("delivery_version", DELIVERY_VERSION),
                ("parcel_count", str(len(frame))),
                ("history_parcel_count", str(int(frame["history_class"].ne("").sum()))),
                ("crop_type_parcel_count", str(int(frame["crop_type_candidate"].ne("").sum()))),
                ("annual_cycle_parcel_count", str(int(frame["annual_cycle"].ne("").sum()))),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, INDEX_PATH)


def compact_activity(payload: dict[str, object]) -> dict[str, object]:
    excluded = {
        "records",
        "record_schema",
        "rasters",
        "activity_class_rasters",
        "review_rasters",
        "household_rasters",
        "raster_coordinates",
    }
    return {key: value for key, value in payload.items() if key not in excluded}


def compact_history(payload: dict[str, object]) -> dict[str, object]:
    excluded = {"records", "record_schema", "class_rasters", "raster_coordinates"}
    return {key: value for key, value in payload.items() if key not in excluded}


def compact_crop_type(payload: dict[str, object]) -> dict[str, object]:
    excluded = {"records", "record_schema"}
    return {key: value for key, value in payload.items() if key not in excluded}


def compact_cycles(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    excluded = {"records", "record_schema"}
    return {key: value for key, value in payload.items() if key not in excluded}


def build() -> dict[str, object]:
    existing = [path for path in (TILE_OUTPUT, MANIFEST_PATH, SUMMARY_PATH, INDEX_PATH)
                if path.exists()]
    if existing:
        raise FileExistsError(
            "Delivery already exists. Set a new LAND_ANALYTICS_DELIVERY_SLUG; "
            "existing tiles, summaries and parcel indexes cannot be reused or overwritten."
        )
    frame, activity, history, crop_type, cycles, geometry_quality = load_delivery_frame()
    build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tile_metrics = build_tiles(frame, build_id)
    _archive_existing(build_id)
    build_index(frame)

    manifest = {
        "schema_version": 1,
        "status": "draft_owner_review",
        "delivery_version": DELIVERY_VERSION,
        "build_id": build_id,
        "format": "Mapbox Vector Tile 2.1",
        "layer_name": LAYER_NAME,
        "tile_url": f"/data/land_analytics/{DELIVERY_SLUG}/{{z}}/{{x}}/{{y}}.pbf",
        "min_zoom": MIN_ZOOM,
        "max_zoom": MAX_ZOOM,
        "feature_count": int(len(frame)),
        "history_feature_count": int(frame["history_class"].ne("").sum()),
        "crop_type_feature_count": int(frame["crop_type_candidate"].ne("").sum()),
        "annual_cycle_feature_count": int(frame["annual_cycle"].ne("").sum()),
        "properties": TILE_PROPERTIES,
        "canonical_geometry_version": geometry_quality["canonical_geometry_version"],
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
        "activity_analysis_version": activity["analysis_version"],
        "history_analysis_version": history["analysis_version"],
        "crop_type_analysis_version": crop_type["analysis_version"],
        "annual_cycle_analysis_version": cycles["analysis_version"] if cycles else None,
        "tile_delivery": tile_metrics,
        "privacy_note": (
            "Delivery tiles contain public derived classes only and exclude internal parcel IDs, "
            "source records, confidence fields, thresholds, prompts, and tool traces."
        ),
        "sources": {
            "current_parcels": {
                "path": str(CURRENT_PARCELS_PATH.relative_to(PRODUCT_ROOT)),
                "sha256": sha256(CURRENT_PARCELS_PATH),
            },
            "activity": {
                "path": str(ACTIVITY_PATH.relative_to(PRODUCT_ROOT)),
                "sha256": sha256(ACTIVITY_PATH),
            },
            "history": {
                "path": str(HISTORY_PATH.relative_to(PRODUCT_ROOT)),
                "sha256": sha256(HISTORY_PATH),
            },
            "crop_type": {
                "path": str(CROP_TYPE_PATH.relative_to(PRODUCT_ROOT)),
                "sha256": sha256(CROP_TYPE_PATH),
            },
        },
    }
    if CYCLE_PATH:
        manifest["sources"]["annual_cycles"] = {
            "path": str(CYCLE_PATH.relative_to(PRODUCT_ROOT)),
            "sha256": sha256(CYCLE_PATH),
        }
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "schema_version": 1,
        "delivery_version": DELIVERY_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tile_delivery": {
            "url": manifest["tile_url"],
            "layer_name": LAYER_NAME,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "bounds": tile_metrics["bounds_wgs84"],
        },
        "activity": compact_activity(activity),
        "history": compact_history(history),
        "land_use_type": compact_crop_type(crop_type),
        "annual_cycles": compact_cycles(cycles),
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    manifest["summary_sha256"] = sha256(SUMMARY_PATH)
    manifest["index_sha256"] = sha256(INDEX_PATH)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
