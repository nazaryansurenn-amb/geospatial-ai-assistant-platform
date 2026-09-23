from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from wp_core.parcel_identity import attach_parcel_identity


SEASONAL_FEATURE_SCHEMA_VERSION = 1


def _season_phase(month: pd.Series) -> pd.Series:
    return pd.Series(
        np.select(
            [month.le(5), month.between(6, 7), month.ge(8)],
            ["early", "middle", "late"],
            default="outside",
        ),
        index=month.index,
        dtype="string",
    )


def build_seasonal_parcel_features(
    observations: pd.DataFrame,
    *,
    analysis_version: str,
) -> pd.DataFrame:
    required = {
        "cadastre_code",
        "observation_date",
        "scene_id",
        "quality_status",
        "valid_fraction",
        "ndvi_mean",
        "ndmi_mean",
        "bsi_mean",
        "ndwi_mean",
        "vegetation_fraction",
        "bare_fraction",
        "surface_water_fraction",
        "gap_days_from_previous",
        "comparable_to_previous",
        "delta_ndvi",
        "delta_ndmi",
    }
    missing = sorted(required.difference(observations.columns))
    if missing:
        raise ValueError(f"Missing observation columns: {', '.join(missing)}")

    source = observations.copy()
    source["cadastre_code"] = source["cadastre_code"].astype(str)
    source["observation_date"] = pd.to_datetime(source["observation_date"], errors="raise")
    source["season_phase"] = _season_phase(source["observation_date"].dt.month)
    all_grouped = source.groupby("cadastre_code", sort=True)
    base = all_grouped.agg(
        observation_count=("scene_id", "nunique"),
        usable_observation_count=("quality_status", lambda values: int(values.eq("usable").sum())),
        partial_observation_count=("quality_status", lambda values: int(values.eq("partial").sum())),
        mean_valid_fraction=("valid_fraction", "mean"),
        maximum_gap_days=("gap_days_from_previous", "max"),
        comparable_pair_count=("comparable_to_previous", "sum"),
    )

    usable = source[source["quality_status"].eq("usable")].copy()
    if usable.empty:
        features = base.copy()
    else:
        grouped = usable.groupby("cadastre_code", sort=True)
        features = base.join(
            grouped.agg(
                ndvi_mean=("ndvi_mean", "mean"),
                ndvi_min=("ndvi_mean", "min"),
                ndvi_max=("ndvi_mean", "max"),
                ndmi_mean=("ndmi_mean", "mean"),
                ndmi_min=("ndmi_mean", "min"),
                ndmi_max=("ndmi_mean", "max"),
                bsi_mean=("bsi_mean", "mean"),
                ndwi_mean=("ndwi_mean", "mean"),
                vegetation_fraction_mean=("vegetation_fraction", "mean"),
                vegetation_fraction_max=("vegetation_fraction", "max"),
                bare_fraction_mean=("bare_fraction", "mean"),
                bare_fraction_max=("bare_fraction", "max"),
                surface_water_fraction_max=("surface_water_fraction", "max"),
                vegetation_observation_rate=(
                    "vegetation_fraction",
                    lambda values: float(values.ge(0.15).mean()),
                ),
                dominant_vegetation_observation_rate=(
                    "vegetation_fraction",
                    lambda values: float(values.ge(0.50).mean()),
                ),
                persistent_bare_observation_rate=(
                    "bare_fraction",
                    lambda values: float(values.ge(0.50).mean()),
                ),
                ndvi_positive_change_count=("delta_ndvi", lambda values: int(values.gt(0.05).sum())),
                ndvi_negative_change_count=("delta_ndvi", lambda values: int(values.lt(-0.05).sum())),
                ndmi_positive_change_count=("delta_ndmi", lambda values: int(values.gt(0.05).sum())),
                ndmi_negative_change_count=("delta_ndmi", lambda values: int(values.lt(-0.05).sum())),
            )
        )
        quantiles = grouped["ndvi_mean"].quantile([0.10, 0.25, 0.50, 0.75, 0.90]).unstack()
        quantiles.columns = ["ndvi_p10", "ndvi_p25", "ndvi_p50", "ndvi_p75", "ndvi_p90"]
        features = features.join(quantiles)
        phase = (
            usable[usable["season_phase"].ne("outside")]
            .groupby(["cadastre_code", "season_phase"])["ndvi_mean"]
            .mean()
            .unstack()
            .rename(
                columns={
                    "early": "ndvi_early_mean",
                    "middle": "ndvi_middle_mean",
                    "late": "ndvi_late_mean",
                }
            )
        )
        features = features.join(phase)

    for column in (
        "ndvi_min",
        "ndvi_max",
        "ndmi_min",
        "ndmi_max",
    ):
        if column not in features:
            features[column] = np.nan
    features["ndvi_amplitude"] = features["ndvi_max"] - features["ndvi_min"]
    features["ndmi_amplitude"] = features["ndmi_max"] - features["ndmi_min"]
    features["feature_status"] = np.select(
        [
            features["usable_observation_count"].eq(0),
            features["usable_observation_count"].lt(4),
        ],
        ["not_observed", "limited_observations"],
        default="seasonal_profile",
    )
    features = attach_parcel_identity(features.reset_index())
    features.insert(0, "schema_version", SEASONAL_FEATURE_SCHEMA_VERSION)
    features.insert(1, "analysis_version", analysis_version)
    numeric_columns = features.select_dtypes(include=["float32", "float64"]).columns
    features[numeric_columns] = features[numeric_columns].round(4)
    return features


def validate_seasonal_parcel_features(features: pd.DataFrame) -> dict[str, Any]:
    required = {
        "schema_version",
        "analysis_version",
        "internal_parcel_id",
        "cadastre_code",
        "observation_count",
        "usable_observation_count",
        "feature_status",
    }
    missing = sorted(required.difference(features.columns))
    if missing:
        raise ValueError(f"Missing seasonal feature columns: {', '.join(missing)}")
    duplicate_count = int(features["cadastre_code"].duplicated().sum())
    if duplicate_count:
        raise ValueError("Seasonal feature table contains duplicate cadastral parcels")
    attach_parcel_identity(features)
    invalid_status = sorted(
        set(features["feature_status"].astype(str)).difference(
            {"seasonal_profile", "limited_observations", "not_observed"}
        )
    )
    if invalid_status:
        raise ValueError(f"Unknown seasonal feature status: {invalid_status}")
    return {
        "row_count": int(len(features)),
        "parcel_count": int(features["cadastre_code"].nunique()),
        "feature_status_counts": {
            str(key): int(value)
            for key, value in features["feature_status"].value_counts().items()
        },
    }
