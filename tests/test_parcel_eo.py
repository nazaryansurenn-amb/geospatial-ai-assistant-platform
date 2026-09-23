import numpy as np
import pandas as pd
import pytest

from wp_core.parcel_eo import (
    PARCEL_EO_SCHEMA_VERSION,
    add_gap_safe_temporal_features,
    aggregate_optical_scene_by_parcel,
    validate_parcel_eo_timeseries,
)
from wp_core.parcel_identity import internal_parcel_id
from scripts.build_lower_hrazdan_parcel_eo_timeseries import (
    _apply_fractional_sampling,
    _collection_for_year,
    _select_interval_scenes,
)


def test_copernicus_collection_supports_pre_2023_seasons() -> None:
    assert _collection_for_year(2021) == "sentinel-2-l2a"
    assert _collection_for_year(2022) == "sentinel-2-l2a"
    assert _collection_for_year(2023) == "sentinel-2-c1-l2a"


def test_scene_selection_prefers_spatial_completeness_before_cloud_cover() -> None:
    items = [
        {
            "id": "almost-cloudless-but-incomplete",
            "properties": {
                "datetime": "2022-07-05T08:00:00Z",
                "eo:cloud_cover": 0.01,
                "s2:nodata_pixel_percentage": 82.0,
            },
        },
        {
            "id": "moderately-cloudy-complete-scene",
            "properties": {
                "datetime": "2022-07-06T08:00:00Z",
                "eo:cloud_cover": 18.0,
                "s2:nodata_pixel_percentage": 0.0,
            },
        },
    ]

    selected = _select_interval_scenes(
        items,
        season_start="2022-07-01T00:00:00Z",
        interval_days=10,
    )

    assert [item["id"] for item in selected] == [
        "moderately-cloudy-complete-scene"
    ]


def test_aggregate_optical_scene_by_canonical_parcel() -> None:
    labels = np.array([[1, 1, 2], [1, 2, 0]], dtype="int32")
    parcel_pixels = np.array([3, 2], dtype="int32")
    red = np.full(labels.shape, 0.2, dtype="float32")
    green = np.full(labels.shape, 0.25, dtype="float32")
    blue = np.full(labels.shape, 0.15, dtype="float32")
    nir = np.array([[0.6, 0.5, 0.25], [0.7, 0.22, 0.0]], dtype="float32")
    swir = np.full(labels.shape, 0.3, dtype="float32")
    scl = np.array([[4, 4, 5], [4, 5, 0]], dtype="uint8")

    result = aggregate_optical_scene_by_parcel(
        cadastre_codes=["04-001", "04-002"],
        labels=labels,
        parcel_pixel_count=parcel_pixels,
        red=red,
        green=green,
        blue=blue,
        nir=nir,
        swir16=swir,
        scl=scl,
    )

    first = result.set_index("cadastre_code").loc["04-001"]
    second = result.set_index("cadastre_code").loc["04-002"]
    assert first["quality_status"] == "usable"
    assert first["vegetation_fraction"] == pytest.approx(1.0)
    assert second["bare_fraction"] == pytest.approx(1.0)
    assert first["ndvi_mean"] > second["ndvi_mean"]


def test_aggregate_accepts_valid_negative_l2a_surface_reflectance() -> None:
    result = aggregate_optical_scene_by_parcel(
        cadastre_codes=["04-2022"],
        labels=np.array([[1]], dtype="int32"),
        parcel_pixel_count=np.array([1], dtype="int32"),
        red=np.array([[-0.01]], dtype="float32"),
        green=np.array([[-0.02]], dtype="float32"),
        blue=np.array([[-0.05]], dtype="float32"),
        nir=np.array([[0.20]], dtype="float32"),
        swir16=np.array([[0.10]], dtype="float32"),
        scl=np.array([[4]], dtype="uint8"),
    )

    parcel = result.iloc[0]
    assert parcel["quality_status"] == "usable"
    assert parcel["valid_fraction"] == pytest.approx(1.0)
    assert parcel["ndvi_mean"] > 0.0


def test_fractional_sampling_preserves_small_parcel_metrics_as_review() -> None:
    table = aggregate_optical_scene_by_parcel(
        cadastre_codes=["04-small"],
        labels=np.array([[0, 0]], dtype="int32"),
        parcel_pixel_count=np.array([0], dtype="int32"),
        red=np.array([[0.2, 0.2]], dtype="float32"),
        green=np.array([[0.25, 0.25]], dtype="float32"),
        blue=np.array([[0.15, 0.15]], dtype="float32"),
        nir=np.array([[0.6, 0.3]], dtype="float32"),
        swir16=np.array([[0.3, 0.3]], dtype="float32"),
        scl=np.array([[4, 5]], dtype="uint8"),
    )
    result = _apply_fractional_sampling(
        table,
        plan={0: (np.array([0, 1]), np.array([75.0, 25.0]))},
        red=np.array([[0.2, 0.2]], dtype="float32"),
        green=np.array([[0.25, 0.25]], dtype="float32"),
        blue=np.array([[0.15, 0.15]], dtype="float32"),
        nir=np.array([[0.6, 0.3]], dtype="float32"),
        swir16=np.array([[0.3, 0.3]], dtype="float32"),
        scl=np.array([[4, 5]], dtype="uint8"),
    )

    parcel = result.iloc[0]
    assert parcel["sampling_method"] == "fractional_10m_overlap"
    assert parcel["spatial_support_m2"] == pytest.approx(100.0)
    assert parcel["valid_fraction"] == pytest.approx(1.0)
    assert parcel["vegetation_fraction"] == pytest.approx(0.75)
    assert parcel["bare_fraction"] == pytest.approx(0.25)
    assert parcel["quality_status"] == "small_parcel_review"


def test_temporal_deltas_do_not_cross_large_or_invalid_gaps() -> None:
    rows = []
    for date, status, ndvi in (
        ("2026-04-01", "usable", 0.30),
        ("2026-04-11", "usable", 0.45),
        ("2026-05-20", "usable", 0.60),
        ("2026-05-30", "partial", 0.70),
    ):
        rows.append(
            {
                "cadastre_code": "04-001",
                "observation_date": date,
                "scene_id": date,
                "quality_status": status,
                "ndvi_mean": ndvi,
                "ndmi_mean": ndvi / 2,
                "bsi_mean": -ndvi,
                "ndwi_mean": 0.0,
                "vegetation_fraction": 0.5,
                "bare_fraction": 0.2,
                "surface_water_fraction": 0.0,
            }
        )
    result = add_gap_safe_temporal_features(pd.DataFrame(rows), maximum_gap_days=21)
    assert bool(result.loc[1, "comparable_to_previous"])
    assert result.loc[1, "delta_ndvi"] == pytest.approx(0.15)
    assert not bool(result.loc[2, "comparable_to_previous"])
    assert pd.isna(result.loc[2, "delta_ndvi"])
    assert not bool(result.loc[3, "comparable_to_previous"])


def test_validate_parcel_eo_timeseries_rejects_public_by_default() -> None:
    frame = pd.DataFrame(
        {
            "schema_version": [PARCEL_EO_SCHEMA_VERSION],
            "analysis_version": ["test"],
            "internal_parcel_id": [internal_parcel_id("04-001")],
            "cadastre_code": ["04-001"],
            "parcel_geometry_version": ["geometry-v1"],
            "observation_date": ["2026-04-01"],
            "scene_id": ["scene-1"],
            "source_program": ["Copernicus Sentinel-2 L2A"],
            "access_provider": ["test"],
            "quality_status": ["usable"],
            "public_release_approved": [True],
            "ndvi_mean": [0.4],
            "ndmi_mean": [0.2],
            "bsi_mean": [-0.1],
            "ndwi_mean": [-0.2],
            "vegetation_fraction": [0.8],
            "bare_fraction": [0.1],
            "surface_water_fraction": [0.0],
        }
    )
    with pytest.raises(ValueError, match="non-public"):
        validate_parcel_eo_timeseries(frame)
