from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_PARCELS_PATH = (
    PRODUCT_ROOT / "data" / "analysis" / "parcel_eo" / "current_halo_v1" / "current_parcels.parquet"
)
EO_DIR = PRODUCT_ROOT / "data" / "analysis" / "parcel_eo" / "v5_full_halo_2021_2025"

TYPE_OUTPUT_PATH = PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_2021_2025_preview.json"
TYPE_FEATURE_PATH = PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_2021_2025_features.parquet"
CYCLE_OUTPUT_PATH = PRODUCT_ROOT / "server_data" / "review" / "annual_cycles_v1_2021_2025_preview.json"
CYCLE_FEATURE_PATH = PRODUCT_ROOT / "server_data" / "review" / "annual_cycles_v1_2021_2025_features.parquet"
QUALITY_PATH = PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_2021_2025_quality.json"
REVIEW_SAMPLE_PATH = PRODUCT_ROOT / "server_data" / "review" / "land_use_type_v2_validation_sample.json"

TYPE_ANALYSIS_VERSION = "lower_hrazdan_land_use_type_v2_2_2021_2025"
CYCLE_ANALYSIS_VERSION = "lower_hrazdan_annual_cycles_v1_2_2021_2025"
YEARS = tuple(range(2021, 2026))
EXPECTED_PARCELS = 43_984
EXPECTED_ELIGIBLE = 22_642

TYPE_COLORS = {
    "annual": "#e7bd46",
    "perennial": "#1fa276",
    "mixed_profile": "#567ea6",
    "insufficient_observations": "#8d9691",
}
CYCLE_COLORS = {
    "single_cycle": "#e7bd46",
    "two_cycle_recurring": "#e8752e",
    "variable_cycle": "#3f7fb5",
    "insufficient_observations": "#8d9691",
}
TYPE_STATE_CODES = {
    "insufficient": 0,
    "annual": 1,
    "perennial": 2,
    "mixed": 3,
}
CYCLE_STATE_CODES = {"insufficient": 0, "single": 1, "double": 2}

MIN_TYPE_PROFILE_YEARS = 3
MIN_TYPE_DOMINANT_YEARS = 3
MIN_YEAR_SIGNAL_COUNT = 5
MIN_YEAR_SIGNAL_MARGIN = 2

PERENNIAL_NDMI_MEAN_MIN = 0.12
PERENNIAL_NDMI_MIN_MIN = 0.02
PERENNIAL_BSI_MEAN_MAX = -0.03
ANNUAL_NDMI_MEAN_MAX = 0.08
ANNUAL_NDMI_MIN_MAX = -0.02
ANNUAL_BSI_MEAN_MIN = 0.00

MIN_CYCLE_VALID_OBSERVATIONS = 8
MIN_CYCLE_ASSESSABLE_YEARS = 3
MIN_RECURRING_DOUBLE_YEARS = 2
MIN_WINDOW_OBSERVATIONS = 2
EARLY_WINDOW_END_DAY = 151
MIDDLE_WINDOW_END_DAY = 212
EARLY_PEAK_END_DAY = 201
LATE_PEAK_START_DAY = 213
MIN_PEAK_SEPARATION_DAYS = 40
MIN_PEAK_NDVI = 0.45
MIN_PEAK_VEGETATION_FRACTION = 0.35
MIN_DROP = 0.18
MIN_REBOUND = 0.18
MAX_TROUGH_NDVI = 0.38
MIN_TROUGH_BARE_FRACTION = 0.12


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perennial_signal_count(frame: pd.DataFrame) -> pd.Series:
    signals = pd.concat(
        [
            frame["ndvi_p10"].ge(0.38),
            frame["ndvi_p25"].ge(0.45),
            frame["vegetation_fraction_mean"].ge(0.60),
            frame["vegetation_observation_rate"].ge(0.80),
            frame["ndvi_amplitude"].le(0.40),
            frame["persistent_bare_observation_rate"].le(0.10),
            frame["ndvi_late_mean"].ge(0.50),
            frame["ndmi_mean"].ge(PERENNIAL_NDMI_MEAN_MIN),
            frame["ndmi_min"].ge(PERENNIAL_NDMI_MIN_MIN),
            frame["bsi_mean"].le(PERENNIAL_BSI_MEAN_MAX),
        ],
        axis=1,
    )
    return signals.sum(axis=1).astype("uint8")


def annual_signal_count(frame: pd.DataFrame) -> pd.Series:
    signals = pd.concat(
        [
            frame["ndvi_amplitude"].ge(0.48),
            frame["ndvi_p10"].le(0.30),
            frame["ndvi_p25"].le(0.36),
            frame["vegetation_fraction_mean"].le(0.50),
            frame["vegetation_observation_rate"].le(0.70),
            frame["persistent_bare_observation_rate"].ge(0.10),
            (frame["ndvi_p90"] - frame["ndvi_p25"]).ge(0.35),
            frame["ndmi_mean"].le(ANNUAL_NDMI_MEAN_MAX),
            frame["ndmi_min"].le(ANNUAL_NDMI_MIN_MAX),
            frame["bsi_mean"].ge(ANNUAL_BSI_MEAN_MIN),
        ],
        axis=1,
    )
    return signals.sum(axis=1).astype("uint8")


def classify_yearly_type(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["annual_signal_count"] = annual_signal_count(result)
    result["perennial_signal_count"] = perennial_signal_count(result)
    result["year_type"] = "mixed"
    insufficient = ~result["feature_status"].eq("seasonal_profile")
    annual = (
        result["annual_signal_count"].ge(MIN_YEAR_SIGNAL_COUNT)
        & (result["annual_signal_count"] - result["perennial_signal_count"]).ge(
            MIN_YEAR_SIGNAL_MARGIN
        )
    )
    perennial = (
        result["perennial_signal_count"].ge(MIN_YEAR_SIGNAL_COUNT)
        & (result["perennial_signal_count"] - result["annual_signal_count"]).ge(
            MIN_YEAR_SIGNAL_MARGIN
        )
    )
    result.loc[annual, "year_type"] = "annual"
    result.loc[perennial, "year_type"] = "perennial"
    result.loc[insufficient, "year_type"] = "insufficient"
    return result


def evaluate_cycle_year(group: pd.DataFrame) -> dict[str, object]:
    observations = group[
        group["quality_status"].eq("usable")
        & group["valid_fraction"].ge(0.35)
        & group["ndvi_mean"].notna()
    ].copy()
    observations = observations.sort_values("observation_date")
    observations["day_of_year"] = observations["observation_date"].dt.dayofyear
    window_counts = (
        int(observations["day_of_year"].le(EARLY_WINDOW_END_DAY).sum()),
        int(
            observations["day_of_year"]
            .between(EARLY_WINDOW_END_DAY + 1, MIDDLE_WINDOW_END_DAY)
            .sum()
        ),
        int(observations["day_of_year"].gt(MIDDLE_WINDOW_END_DAY).sum()),
    )
    assessable = len(observations) >= MIN_CYCLE_VALID_OBSERVATIONS and all(
        count >= MIN_WINDOW_OBSERVATIONS for count in window_counts
    )
    if not assessable:
        return {
            "cycle_state": "insufficient",
            "valid_observation_count": int(len(observations)),
            "cycle_strength": np.nan,
        }

    early = observations[
        observations["day_of_year"].le(EARLY_PEAK_END_DAY)
        & observations["ndvi_mean"].ge(MIN_PEAK_NDVI)
        & observations["vegetation_fraction"].ge(MIN_PEAK_VEGETATION_FRACTION)
    ]
    late = observations[
        observations["day_of_year"].ge(LATE_PEAK_START_DAY)
        & observations["ndvi_mean"].ge(MIN_PEAK_NDVI)
        & observations["vegetation_fraction"].ge(MIN_PEAK_VEGETATION_FRACTION)
    ]
    best_strength = 0.0
    double_cycle = False
    for early_row in early.itertuples(index=False):
        for late_row in late.itertuples(index=False):
            separation = (late_row.observation_date - early_row.observation_date).days
            if separation < MIN_PEAK_SEPARATION_DAYS:
                continue
            middle = observations[
                observations["observation_date"].gt(
                    early_row.observation_date + pd.Timedelta(days=7)
                )
                & observations["observation_date"].lt(
                    late_row.observation_date - pd.Timedelta(days=7)
                )
            ]
            if middle.empty:
                continue
            trough = middle.loc[middle["ndvi_mean"].idxmin()]
            drop = float(early_row.ndvi_mean - trough["ndvi_mean"])
            rebound = float(late_row.ndvi_mean - trough["ndvi_mean"])
            best_strength = max(best_strength, min(drop, rebound))
            trough_supported = (
                float(trough["ndvi_mean"]) <= MAX_TROUGH_NDVI
                or float(trough["bare_fraction"]) >= MIN_TROUGH_BARE_FRACTION
            )
            if drop >= MIN_DROP and rebound >= MIN_REBOUND and trough_supported:
                double_cycle = True
    return {
        "cycle_state": "double" if double_cycle else "single",
        "valid_observation_count": int(len(observations)),
        "cycle_strength": round(best_strength, 4),
    }


def summarize_type(frame: pd.DataFrame) -> dict[str, object]:
    counts = {
        class_name: int(count)
        for class_name, count in frame["crop_type_candidate"]
        .value_counts()
        .reindex(TYPE_COLORS, fill_value=0)
        .items()
    }
    return {
        "eligible_parcel_count": int(len(frame)),
        "class_counts": counts,
        "class_area_ha": {
            class_name: round(
                float(
                    frame.loc[
                        frame["crop_type_candidate"].eq(class_name), "area_official_m2"
                    ].sum()
                )
                / 10_000.0,
                2,
            )
            for class_name in TYPE_COLORS
        },
    }


def summarize_cycles(frame: pd.DataFrame) -> dict[str, object]:
    counts = {
        class_name: int(count)
        for class_name, count in frame["annual_cycle"]
        .value_counts()
        .reindex(CYCLE_COLORS, fill_value=0)
        .items()
    }
    return {"annual_parcel_count": int(len(frame)), "class_counts": counts}


def stable_sample(frame: pd.DataFrame, count: int, salt: str) -> pd.DataFrame:
    candidates = frame.copy()
    projected_centroids = candidates.to_crs(3857).geometry.centroid
    candidates["area_bin"] = pd.qcut(
        candidates["area_official_m2"].rank(method="first"), 4, labels=False
    )
    candidates["lon_bin"] = pd.qcut(
        projected_centroids.x.rank(method="first"), 3, labels=False
    )
    candidates["lat_bin"] = pd.qcut(
        projected_centroids.y.rank(method="first"), 3, labels=False
    )
    candidates["sample_hash"] = candidates["cadastre_code"].map(
        lambda code: hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()
    )
    candidates = candidates.sort_values(
        ["stage", "area_bin", "lon_bin", "lat_bin", "sample_hash"]
    )
    candidates["round"] = candidates.groupby(
        ["stage", "area_bin", "lon_bin", "lat_bin"], observed=True
    ).cumcount()
    return candidates.sort_values(["round", "sample_hash"]).head(count)


def build_review_sample(
    typed: gpd.GeoDataFrame,
    cycles: pd.DataFrame,
    observations: pd.DataFrame,
) -> dict[str, object]:
    review = typed.merge(
        cycles[["cadastre_code", "annual_cycle"]],
        on="cadastre_code",
        how="left",
        validate="1:1",
    )
    groups = {
        "annual_single_cycle": review[
            review["crop_type_candidate"].eq("annual")
            & review["annual_cycle"].eq("single_cycle")
        ],
        "annual_two_or_variable": review[
            review["crop_type_candidate"].eq("annual")
            & review["annual_cycle"].isin({"two_cycle_recurring", "variable_cycle"})
        ],
        "perennial": review[review["crop_type_candidate"].eq("perennial")],
        "mixed_profile": review[review["crop_type_candidate"].eq("mixed_profile")],
    }
    samples = []
    for group_name, candidates in groups.items():
        if len(candidates) < 30:
            raise ValueError(f"Not enough candidates for review group {group_name}")
        selected = stable_sample(candidates, 30, group_name)
        for row in selected.itertuples(index=False):
            label_point = row.geometry.representative_point()
            samples.append(
                {
                    "review_group": group_name,
                    "cadastre_code": str(row.cadastre_code),
                    "stage": str(row.stage),
                    "area_ha": round(float(row.area_official_m2) / 10_000.0, 4),
                    "centroid": [
                        round(float(label_point.x), 7),
                        round(float(label_point.y), 7),
                    ],
                    "crop_type": str(row.crop_type_candidate),
                    "annual_cycle": str(row.annual_cycle) if pd.notna(row.annual_cycle) else None,
                }
            )
    sample_codes = {sample["cadastre_code"] for sample in samples}
    series = observations[observations["cadastre_code"].isin(sample_codes)].copy()
    series = series.sort_values(["cadastre_code", "observation_date"])
    series_by_code = {
        str(code): [
            {
                "date": row.observation_date.strftime("%Y-%m-%d"),
                "ndvi": None if pd.isna(row.ndvi_mean) else round(float(row.ndvi_mean), 4),
                "ndmi": None if pd.isna(row.ndmi_mean) else round(float(row.ndmi_mean), 4),
                "bsi": None if pd.isna(row.bsi_mean) else round(float(row.bsi_mean), 4),
                "valid_fraction": round(float(row.valid_fraction), 4),
            }
            for row in group.itertuples(index=False)
        ]
        for code, group in series.groupby("cadastre_code", sort=False)
    }
    for sample in samples:
        sample["series"] = series_by_code.get(sample["cadastre_code"], [])
    return {
        "schema_version": 1,
        "status": "owner_review_only",
        "type_analysis_version": TYPE_ANALYSIS_VERSION,
        "cycle_analysis_version": CYCLE_ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(samples),
        "group_counts": {
            group_name: sum(sample["review_group"] == group_name for sample in samples)
            for group_name in groups
        },
        "records": samples,
    }


def build() -> dict[str, object]:
    seasonal_paths = {year: EO_DIR / f"seasonal_features_{year}.parquet" for year in YEARS}
    observation_paths = {
        year: EO_DIR / f"lower_hrazdan_parcel_eo_observations_{year}.parquet"
        for year in YEARS
    }
    required = [CURRENT_PARCELS_PATH, *seasonal_paths.values(), *observation_paths.values()]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing land-use v2 inputs: {missing}")

    parcels = gpd.read_parquet(CURRENT_PARCELS_PATH)[
        [
            "public_parcel_id",
            "cadastre_code",
            "area_official_m2",
            "stage",
            "household_agriculture",
            "road_excluded",
            "geometry",
        ]
    ]
    parcels["cadastre_code"] = parcels["cadastre_code"].astype(str)
    parcel_duplicate_count = int(parcels["cadastre_code"].duplicated().sum())
    parcel_null_geometry_count = int(parcels.geometry.isna().sum())
    parcel_invalid_geometry_count = int((~parcels.geometry.is_valid).sum())
    if len(parcels) != EXPECTED_PARCELS:
        raise ValueError(f"Unexpected immutable parcel count: {len(parcels)}")
    if parcel_duplicate_count:
        raise ValueError("Immutable cadastre has duplicate cadastral codes")
    if parcel_null_geometry_count or parcel_invalid_geometry_count:
        raise ValueError("Immutable cadastre has null or invalid geometry")
    if parcels.crs is None or parcels.crs.to_epsg() != 4326:
        raise ValueError(f"Unexpected immutable cadastre CRS: {parcels.crs}")
    eligible = parcels[
        ~parcels["household_agriculture"].astype(bool)
        & ~parcels["road_excluded"].astype(bool)
    ].copy()
    if len(eligible) != EXPECTED_ELIGIBLE:
        raise ValueError(f"Unexpected eligible parcel count: {len(eligible)}")
    eligible = eligible.rename(columns={"public_parcel_id": "parcel_id"})
    typed = gpd.GeoDataFrame(
        eligible[["parcel_id", "cadastre_code", "stage", "area_official_m2", "geometry"]],
        geometry="geometry",
        crs=parcels.crs,
    )
    unmatched_eligible_count = int(typed.geometry.isna().sum())
    if unmatched_eligible_count:
        raise ValueError(f"Eligible parcels missing immutable geometry: {unmatched_eligible_count}")
    eligible_codes = set(typed["cadastre_code"])

    yearly_type_frames = []
    for year, path in seasonal_paths.items():
        seasonal = pd.read_parquet(path)
        seasonal["cadastre_code"] = seasonal["cadastre_code"].astype(str)
        seasonal = seasonal[seasonal["cadastre_code"].isin(eligible_codes)].copy()
        seasonal["analysis_year"] = year
        yearly_type_frames.append(classify_yearly_type(seasonal))
    yearly_types = pd.concat(yearly_type_frames, ignore_index=True)
    state_counts = pd.crosstab(yearly_types["cadastre_code"], yearly_types["year_type"]).reindex(
        columns=["annual", "perennial", "mixed", "insufficient"], fill_value=0
    )
    year_codes = (
        yearly_types.sort_values(["cadastre_code", "analysis_year"])
        .groupby("cadastre_code")["year_type"]
        .apply(lambda values: [TYPE_STATE_CODES[str(value)] for value in values])
    )
    typed = typed.merge(state_counts.reset_index(), on="cadastre_code", how="left", validate="1:1")
    typed["profile_year_count"] = len(YEARS) - typed["insufficient"]
    typed["crop_type_year_codes"] = typed["cadastre_code"].map(year_codes)
    typed["crop_type_candidate"] = "mixed_profile"
    typed.loc[typed["profile_year_count"].lt(MIN_TYPE_PROFILE_YEARS), "crop_type_candidate"] = (
        "insufficient_observations"
    )
    typed.loc[
        typed["annual"].ge(MIN_TYPE_DOMINANT_YEARS) & typed["annual"].gt(typed["perennial"]),
        "crop_type_candidate",
    ] = "annual"
    typed.loc[
        typed["perennial"].ge(MIN_TYPE_DOMINANT_YEARS) & typed["perennial"].gt(typed["annual"]),
        "crop_type_candidate",
    ] = "perennial"
    typed["visual_review_required"] = True
    typed["public_release_approved"] = False

    annual_codes = set(typed.loc[typed["crop_type_candidate"].eq("annual"), "cadastre_code"])
    observation_frames = []
    cycle_year_frames = []
    for year, path in observation_paths.items():
        observations = pd.read_parquet(
            path,
            columns=[
                "cadastre_code",
                "observation_date",
                "quality_status",
                "valid_fraction",
                "ndvi_mean",
                "ndmi_mean",
                "bsi_mean",
                "vegetation_fraction",
                "bare_fraction",
            ],
        )
        observations["cadastre_code"] = observations["cadastre_code"].astype(str)
        observations = observations[observations["cadastre_code"].isin(eligible_codes)].copy()
        observations["observation_date"] = pd.to_datetime(observations["observation_date"])
        observation_frames.append(observations)
        annual_observations = observations[observations["cadastre_code"].isin(annual_codes)]
        evaluated_series = annual_observations.groupby(
            "cadastre_code", sort=False, observed=True
        ).apply(evaluate_cycle_year, include_groups=False)
        evaluated = pd.DataFrame(evaluated_series.tolist(), index=evaluated_series.index).reset_index()
        evaluated["analysis_year"] = year
        cycle_year_frames.append(evaluated)
    observations_all = pd.concat(observation_frames, ignore_index=True)
    cycle_years = pd.concat(cycle_year_frames, ignore_index=True)
    cycle_counts = pd.crosstab(cycle_years["cadastre_code"], cycle_years["cycle_state"]).reindex(
        columns=["single", "double", "insufficient"], fill_value=0
    )
    cycle_codes = (
        cycle_years.sort_values(["cadastre_code", "analysis_year"])
        .groupby("cadastre_code")["cycle_state"]
        .apply(lambda values: [CYCLE_STATE_CODES[str(value)] for value in values])
    )
    cycles = typed.loc[
        typed["crop_type_candidate"].eq("annual"),
        ["parcel_id", "cadastre_code", "stage", "area_official_m2"],
    ].merge(cycle_counts.reset_index(), on="cadastre_code", how="left", validate="1:1")
    cycles["assessed_year_count"] = len(YEARS) - cycles["insufficient"]
    cycles["annual_cycle_year_codes"] = cycles["cadastre_code"].map(cycle_codes)
    cycles["annual_cycle"] = "single_cycle"
    cycles.loc[cycles["double"].eq(1), "annual_cycle"] = "variable_cycle"
    cycles.loc[cycles["double"].ge(MIN_RECURRING_DOUBLE_YEARS), "annual_cycle"] = (
        "two_cycle_recurring"
    )
    cycles.loc[
        cycles["assessed_year_count"].lt(MIN_CYCLE_ASSESSABLE_YEARS), "annual_cycle"
    ] = "insufficient_observations"
    cycles["visual_review_required"] = True
    cycles["public_release_approved"] = False

    type_summaries = {"lower_hrazdan": summarize_type(typed)}
    cycle_summaries = {"lower_hrazdan": summarize_cycles(cycles)}
    for stage in ("stage_1", "stage_2"):
        type_summaries[stage] = summarize_type(typed[typed["stage"].eq(stage)])
        cycle_summaries[stage] = summarize_cycles(cycles[cycles["stage"].eq(stage)])

    type_columns = [
        "parcel_id",
        "cadastre_code",
        "stage",
        "crop_type_candidate",
        "profile_year_count",
        "crop_type_year_codes",
    ]
    type_output = typed[type_columns].sort_values("cadastre_code").reset_index(drop=True)
    type_payload = {
        "schema_version": 2,
        "analysis_version": TYPE_ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "draft_owner_review",
        "period": list(YEARS),
        "spatial_unit": "immutable_cadastral_parcel",
        "eligibility": "all non-household, non-road cadastral parcels in the observed zone",
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
        "class_colors": TYPE_COLORS,
        "year_state_codes": TYPE_STATE_CODES,
        "record_schema": type_columns,
        "records": type_output.values.tolist(),
        "summaries": type_summaries,
        "limitations": [
            "Classes are broad multi-season phenology candidates, not exact crops.",
            "Mixed profile is preserved instead of forcing a binary annual/perennial label.",
            "Household agriculture and roads are excluded from open-field crop typing.",
            "The 2026 incomplete season is not used.",
            "All classes remain draft results pending owner visual review.",
        ],
    }
    cycle_columns = [
        "parcel_id",
        "cadastre_code",
        "stage",
        "annual_cycle",
        "assessed_year_count",
        "annual_cycle_year_codes",
    ]
    cycle_output = cycles[cycle_columns].sort_values("cadastre_code").reset_index(drop=True)
    cycle_payload = {
        "schema_version": 1,
        "analysis_version": CYCLE_ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "draft_owner_review",
        "period": list(YEARS),
        "parent_class": "annual",
        "spatial_unit": "immutable_cadastral_parcel",
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
        "class_colors": CYCLE_COLORS,
        "year_state_codes": CYCLE_STATE_CODES,
        "record_schema": cycle_columns,
        "records": cycle_output.values.tolist(),
        "summaries": cycle_summaries,
        "limitations": [
            "The result identifies EO-observed cropping cycles, not crop species.",
            "Two recurring cycles require evidence in at least two completed seasons.",
            "One two-cycle season is reported as variable use.",
            "The 2026 incomplete season is not used.",
            "All classes remain draft results pending owner visual review.",
        ],
    }

    TYPE_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TYPE_OUTPUT_PATH.write_text(json.dumps(type_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    CYCLE_OUTPUT_PATH.write_text(json.dumps(cycle_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    typed.drop(columns="geometry").to_parquet(TYPE_FEATURE_PATH, index=False, compression="zstd")
    cycles.to_parquet(CYCLE_FEATURE_PATH, index=False, compression="zstd")

    review_sample = build_review_sample(typed, cycles, observations_all)
    REVIEW_SAMPLE_PATH.write_text(json.dumps(review_sample, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    quality = {
        "schema_version": 1,
        "type_analysis_version": TYPE_ANALYSIS_VERSION,
        "cycle_analysis_version": CYCLE_ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "current_parcels": {"path": str(CURRENT_PARCELS_PATH), "sha256": sha256(CURRENT_PARCELS_PATH)},
            "seasonal_features": {str(year): {"path": str(path), "sha256": sha256(path)} for year, path in seasonal_paths.items()},
            "observations": {str(year): {"path": str(path), "sha256": sha256(path)} for year, path in observation_paths.items()},
        },
        "type_rules": {
            "minimum_profile_years": MIN_TYPE_PROFILE_YEARS,
            "minimum_dominant_years": MIN_TYPE_DOMINANT_YEARS,
            "minimum_year_signal_count": MIN_YEAR_SIGNAL_COUNT,
            "minimum_year_signal_margin": MIN_YEAR_SIGNAL_MARGIN,
            "perennial_ndmi_mean_min": PERENNIAL_NDMI_MEAN_MIN,
            "perennial_ndmi_min_min": PERENNIAL_NDMI_MIN_MIN,
            "perennial_bsi_mean_max": PERENNIAL_BSI_MEAN_MAX,
            "annual_ndmi_mean_max": ANNUAL_NDMI_MEAN_MAX,
            "annual_ndmi_min_max": ANNUAL_NDMI_MIN_MAX,
            "annual_bsi_mean_min": ANNUAL_BSI_MEAN_MIN,
        },
        "cycle_rules": {
            "minimum_valid_observations": MIN_CYCLE_VALID_OBSERVATIONS,
            "minimum_assessable_years": MIN_CYCLE_ASSESSABLE_YEARS,
            "minimum_recurring_double_years": MIN_RECURRING_DOUBLE_YEARS,
            "minimum_window_observations": MIN_WINDOW_OBSERVATIONS,
        },
        "parcel_validation": {
            "canonical_parcel_count": int(len(parcels)),
            "eligible_parcel_count": int(len(typed)),
            "crs": f"EPSG:{parcels.crs.to_epsg()}",
            "crs_epsg": int(parcels.crs.to_epsg()),
            "duplicate_cadastre_code_count": parcel_duplicate_count,
            "null_geometry_count": parcel_null_geometry_count,
            "invalid_geometry_count": parcel_invalid_geometry_count,
            "unmatched_eligible_count": unmatched_eligible_count,
            "cadastral_geometry_modified": False,
            "official_area_modified": False,
        },
        "type_summaries": type_summaries,
        "cycle_summaries": cycle_summaries,
        "type_output": str(TYPE_OUTPUT_PATH),
        "type_output_sha256": sha256(TYPE_OUTPUT_PATH),
        "cycle_output": str(CYCLE_OUTPUT_PATH),
        "cycle_output_sha256": sha256(CYCLE_OUTPUT_PATH),
        "review_sample": str(REVIEW_SAMPLE_PATH),
        "review_sample_sha256": sha256(REVIEW_SAMPLE_PATH),
        "review_sample_count": int(review_sample["sample_count"]),
        "legacy_250m_grid_used": False,
        "visual_review_required": True,
        "public_release_approved": False,
    }
    QUALITY_PATH.write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return quality


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
