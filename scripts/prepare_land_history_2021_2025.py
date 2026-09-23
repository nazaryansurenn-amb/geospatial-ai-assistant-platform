from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import from_origin


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
CADASTRE_SOURCE = PRODUCT_ROOT / "data" / "source" / "cadastre" / "parcels_wua.gpkg"
ACTIVITY_SOURCE = (
    PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview.json"
)
SEASONAL_DIR = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "v5_full_halo_2021_2025"
)
GRID_METADATA_SOURCE = PRODUCT_ROOT / "data" / "analysis" / "eo_grid_metadata_2026.json"
OUTPUT_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_history_2021_2025_preview.json"
)
QUALITY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_history_2021_2025_quality.json"
)
PUBLIC_RASTER_DIR = PRODUCT_ROOT / "public" / "data" / "review"

ANALYSIS_VERSION = "lower_hrazdan_land_history_v1_5_2021_2025"
YEARS = tuple(range(2021, 2026))
METRIC_CRS = "EPSG:32638"
EXPECTED_CURRENT_PARCELS = 43_984

CLASS_COLORS = {
    "stable_active": "#249b6b",
    "periodic": "#e2aa43",
    "stable_no_activity": "#8d9691",
    "insufficient": "#9b5de5",
}
HISTORY_CLASSES = (*CLASS_COLORS, "not_calculated")
CLASS_CODES = {name: index + 1 for index, name in enumerate(CLASS_COLORS)}
ANNUAL_STATE_CODES = {
    "insufficient": 0,
    "active": 1,
    "partial": 2,
    "no_activity": 3,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hex_color(value: str) -> tuple[int, int, int]:
    clean = value.lstrip("#")
    return tuple(int(clean[index : index + 2], 16) for index in (0, 2, 4))


def annual_states(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["cadastre_code"] = result["cadastre_code"].astype(str)
    result["reliable"] = result["feature_status"].eq("seasonal_profile")
    active = (
        result["reliable"]
        & result["ndvi_p90"].ge(0.40)
        & result["vegetation_observation_rate"].ge(0.40)
        & result["vegetation_fraction_max"].ge(0.30)
    )
    no_activity = (
        result["reliable"]
        & result["persistent_bare_observation_rate"].ge(0.60)
        & result["vegetation_observation_rate"].le(0.20)
    )
    result["annual_state"] = "insufficient"
    result.loc[result["reliable"], "annual_state"] = "partial"
    result.loc[active, "annual_state"] = "active"
    result.loc[no_activity, "annual_state"] = "no_activity"
    result["annual_state_code"] = result["annual_state"].map(ANNUAL_STATE_CODES).astype("uint8")
    return result


def classify_history(eligible: pd.DataFrame, annual: pd.DataFrame) -> pd.DataFrame:
    annual = annual[annual["cadastre_code"].isin(set(eligible["cadastre_code"]))].copy()
    annual["active_year"] = annual["annual_state"].eq("active")
    annual["partial_year"] = annual["annual_state"].eq("partial")
    annual["no_activity_year"] = annual["annual_state"].eq("no_activity")
    grouped = annual.groupby("cadastre_code", sort=False)
    aggregate = grouped.agg(
        source_year_count=("analysis_year", "nunique"),
        profile_year_count=("reliable", "sum"),
        active_year_count=("active_year", "sum"),
        partial_year_count=("partial_year", "sum"),
        no_activity_year_count=("no_activity_year", "sum"),
    )
    aggregate["used_year_count"] = (
        aggregate["active_year_count"] + aggregate["partial_year_count"]
    )
    aggregate["used_year_fraction"] = (
        aggregate["used_year_count"]
        / aggregate["profile_year_count"].replace(0, np.nan)
    )
    aggregate["no_activity_year_fraction"] = (
        aggregate["no_activity_year_count"]
        / aggregate["profile_year_count"].replace(0, np.nan)
    )

    result = eligible.merge(
        aggregate.reset_index(), on="cadastre_code", how="left", validate="1:1"
    )
    count_columns = [
        "profile_year_count",
        "source_year_count",
        "active_year_count",
        "partial_year_count",
        "no_activity_year_count",
        "used_year_count",
    ]
    result[count_columns] = result[count_columns].fillna(0).astype("int16")
    result["history_class"] = "not_calculated"
    result.loc[result["source_year_count"].gt(0), "history_class"] = "insufficient"

    stable_active = (
        result["profile_year_count"].ge(3)
        & result["used_year_count"].ge(3)
        & result["used_year_fraction"].ge(0.75)
    )
    periodic = (
        result["profile_year_count"].ge(3)
        & result["used_year_count"].ge(2)
        & ~stable_active
    )
    stable_no_activity = (
        result["profile_year_count"].ge(3)
        & result["used_year_count"].le(1)
        & result["no_activity_year_count"].ge(3)
        & result["no_activity_year_fraction"].ge(0.75)
    )
    result.loc[stable_active, "history_class"] = "stable_active"
    result.loc[periodic, "history_class"] = "periodic"
    result.loc[stable_no_activity, "history_class"] = "stable_no_activity"

    # A current unresolved land-review state remains unresolved in this first draft.
    result.loc[
        result["profile_year_count"].ge(3) & result["preview_state"].ne("ready"),
        "history_class",
    ] = "insufficient"

    annual_index = {
        (str(row.cadastre_code), int(row.analysis_year)): int(row.annual_state_code)
        for row in annual.itertuples(index=False)
    }
    result["annual_state_codes"] = result["cadastre_code"].map(
        lambda code: [annual_index.get((str(code), year), 0) for year in YEARS]
    )
    return result


def render_scope_rasters(
    frame: gpd.GeoDataFrame,
    *,
    scope: str,
    transform,
    width: int,
    height: int,
) -> dict[str, Path]:
    selected = frame
    if scope != "lower_hrazdan":
        selected = selected[selected["stage"].eq(scope)]
    selected = selected.sort_values("cadastre_code")
    labels = rasterize(
        (
            (row.geometry, CLASS_CODES[str(row.history_class)])
            for row in selected.itertuples(index=False)
            if row.geometry is not None and not row.geometry.is_empty
            and str(row.history_class) in CLASS_CODES
        ),
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=False,
    )
    PUBLIC_RASTER_DIR.mkdir(parents=True, exist_ok=True)
    paths = {}
    for history_class, class_code in CLASS_CODES.items():
        rgba = np.zeros((height, width, 4), dtype="uint8")
        visible = labels == class_code
        rgba[visible, :3] = hex_color(CLASS_COLORS[history_class])
        rgba[visible, 3] = 220
        path = PUBLIC_RASTER_DIR / f"land_history_2021_2025_{history_class}_{scope}.png"
        Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)
        paths[history_class] = path
    return paths


def scope_summary(frame: pd.DataFrame) -> dict[str, object]:
    class_counts = {
        key: int(value)
        for key, value in frame["history_class"].value_counts().reindex(
            HISTORY_CLASSES, fill_value=0
        ).items()
    }
    class_area_ha = {
        key: round(
            float(frame.loc[frame["history_class"].eq(key), "area_official_m2"].sum())
            / 10_000.0,
            2,
        )
        for key in HISTORY_CLASSES
    }
    return {
        "eligible_parcel_count": int(len(frame)),
        "processed_parcel_count": int(frame["source_year_count"].gt(0).sum()),
        "source_history_parcel_count": int(frame["profile_year_count"].gt(0).sum()),
        "without_reliable_history_count": int(frame["profile_year_count"].eq(0).sum()),
        "without_source_history_count": int(frame["source_year_count"].eq(0).sum()),
        "automatic_classified_count": int(
            frame["history_class"].isin(
                {"stable_active", "periodic", "stable_no_activity"}
            ).sum()
        ),
        "review_count": int(frame["history_class"].eq("insufficient").sum()),
        "class_counts": class_counts,
        "class_area_ha": class_area_ha,
    }


def build() -> dict[str, object]:
    required = [CADASTRE_SOURCE, ACTIVITY_SOURCE, GRID_METADATA_SOURCE]
    seasonal_paths = {
        year: SEASONAL_DIR / f"seasonal_features_{year}.parquet" for year in YEARS
    }
    required.extend(seasonal_paths.values())
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing history inputs: {missing}")

    activity = json.loads(ACTIVITY_SOURCE.read_text(encoding="utf-8"))
    activity_records = pd.DataFrame(activity["records"], columns=activity["record_schema"])
    if len(activity_records) != EXPECTED_CURRENT_PARCELS:
        raise ValueError(f"Unexpected current parcel count: {len(activity_records)}")
    if activity_records["cadastre_code"].astype(str).duplicated().any():
        raise ValueError("Current activity records contain duplicate cadastral codes")
    activity_records["cadastre_code"] = activity_records["cadastre_code"].astype(str)

    cadastre = gpd.read_file(
        CADASTRE_SOURCE,
        layer="parcels",
        columns=["cadastre_code", "area_official_m2", "geometry"],
    )
    cadastre["cadastre_code"] = cadastre["cadastre_code"].astype(str)
    current_codes = set(activity_records["cadastre_code"])
    cadastre = cadastre[cadastre["cadastre_code"].isin(current_codes)].copy()
    if len(cadastre) != EXPECTED_CURRENT_PARCELS:
        raise ValueError(f"Current cadastral geometry coverage is {len(cadastre)}")
    if cadastre.crs is None or cadastre.crs.to_epsg() != 4326:
        raise ValueError(f"Expected cadastral EPSG:4326, found {cadastre.crs}")

    current = cadastre.merge(
        activity_records[
            [
                "parcel_id",
                "cadastre_code",
                "stage",
                "preview_state",
                "household_agriculture",
                "road_excluded",
            ]
        ],
        on="cadastre_code",
        how="inner",
        validate="1:1",
    )
    eligible = current[
        ~current["household_agriculture"].astype(bool)
        & ~current["road_excluded"].astype(bool)
    ].copy()

    annual_frames = []
    source_validation = {}
    for year, path in seasonal_paths.items():
        frame = pd.read_parquet(path)
        if frame["cadastre_code"].astype(str).duplicated().any():
            raise ValueError(f"Seasonal features contain duplicate parcels for {year}")
        frame["analysis_year"] = year
        annual = annual_states(frame)
        annual_frames.append(annual)
        source_validation[str(year)] = {
            "row_count": int(len(frame)),
            "parcel_count": int(frame["cadastre_code"].nunique()),
            "feature_status_counts": {
                str(key): int(value)
                for key, value in frame["feature_status"].value_counts().items()
            },
        }
    annual = pd.concat(annual_frames, ignore_index=True)
    classified = classify_history(eligible, annual)
    classified = gpd.GeoDataFrame(classified, geometry="geometry", crs=cadastre.crs)

    grid = json.loads(GRID_METADATA_SOURCE.read_text(encoding="utf-8"))
    width = int(grid["width"])
    height = int(grid["height"])
    minx, miny, maxx, maxy = [float(value) for value in grid["bounds"]]
    transform = from_origin(minx, maxy, 10.0, 10.0)
    metric = classified.to_crs(METRIC_CRS)

    raster_urls: dict[str, dict[str, str]] = {key: {} for key in CLASS_COLORS}
    raster_hashes: dict[str, dict[str, str]] = {key: {} for key in CLASS_COLORS}
    for scope in ("lower_hrazdan", "stage_1", "stage_2"):
        scope_paths = render_scope_rasters(
            metric,
            scope=scope,
            transform=transform,
            width=width,
            height=height,
        )
        for history_class, path in scope_paths.items():
            raster_urls[history_class][scope] = f"/data/review/{path.name}"
            raster_hashes[history_class][scope] = sha256(path)

    summaries = {"lower_hrazdan": scope_summary(classified)}
    for stage in ("stage_1", "stage_2"):
        summaries[stage] = scope_summary(classified[classified["stage"].eq(stage)])

    records = [
        [
            int(row.parcel_id),
            str(row.cadastre_code),
            str(row.stage),
            str(row.history_class),
            list(row.annual_state_codes),
            int(row.profile_year_count),
            int(row.used_year_count),
            int(row.source_year_count),
        ]
        for row in classified.itertuples(index=False)
    ]
    payload = {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "draft_owner_review",
        "period": [2021, 2025],
        "spatial_unit": "immutable_cadastral_parcel",
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
        "eligible_rule": "current Lower Hrazdan analytical parcels excluding household and road classes",
        "class_colors": CLASS_COLORS,
        "annual_state_codes": ANNUAL_STATE_CODES,
        "record_schema": [
            "parcel_id",
            "cadastre_code",
            "stage",
            "history_class",
            "annual_state_codes",
            "profile_year_count",
            "used_year_count",
            "source_year_count",
        ],
        "records": records,
        "summaries": summaries,
        "class_rasters": raster_urls,
        "raster_coordinates": activity["raster_coordinates"],
        "limitations": [
            "Every current parcel in the approved analytical extent was processed for every season.",
            "A missing or limited season is never converted into no activity.",
            "Sub-pixel parcels retain fractional indicators and remain a separate review class.",
            "Classes are draft analytical screening results pending owner visual review.",
        ],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    quality = {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": payload["generated_at_utc"],
        "status": payload["status"],
        "public_release_approved": False,
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "current_parcel_count": int(len(current)),
        "eligible_parcel_count": int(len(classified)),
        "excluded_household_count": int(current["household_agriculture"].sum()),
        "excluded_road_count": int(current["road_excluded"].sum()),
        "source_validation": source_validation,
        "summaries": summaries,
        "rules": {
            "annual_active": "seasonal profile with compatible NDVI and vegetation-frequency signals",
            "annual_no_activity": "seasonal profile with persistent bare and low vegetation-frequency signals",
            "stable_active": "at least three observed used seasons and used fraction >= 0.75",
            "periodic": "at least two observed used seasons without stable-active pattern",
            "stable_no_activity": "at least three no-activity seasons, used seasons <= 1, and no-activity fraction >= 0.75",
            "insufficient": "the parcel was processed but reliable multi-year classification is prevented by review state or insufficient spatial support",
            "not_calculated": "no parcel Copernicus seasonal record exists in the current history package",
        },
        "sources": {
            "cadastre": {"path": str(CADASTRE_SOURCE), "sha256": sha256(CADASTRE_SOURCE)},
            "activity": {"path": str(ACTIVITY_SOURCE), "sha256": sha256(ACTIVITY_SOURCE)},
            "grid_metadata": {
                "path": str(GRID_METADATA_SOURCE),
                "sha256": sha256(GRID_METADATA_SOURCE),
            },
            "seasonal_features": {
                str(year): {"path": str(path), "sha256": sha256(path)}
                for year, path in seasonal_paths.items()
            },
        },
        "outputs": {
            "preview": {"path": str(OUTPUT_PATH), "sha256": sha256(OUTPUT_PATH)},
            "raster_sha256": raster_hashes,
        },
    }
    QUALITY_PATH.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return quality


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
