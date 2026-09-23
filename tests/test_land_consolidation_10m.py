from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wp_core.land_consolidation_10m import Policy, compare_season, mask_observations, target_mask
from run_land_consolidation_10m import qualifies


def profile(shift=0):
    days = np.arange(60, 335, 10)
    growth = np.maximum(0, np.sin((days - 70 - shift) / 240 * np.pi))
    return days, np.column_stack([.2 + .6 * growth, .1 + .5 * growth, -.1 + .4 * growth, .2 - .4 * growth])


def compare(a, b, shared=.05, support=None):
    days, _ = profile()
    s = np.ones(len(days)) if support is None else support
    return compare_season(days, a, b, s, s, shared, 2021, Policy())


def test_no_20m_support_does_not_fabricate_or_block_primary_match():
    _, a = profile()
    a[:, 2:] = np.nan
    result = compare(a, a.copy())
    assert result["similar"]
    assert all(v["state"] == "insufficient_context" for v in result["context_20m"].values())


def test_conflicting_20m_context_is_retained_separately():
    _, a = profile()
    b = a.copy()
    b[:, 2:] += .3
    result = compare(a, b)
    assert result["similar"]
    assert all(v["state"] == "different_mixed_context" for v in result["context_20m"].values())


def test_flat_profiles_and_large_gaps_do_not_confirm_similarity():
    _, a = profile()
    b = a.copy()
    b[5:14, :2] = np.nan
    assert compare(a, b)["reason"] == "insufficient_common_dates"
    flat = np.full_like(a, .2)
    assert compare(flat, flat)["reason"] == "weak_seasonal_contrast"


def test_phase_shift_is_not_matched_by_shifting_calendar():
    _, a = profile()
    _, b = profile(60)
    assert not compare(a, b)["similar"]


def test_per_date_contamination_bound_uses_valid_area():
    days, a = profile()
    assert compare(a, a, shared=.15)["similar"]
    result = compare(a, a, shared=.15, support=np.full(len(days), .6))
    assert not result["similar"]
    assert result["shared_pixel_dates_excluded"] == len(days)


@pytest.mark.parametrize("bad", [np.nan, .59, 1.1, -1])
def test_invalid_primary_support_is_not_coverage(bad):
    days, a = profile()
    assert not compare(a, a, support=np.full(len(days), bad))["similar"]


def test_index_specific_quality_does_not_depend_on_all_index_support():
    values = np.ones((2, 4))
    fractions = np.array([[.8, .8, 0., 0.], [.59, .8, 1., 1.]])
    masked = mask_observations(values, fractions, Policy())
    assert np.isfinite(masked[0, :2]).all()
    assert np.isnan(masked[0, 2:]).all()
    assert np.isnan(masked[1, 0])
    assert np.array_equal(values, np.ones((2, 4)))


def test_target_keeps_exact_support_population_and_exclusions():
    frame = pd.DataFrame({"screening_state": ["spatial_support_review"] * 10,
        "minimum_width_m": [11.] * 10, "pure_pixels_10m": [3] * 10,
        "pure_pixels_20m": [0] * 10, "activity_class": ["active"] * 10,
        "activity_state": ["ready"] * 10, "household": [False] * 10,
        "household_agriculture": [False] * 10, "road_excluded": [False] * 10,
        "road_excluded_release": [False] * 10, "mask_conflict": [False] * 10})
    frame.loc[1, "minimum_width_m"] = 9
    frame.loc[2, "pure_pixels_10m"] = 2
    frame.loc[3, "pure_pixels_20m"] = 1
    frame.loc[4, "activity_class"] = "partial"
    frame.loc[5, "activity_state"] = "review"
    frame.loc[6, "household"] = True
    frame.loc[7, "road_excluded"] = True
    frame.loc[8, "mask_conflict"] = True
    frame.loc[9, "screening_state"] = "overlap_review"
    assert target_mask(frame, Policy()).tolist() == [True] + [False] * 9


def test_five_hectares_and_three_common_years_are_still_required():
    assert qualifies(2, 5., 0b10101, Policy())
    assert not qualifies(2, 4.99999, 31, Policy())
    assert not qualifies(1, 8., 31, Policy())
    assert not qualifies(4, 20., 0b00011, Policy())


def test_duplicate_or_unordered_dates_raise():
    days, a = profile()
    days[1] = days[0]
    with pytest.raises(ValueError, match="unique and ordered"):
        compare_season(days, a, a, np.ones(len(days)), np.ones(len(days)), .05, 2021, Policy())


def test_loader_uses_native_primary_quality_and_actual_dates(monkeypatch):
    import run_land_consolidation_10m as runner
    days, values = profile()
    frame = pd.DataFrame({"internal_parcel_id": ["a"] * len(days),
                          "observation_date": pd.Timestamp("2021-01-01") + pd.to_timedelta(days - 1, unit="D")})
    for k, name in enumerate(runner.INDICES):
        frame[name + "_median"] = values[:, k]
        frame[name + "_valid_fraction"] = .9 if k < 2 else 0.
    def fake_read(path, columns, filters):
        assert "support" not in columns and "finite" not in columns
        assert filters == [("internal_parcel_id", "in", ["a"])]
        return frame[columns].copy()
    monkeypatch.setattr(runner.pd, "read_parquet", fake_read)
    series, counts = runner.load_series({"eo": "data/analysis/observation_screening/mock", "years": [2021]}, ["a"], Policy())
    actual_days, actual_values, support = series[2021]
    np.testing.assert_array_equal(actual_days, days)
    assert np.isfinite(actual_values[:, :, :2]).all()
    assert np.isnan(actual_values[:, :, 2:]).all()
    assert counts[2021]["rows"] == len(days)


def test_no_network_raster_grid_or_public_writes():
    root = Path(__file__).resolve().parents[1]
    source = "\n".join((root / f).read_text() for f in (
        "run_land_consolidation_10m.py", "wp_core/land_consolidation_10m.py"))
    for forbidden in ("requests.", "httpx.", "rasterio.", "grid_250", "setFeatureState", "openai."):
        assert forbidden not in source
    assert 'data/analysis/land_consolidation/consolidation_10m_' in source
    assert 'baseline.load_population' not in source
    assert 'baseline.verify()' in source
