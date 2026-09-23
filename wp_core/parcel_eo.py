from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from wp_core.parcel_identity import attach_parcel_identity


PARCEL_EO_SCHEMA_VERSION = 2
MIN_VALID_SURFACE_REFLECTANCE = -0.1
MAX_VALID_SURFACE_REFLECTANCE = 1.6
PARCEL_EO_METRICS = (
    "ndvi_mean",
    "ndmi_mean",
    "bsi_mean",
    "ndwi_mean",
    "vegetation_fraction",
    "bare_fraction",
    "surface_water_fraction",
)


def _safe_index(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    result = np.full(numerator.shape, np.nan, dtype="float32")
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (np.abs(denominator) > 1e-6)
    np.divide(numerator, denominator, out=result, where=valid)
    return np.clip(result, -1.0, 1.0)


def _label_count(labels: np.ndarray, mask: np.ndarray, size: int) -> np.ndarray:
    selected = mask & (labels > 0)
    return np.bincount(labels[selected], minlength=size + 1)[1:]


def _label_mean(
    labels: np.ndarray,
    values: np.ndarray,
    mask: np.ndarray,
    size: int,
) -> np.ndarray:
    selected = mask & (labels > 0) & np.isfinite(values)
    counts = np.bincount(labels[selected], minlength=size + 1)[1:]
    sums = np.bincount(
        labels[selected],
        weights=values[selected],
        minlength=size + 1,
    )[1:]
    result = np.full(size, np.nan, dtype="float64")
    np.divide(sums, counts, out=result, where=counts > 0)
    return result


def _fraction(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    result = np.full(numerator.shape, np.nan, dtype="float64")
    np.divide(numerator, denominator, out=result, where=denominator > 0)
    return result


def aggregate_optical_scene_by_parcel(
    *,
    cadastre_codes: Sequence[str],
    labels: np.ndarray,
    parcel_pixel_count: np.ndarray,
    red: np.ndarray,
    green: np.ndarray,
    blue: np.ndarray,
    nir: np.ndarray,
    swir16: np.ndarray,
    scl: np.ndarray,
) -> pd.DataFrame:
    """Aggregate one Copernicus Sentinel-2 scene over canonical parcel labels.

    The labels are a temporary 10 m calculation grid. They never replace or alter the
    cadastral geometry, and no grid identifier is written to the result.
    """

    arrays = (red, green, blue, nir, swir16, scl)
    if any(array.shape != labels.shape for array in arrays):
        raise ValueError("All scene arrays and parcel labels must have the same shape")
    size = len(cadastre_codes)
    if parcel_pixel_count.shape != (size,):
        raise ValueError("parcel_pixel_count must contain one value per cadastral parcel")

    # Sentinel-2 L2A processing baselines can produce small negative surface
    # reflectance values after the documented radiometric offset is applied.
    reflectance_valid = np.logical_and.reduce(
        [
            np.isfinite(array)
            & (array >= MIN_VALID_SURFACE_REFLECTANCE)
            & (array <= MAX_VALID_SURFACE_REFLECTANCE)
            for array in (red, green, blue, nir, swir16)
        ]
    )
    valid = reflectance_valid & np.isin(scl.astype("int16"), (4, 5, 6, 7))
    ndvi = _safe_index(nir - red, nir + red)
    ndmi = _safe_index(nir - swir16, nir + swir16)
    bsi = _safe_index((swir16 + red) - (nir + blue), (swir16 + red) + (nir + blue))
    ndwi = _safe_index(green - nir, green + nir)

    valid_count = _label_count(labels, valid, size)
    vegetation_count = _label_count(
        labels,
        valid & (scl == 4) & (ndvi >= 0.30),
        size,
    )
    bare_count = _label_count(
        labels,
        valid & (scl == 5) & (ndvi <= 0.25),
        size,
    )
    water_count = _label_count(labels, valid & (scl == 6), size)
    valid_fraction = _fraction(valid_count, parcel_pixel_count)

    quality_status = np.full(size, "not_observed", dtype=object)
    quality_status[(valid_count > 0) & (valid_fraction < 0.25)] = "partial"
    quality_status[(valid_count > 0) & (valid_fraction >= 0.25)] = "usable"
    quality_status[parcel_pixel_count == 0] = "small_parcel_review"

    return pd.DataFrame(
        {
            "cadastre_code": [str(value) for value in cadastre_codes],
            "parcel_pixel_count": parcel_pixel_count.astype("int32"),
            "valid_pixel_count": valid_count.astype("int32"),
            "valid_fraction": np.round(valid_fraction, 4),
            "quality_status": quality_status,
            "ndvi_mean": np.round(_label_mean(labels, ndvi, valid, size), 4),
            "ndmi_mean": np.round(_label_mean(labels, ndmi, valid, size), 4),
            "bsi_mean": np.round(_label_mean(labels, bsi, valid, size), 4),
            "ndwi_mean": np.round(_label_mean(labels, ndwi, valid, size), 4),
            "vegetation_fraction": np.round(_fraction(vegetation_count, valid_count), 4),
            "bare_fraction": np.round(_fraction(bare_count, valid_count), 4),
            "surface_water_fraction": np.round(_fraction(water_count, valid_count), 4),
        }
    )


def add_gap_safe_temporal_features(
    observations: pd.DataFrame,
    *,
    maximum_gap_days: int = 21,
) -> pd.DataFrame:
    required = {
        "cadastre_code",
        "observation_date",
        "scene_id",
        "quality_status",
        *PARCEL_EO_METRICS,
    }
    missing = sorted(required.difference(observations.columns))
    if missing:
        raise ValueError(f"Missing parcel EO columns: {', '.join(missing)}")
    if maximum_gap_days < 1:
        raise ValueError("maximum_gap_days must be positive")

    result = attach_parcel_identity(observations)
    result["observation_date"] = pd.to_datetime(result["observation_date"], errors="raise")
    result = result.sort_values(["cadastre_code", "observation_date", "scene_id"]).reset_index(
        drop=True
    )
    grouped = result.groupby("cadastre_code", sort=False)
    previous_date = grouped["observation_date"].shift(1)
    result["gap_days_from_previous"] = (
        result["observation_date"] - previous_date
    ).dt.days.astype("Int64")
    previous_quality = grouped["quality_status"].shift(1)
    comparable = (
        result["quality_status"].eq("usable")
        & previous_quality.eq("usable")
        & result["gap_days_from_previous"].le(maximum_gap_days)
    )
    result["comparable_to_previous"] = comparable.fillna(False)
    for metric in ("ndvi_mean", "ndmi_mean", "bsi_mean"):
        previous = grouped[metric].shift(1)
        result[f"delta_{metric.removesuffix('_mean')}"] = (
            pd.to_numeric(result[metric], errors="coerce") - pd.to_numeric(previous, errors="coerce")
        ).where(result["comparable_to_previous"])
    return result


def validate_parcel_eo_timeseries(observations: pd.DataFrame) -> dict[str, Any]:
    required = {
        "schema_version",
        "analysis_version",
        "internal_parcel_id",
        "cadastre_code",
        "parcel_geometry_version",
        "observation_date",
        "scene_id",
        "source_program",
        "access_provider",
        "quality_status",
        "public_release_approved",
        *PARCEL_EO_METRICS,
    }
    missing = sorted(required.difference(observations.columns))
    if missing:
        raise ValueError(f"Missing parcel EO columns: {', '.join(missing)}")
    duplicate_count = int(
        observations.duplicated(
            ["internal_parcel_id", "observation_date", "scene_id"]
        ).sum()
    )
    invalid_quality = sorted(
        set(observations["quality_status"].dropna().astype(str)).difference(
            {"usable", "partial", "not_observed", "small_parcel_review"}
        )
    )
    if duplicate_count:
        raise ValueError("Parcel EO observations contain duplicate parcel/date/scene keys")
    if invalid_quality:
        raise ValueError(f"Unknown parcel EO quality status: {invalid_quality}")
    if observations["public_release_approved"].fillna(False).astype(bool).any():
        raise ValueError("New parcel EO observations must start as non-public review data")
    attach_parcel_identity(observations)
    return {
        "row_count": int(len(observations)),
        "parcel_count": int(observations["cadastre_code"].nunique()),
        "observation_date_count": int(observations["observation_date"].nunique()),
        "duplicate_key_count": duplicate_count,
        "quality_status_counts": {
            str(key): int(value)
            for key, value in observations["quality_status"].value_counts().items()
        },
    }
