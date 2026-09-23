from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
CROP_TYPE_PATH = (
    PRODUCT_ROOT / "server_data" / "review" / "land_use_type_2021_2025_preview.json"
)
SEASONAL_DIR = (
    PRODUCT_ROOT
    / "data"
    / "analysis"
    / "parcel_eo"
    / "v5_full_halo_2021_2025"
)
OUTPUT_PATH = (
    PRODUCT_ROOT
    / "server_data"
    / "review"
    / "annual_cropping_intensity_2021_2025_preview.json"
)
FEATURE_OUTPUT_PATH = (
    PRODUCT_ROOT
    / "server_data"
    / "review"
    / "annual_cropping_intensity_2021_2025_features.parquet"
)
QUALITY_PATH = (
    PRODUCT_ROOT
    / "server_data"
    / "review"
    / "annual_cropping_intensity_2021_2025_quality.json"
)

ANALYSIS_VERSION = "lower_hrazdan_annual_cropping_intensity_v1_2021_2025"
YEARS = tuple(range(2021, 2026))
EXPECTED_ANNUAL_PARCELS = 8_212

MIN_VALID_OBSERVATIONS = 8
MIN_ASSESSABLE_YEARS = 3
MIN_RECURRING_DOUBLE_CYCLE_YEARS = 2
EARLY_PEAK_END_DAY = 201  # July 20
LATE_PEAK_START_DAY = 213  # August 1
MIN_PEAK_SEPARATION_DAYS = 40
MIN_PEAK_NDVI = 0.45
MIN_PEAK_VEGETATION_FRACTION = 0.35
MIN_DROP = 0.18
MIN_REBOUND = 0.18
MAX_TROUGH_NDVI = 0.38
MIN_TROUGH_BARE_FRACTION = 0.12

CLASS_COLORS = {
    "single_cycle_candidate": "#e7bd46",
    "two_cycle_candidate": "#e8752e",
    "annual_cycle_review": "#8296a5",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def records(payload: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(payload["records"], columns=payload["record_schema"])


def evaluate_year(group: pd.DataFrame) -> dict[str, object]:
    observations = group[
        group["quality_status"].eq("usable")
        & group["valid_fraction"].ge(0.35)
        & group["ndvi_mean"].notna()
    ].copy()
    observations = observations.sort_values("observation_date")
    if len(observations) < MIN_VALID_OBSERVATIONS:
        return {
            "assessable": False,
            "double_cycle": False,
            "valid_observation_count": int(len(observations)),
            "cycle_strength": np.nan,
        }

    observations["day_of_year"] = observations["observation_date"].dt.dayofyear
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
    if early.empty or late.empty:
        return {
            "assessable": True,
            "double_cycle": False,
            "valid_observation_count": int(len(observations)),
            "cycle_strength": 0.0,
        }

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
            strength = min(drop, rebound)
            best_strength = max(best_strength, strength)
            trough_supported = (
                float(trough["ndvi_mean"]) <= MAX_TROUGH_NDVI
                or float(trough["bare_fraction"]) >= MIN_TROUGH_BARE_FRACTION
            )
            if drop >= MIN_DROP and rebound >= MIN_REBOUND and trough_supported:
                double_cycle = True

    return {
        "assessable": True,
        "double_cycle": double_cycle,
        "valid_observation_count": int(len(observations)),
        "cycle_strength": round(best_strength, 4),
    }


def scope_summary(frame: pd.DataFrame) -> dict[str, object]:
    counts = {
        class_name: int(count)
        for class_name, count in frame["annual_cycle_candidate"]
        .value_counts()
        .reindex(CLASS_COLORS, fill_value=0)
        .items()
    }
    return {
        "annual_parcel_count": int(len(frame)),
        "class_counts": counts,
        "recurrent_two_cycle_count": int(
            frame["annual_cycle_candidate"].eq("two_cycle_candidate").sum()
        ),
        "review_count": int(
            frame["annual_cycle_candidate"].eq("annual_cycle_review").sum()
        ),
    }


def build() -> dict[str, object]:
    observation_paths = {
        year: SEASONAL_DIR / f"lower_hrazdan_parcel_eo_observations_{year}.parquet"
        for year in YEARS
    }
    required = [CROP_TYPE_PATH, *observation_paths.values()]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing annual-cycle inputs: {missing}")

    crop_type = json.loads(CROP_TYPE_PATH.read_text(encoding="utf-8"))
    crop_records = records(crop_type)
    crop_records["cadastre_code"] = crop_records["cadastre_code"].astype(str)
    annual = crop_records[
        crop_records["crop_type_candidate"].eq("annual")
    ][["parcel_id", "cadastre_code", "stage"]].copy()
    if len(annual) != EXPECTED_ANNUAL_PARCELS:
        raise ValueError(f"Unexpected annual parcel count: {len(annual)}")
    if annual["cadastre_code"].duplicated().any():
        raise ValueError("Annual crop candidates contain duplicate cadastral codes")

    annual_codes = set(annual["cadastre_code"])
    yearly_results: list[pd.DataFrame] = []
    yearly_quality: dict[str, object] = {}
    for year, path in observation_paths.items():
        observations = pd.read_parquet(
            path,
            columns=[
                "cadastre_code",
                "observation_date",
                "quality_status",
                "valid_fraction",
                "ndvi_mean",
                "vegetation_fraction",
                "bare_fraction",
            ],
        )
        observations["cadastre_code"] = observations["cadastre_code"].astype(str)
        observations = observations[observations["cadastre_code"].isin(annual_codes)]
        observations["observation_date"] = pd.to_datetime(
            observations["observation_date"]
        )
        evaluated_series = observations.groupby(
            "cadastre_code", sort=False, observed=True
        ).apply(evaluate_year, include_groups=False)
        evaluated = pd.DataFrame(
            evaluated_series.tolist(), index=evaluated_series.index
        ).reset_index()
        evaluated["analysis_year"] = year
        yearly_results.append(evaluated)
        yearly_quality[str(year)] = {
            "source_observation_count": int(len(observations)),
            "assessable_parcel_count": int(evaluated["assessable"].sum()),
            "double_cycle_evidence_count": int(evaluated["double_cycle"].sum()),
        }

    yearly = pd.concat(yearly_results, ignore_index=True)
    grouped = yearly.groupby("cadastre_code", sort=False)
    aggregate = grouped.agg(
        assessed_year_count=("assessable", "sum"),
        double_cycle_year_count=("double_cycle", "sum"),
        median_valid_observation_count=("valid_observation_count", "median"),
        maximum_cycle_strength=("cycle_strength", "max"),
    ).reset_index()
    double_years = (
        yearly[yearly["double_cycle"]]
        .groupby("cadastre_code")["analysis_year"]
        .apply(lambda values: [int(value) for value in sorted(values)])
    )
    aggregate["double_cycle_years"] = aggregate["cadastre_code"].map(double_years)
    aggregate["double_cycle_years"] = aggregate["double_cycle_years"].map(
        lambda value: value if isinstance(value, list) else []
    )

    result = annual.merge(aggregate, on="cadastre_code", how="left", validate="1:1")
    result["assessed_year_count"] = result["assessed_year_count"].fillna(0).astype("uint8")
    result["double_cycle_year_count"] = (
        result["double_cycle_year_count"].fillna(0).astype("uint8")
    )
    result["annual_cycle_candidate"] = "annual_cycle_review"
    recurring = result["double_cycle_year_count"].ge(
        MIN_RECURRING_DOUBLE_CYCLE_YEARS
    )
    single = (
        result["assessed_year_count"].ge(4)
        & result["double_cycle_year_count"].eq(0)
    )
    result.loc[recurring, "annual_cycle_candidate"] = "two_cycle_candidate"
    result.loc[single, "annual_cycle_candidate"] = "single_cycle_candidate"
    result["visual_review_required"] = True
    result["public_release_approved"] = False

    summaries = {"lower_hrazdan": scope_summary(result)}
    for stage in ("stage_1", "stage_2"):
        summaries[stage] = scope_summary(result[result["stage"].eq(stage)])

    output_columns = [
        "parcel_id",
        "cadastre_code",
        "stage",
        "annual_cycle_candidate",
        "assessed_year_count",
        "double_cycle_year_count",
        "double_cycle_years",
    ]
    output = result[output_columns].sort_values("cadastre_code").reset_index(drop=True)
    payload = {
        "schema_version": 1,
        "analysis_version": ANALYSIS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "draft_owner_review",
        "period": list(YEARS),
        "spatial_unit": "immutable_cadastral_parcel",
        "parent_class": "annual",
        "cadastral_geometry_modified": False,
        "legacy_250m_grid_used": False,
        "public_release_approved": False,
        "class_colors": CLASS_COLORS,
        "record_schema": output_columns,
        "records": output.values.tolist(),
        "summaries": summaries,
        "limitations": [
            "The result describes EO-observed cropping cycles, not exact crop species.",
            "A two-cycle candidate is not proof of a specific potato-bean rotation.",
            "One isolated two-peak season remains in the review class.",
            "All classes remain draft results pending owner visual review.",
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
            "crop_type": {"path": str(CROP_TYPE_PATH), "sha256": sha256(CROP_TYPE_PATH)},
            "observations": {
                str(year): {"path": str(path), "sha256": sha256(path)}
                for year, path in observation_paths.items()
            },
        },
        "rules": {
            "minimum_valid_observations": MIN_VALID_OBSERVATIONS,
            "minimum_assessable_years": MIN_ASSESSABLE_YEARS,
            "minimum_recurring_double_cycle_years": MIN_RECURRING_DOUBLE_CYCLE_YEARS,
            "early_peak_end_day": EARLY_PEAK_END_DAY,
            "late_peak_start_day": LATE_PEAK_START_DAY,
            "minimum_peak_separation_days": MIN_PEAK_SEPARATION_DAYS,
            "minimum_peak_ndvi": MIN_PEAK_NDVI,
            "minimum_peak_vegetation_fraction": MIN_PEAK_VEGETATION_FRACTION,
            "minimum_drop": MIN_DROP,
            "minimum_rebound": MIN_REBOUND,
            "maximum_trough_ndvi": MAX_TROUGH_NDVI,
            "minimum_trough_bare_fraction": MIN_TROUGH_BARE_FRACTION,
        },
        "row_count": int(len(result)),
        "unique_parcel_count": int(result["cadastre_code"].nunique()),
        "null_class_count": int(result["annual_cycle_candidate"].isna().sum()),
        "yearly_quality": yearly_quality,
        "summaries": summaries,
        "output": str(OUTPUT_PATH),
        "output_sha256": sha256(OUTPUT_PATH),
        "feature_output": str(FEATURE_OUTPUT_PATH),
        "feature_output_sha256": sha256(FEATURE_OUTPUT_PATH),
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
