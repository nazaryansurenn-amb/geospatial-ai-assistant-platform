import pandas as pd
import pytest

from wp_core.parcel_land_features import (
    build_seasonal_parcel_features,
    validate_seasonal_parcel_features,
)


def _observation(code: str, date: str, ndvi: float, vegetation: float, bare: float) -> dict:
    return {
        "cadastre_code": code,
        "observation_date": date,
        "scene_id": f"scene-{date}",
        "quality_status": "usable",
        "valid_fraction": 1.0,
        "ndvi_mean": ndvi,
        "ndmi_mean": ndvi / 2,
        "bsi_mean": -ndvi / 3,
        "ndwi_mean": -ndvi / 4,
        "vegetation_fraction": vegetation,
        "bare_fraction": bare,
        "surface_water_fraction": 0.0,
        "gap_days_from_previous": 10,
        "comparable_to_previous": True,
        "delta_ndvi": 0.1,
        "delta_ndmi": 0.05,
    }


def test_builds_neutral_seasonal_features_without_assigning_land_class() -> None:
    frame = pd.DataFrame(
        [
            _observation("04-001", "2026-04-10", 0.20, 0.05, 0.80),
            _observation("04-001", "2026-06-10", 0.55, 0.75, 0.05),
            _observation("04-001", "2026-08-10", 0.35, 0.30, 0.20),
            _observation("04-001", "2026-08-20", 0.25, 0.10, 0.55),
        ]
    )
    result = build_seasonal_parcel_features(frame, analysis_version="test-v1")
    row = result.iloc[0]
    assert row["feature_status"] == "seasonal_profile"
    assert row["ndvi_amplitude"] == pytest.approx(0.35)
    assert row["ndvi_early_mean"] == pytest.approx(0.20)
    assert row["ndvi_middle_mean"] == pytest.approx(0.55)
    assert row["ndvi_late_mean"] == pytest.approx(0.30)
    assert "land_activity_class" not in result.columns
    assert validate_seasonal_parcel_features(result)["parcel_count"] == 1


def test_limited_observation_status_is_kept_for_review() -> None:
    frame = pd.DataFrame([_observation("04-002", "2026-07-01", 0.4, 0.5, 0.0)])
    result = build_seasonal_parcel_features(frame, analysis_version="test-v1")
    assert result.iloc[0]["feature_status"] == "limited_observations"


def test_no_usable_observation_remains_explicit() -> None:
    row = _observation("04-003", "2026-07-01", 0.4, 0.5, 0.0)
    row["quality_status"] = "not_observed"
    result = build_seasonal_parcel_features(pd.DataFrame([row]), analysis_version="test-v1")
    assert result.iloc[0]["feature_status"] == "not_observed"
    assert pd.isna(result.iloc[0]["ndvi_amplitude"])
