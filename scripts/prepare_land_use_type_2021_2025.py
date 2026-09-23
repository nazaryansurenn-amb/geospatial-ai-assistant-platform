from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
ACTIVITY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_activity_2026_preview.json"
)
CURRENT_PARCELS_PATH = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "current_halo_v1"
    / "current_parcels.parquet"
)
SEASONAL_DIR = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "v5_full_halo_2021_2025"
)
OUTPUT_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_2021_2025_preview.json"
)
FEATURE_OUTPUT_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_2021_2025_features.parquet"
)
QUALITY_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_2021_2025_quality.json"
)

ANALYSIS_VERSION = "lower_hrazdan_crop_type_eo_rules_v1_2021_2025"
YEARS = tuple(range(2021, 2026))
EXPECTED_CURRENT_PARCELS = 43_984
EXPECTED_ELIGIBLE_PARCELS = 18_303
MIN_PROFILE_YEARS = 4
MIN_SIGNAL_COUNT = 5
MAX_OPPOSING_SIGNAL_COUNT = 2

CLASS_COLORS = {
    "annual": "#e7bd46",
    "perennial": "#1fa276",
    "crop_type_review": "#8296a5",
}

# Thresholds are internal deterministic screening rules. Public delivery contains
# only the resulting broad candidate class and never exposes these values.
PERENNIAL_SIGNALS = {
    "ndvi_p10_min": 0.38,
    "ndvi_p25_min": 0.45,
    "vegetation_fraction_mean_min": 0.60,
    "vegetation_observation_rate_min": 0.80,
    "ndvi_amplitude_max": 0.40,
    "persistent_bare_observation_rate_max": 0.10,
    "ndvi_late_mean_min": 0.50,
}
ANNUAL_SIGNALS = {
    "ndvi_amplitude_min": 0.48,
    "ndvi_p10_max": 0.30,
    "ndvi_p25_max": 0.36,
    "vegetation_fraction_mean_max": 0.50,
    "vegetation_observation_rate_max": 0.70,
    "persistent_bare_observation_rate_min": 0.10,
    "ndvi_upper_spread_min": 0.35,
}

FEATURE_COLUMNS = [
    "ndvi_p10",
    "ndvi_p25",
    "ndvi_p90",
    "ndvi_early_mean",
    "ndvi_middle_mean",
    "ndvi_late_mean",
    "ndvi_amplitude",
    "ndmi_mean",
    "ndmi_amplitude",
    "vegetation_fraction_mean",
    "vegetation_observation_rate",
    "persistent_bare_observation_rate",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def records(payload: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(payload["records"], columns=payload["record_schema"])


def perennial_signal_count(frame: pd.DataFrame) -> pd.Series:
    signals = pd.concat(
        [
            frame["ndvi_p10"].ge(PERENNIAL_SIGNALS["ndvi_p10_min"]),
            frame["ndvi_p25"].ge(PERENNIAL_SIGNALS["ndvi_p25_min"]),
            frame["vegetation_fraction_mean"].ge(
                PERENNIAL_SIGNALS["vegetation_fraction_mean_min"]
            ),
            frame["vegetation_observation_rate"].ge(
                PERENNIAL_SIGNALS["vegetation_observation_rate_min"]
            ),
            frame["ndvi_amplitude"].le(
                PERENNIAL_SIGNALS["ndvi_amplitude_max"]
            ),
            frame["persistent_bare_observation_rate"].le(
                PERENNIAL_SIGNALS["persistent_bare_observation_rate_max"]
            ),
            frame["ndvi_late_mean"].ge(
                PERENNIAL_SIGNALS["ndvi_late_mean_min"]
            ),
        ],
        axis=1,
    )
    return signals.sum(axis=1).astype("uint8")


def annual_signal_count(frame: pd.DataFrame) -> pd.Series:
    signals = pd.concat(
        [
            frame["ndvi_amplitude"].ge(ANNUAL_SIGNALS["ndvi_amplitude_min"]),
            frame["ndvi_p10"].le(ANNUAL_SIGNALS["ndvi_p10_max"]),
            frame["ndvi_p25"].le(ANNUAL_SIGNALS["ndvi_p25_max"]),
            frame["vegetation_fraction_mean"].le(
                ANNUAL_SIGNALS["vegetation_fraction_mean_max"]
            ),
            frame["vegetation_observation_rate"].le(
                ANNUAL_SIGNALS["vegetation_observation_rate_max"]
            ),
            frame["persistent_bare_observation_rate"].ge(
                ANNUAL_SIGNALS["persistent_bare_observation_rate_min"]
            ),
            (frame["ndvi_p90"] - frame["ndvi_p25"]).ge(
                ANNUAL_SIGNALS["ndvi_upper_spread_min"]
            ),
        ],
        axis=1,
    )
    return signals.sum(axis=1).astype("uint8")


def classify(features: pd.DataFrame) -> pd.DataFrame:
    result = features.copy()
    result["perennial_signal_count"] = perennial_signal_count(result)
    result["annual_signal_count"] = annual_signal_count(result)
    result["crop_type_candidate"] = "crop_type_review"

    perennial = (
        result["profile_year_count"].ge(MIN_PROFILE_YEARS)
        & result["perennial_signal_count"].ge(MIN_SIGNAL_COUNT)
        & result["annual_signal_count"].le(MAX_OPPOSING_SIGNAL_COUNT)
    )
    annual = (
        result["profile_year_count"].ge(MIN_PROFILE_YEARS)
        & result["annual_signal_count"].ge(MIN_SIGNAL_COUNT)
        & result["perennial_signal_count"].le(MAX_OPPOSING_SIGNAL_COUNT)
    )
    result.loc[perennial, "crop_type_candidate"] = "perennial"
    result.loc[annual, "crop_type_candidate"] = "annual"
    result["visual_review_required"] = True
    result["public_release_approved"] = False
    return result


def scope_summary(frame: pd.DataFrame) -> dict[str, object]:
    class_counts = {
        crop_type: int(count)
        for crop_type, count in frame["crop_type_candidate"]
        .value_counts()
        .reindex(CLASS_COLORS, fill_value=0)
        .items()
    }
    class_area_ha = {
        crop_type: round(
            float(
                frame.loc[
                    frame["crop_type_candidate"].eq(crop_type), "area_official_m2"
                ].sum()
            )
            / 10_000.0,
            2,
        )
        for crop_type in CLASS_COLORS
    }
    return {
        "eligible_parcel_count": int(len(frame)),
        "automatic_classified_count": int(
            frame["crop_type_candidate"].isin({"annual", "perennial"}).sum()
        ),
        "review_count": int(frame["crop_type_candidate"].eq("crop_type_review").sum()),
        "class_counts": class_counts,
        "class_area_ha": class_area_ha,
    }


def build() -> dict[str, object]:
    seasonal_paths = {
        year: SEASONAL_DIR / f"seasonal_features_{year}.parquet" for year in YEARS
    }
    required = [ACTIVITY_PATH, CURRENT_PARCELS_PATH, *seasonal_paths.values()]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing crop-type inputs: {missing}")

    activity_payload = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
    activity = records(activity_payload)
    activity["cadastre_code"] = activity["cadastre_code"].astype(str)
    if len(activity) != EXPECTED_CURRENT_PARCELS:
        raise ValueError(f"Unexpected activity coverage: {len(activity)}")
    if activity["cadastre_code"].duplicated().any():
        raise ValueError("Activity records contain duplicate cadastral codes")

    eligible = activity[
        activity["preview_state"].eq("ready")
        & ~activity["household_agriculture"].astype(bool)
        & ~activity["road_excluded"].astype(bool)
        & activity["activity_class"].isin({"active", "partial"})
    ][["parcel_id", "cadastre_code", "stage"]].copy()
    if len(eligible) != EXPECTED_ELIGIBLE_PARCELS:
        raise ValueError(f"Unexpected crop-type eligibility count: {len(eligible)}")

    parcels = pd.read_parquet(
        CURRENT_PARCELS_PATH,
        columns=["cadastre_code", "area_official_m2"],
    )
    parcels["cadastre_code"] = parcels["cadastre_code"].astype(str)
    eligible = eligible.merge(parcels, on="cadastre_code", how="left", validate="1:1")
    if eligible["area_official_m2"].isna().any():
        raise ValueError("Eligible parcels are missing official area")

    seasonal_frames = []
    for year, path in seasonal_paths.items():
        seasonal = pd.read_parquet(
            path,
            columns=["cadastre_code", "feature_status", *FEATURE_COLUMNS],
        )
        seasonal["cadastre_code"] = seasonal["cadastre_code"].astype(str)
        if len(seasonal) != EXPECTED_CURRENT_PARCELS:
            raise ValueError(f"Unexpected seasonal coverage for {year}: {len(seasonal)}")
        if seasonal["cadastre_code"].duplicated().any():
            raise ValueError(f"Duplicate seasonal cadastral codes for {year}")
        seasonal["analysis_year"] = year
        seasonal_frames.append(seasonal)

    seasonal = pd.concat(seasonal_frames, ignore_index=True)
    seasonal = seasonal[
        seasonal["cadastre_code"].isin(set(eligible["cadastre_code"]))
        & seasonal["feature_status"].eq("seasonal_profile")
    ]
    grouped = seasonal.groupby("cadastre_code", sort=False)
    medians = grouped[FEATURE_COLUMNS].median()
    medians["profile_year_count"] = grouped["analysis_year"].nunique()
    features = eligible.merge(
        medians.reset_index(), on="cadastre_code", how="left", validate="1:1"
    )
    features["profile_year_count"] = features["profile_year_count"].fillna(0).astype("uint8")
    result = classify(features)

    if result["cadastre_code"].duplicated().any():
        raise ValueError("Crop-type output contains duplicate cadastral codes")
    if set(result["crop_type_candidate"]) != set(CLASS_COLORS):
        raise ValueError("Crop-type output did not produce the complete expected class set")

    summaries = {"lower_hrazdan": scope_summary(result)}
    for stage in ("stage_1", "stage_2"):
        summaries[stage] = scope_summary(result[result["stage"].eq(stage)])

    output_columns = [
        "parcel_id",
        "cadastre_code",
        "stage",
        "crop_type_candidate",
        "profile_year_count",
    ]
    output = result[output_columns].sort_values("cadastre_code").reset_index(drop=True)
    payload = {
        "schema_version": 1,
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "draft_owner_review",
        "period": list(YEARS),
        "spatial_unit": "immutable_cadastral_parcel",
        "eligibility": "current active or partially active open-field parcels",
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
        "class_colors": CLASS_COLORS,
        "record_schema": output_columns,
        "records": output.values.tolist(),
        "summaries": summaries,
        "limitations": [
            "Classes describe broad EO crop-pattern candidates, not exact crops.",
            "Household agriculture and roads are excluded from this classification.",
            "Conflicting or weak seasonal evidence remains in the review class.",
            "All candidate classes remain draft results pending owner visual review.",
        ],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    result.to_parquet(FEATURE_OUTPUT_PATH, index=False, compression="zstd")
    quality = {
        "schema_version": 1,
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "activity": {"path": str(ACTIVITY_PATH), "sha256": sha256(ACTIVITY_PATH)},
            "current_parcels": {
                "path": str(CURRENT_PARCELS_PATH),
                "sha256": sha256(CURRENT_PARCELS_PATH),
            },
            "seasonal_features": {
                str(year): {"path": str(path), "sha256": sha256(path)}
                for year, path in seasonal_paths.items()
            },
        },
        "rules": {
            "minimum_profile_years": MIN_PROFILE_YEARS,
            "minimum_supporting_signals": MIN_SIGNAL_COUNT,
            "maximum_opposing_signals": MAX_OPPOSING_SIGNAL_COUNT,
            "perennial_signals": PERENNIAL_SIGNALS,
            "annual_signals": ANNUAL_SIGNALS,
        },
        "row_count": int(len(result)),
        "unique_parcel_count": int(result["cadastre_code"].nunique()),
        "null_class_count": int(result["crop_type_candidate"].isna().sum()),
        "summaries": summaries,
        "output": str(OUTPUT_PATH),
        "output_sha256": sha256(OUTPUT_PATH),
        "feature_output": str(FEATURE_OUTPUT_PATH),
        "feature_output_sha256": sha256(FEATURE_OUTPUT_PATH),
        "legacy_reference_used_for_classification": False,
        "legacy_250m_grid_used": False,
        "visual_review_required": True,
        "public_release_approved": False,
    }
    QUALITY_PATH.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return quality


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
