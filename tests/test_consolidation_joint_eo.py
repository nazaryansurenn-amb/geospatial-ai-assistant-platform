import numpy as np
import pandas as pd

from wp_core.consolidation_joint_eo import compare_joint_season, eligible_mask, sampling_footprint
from wp_core.land_consolidation_10m import Policy, compare_season


def test_narrow_shared_pixel_parcels_remain_eligible_but_barriers_do_not():
    frame = pd.DataFrame({"area_official_m2": [1000., 5000., 5000.01, 3000., 3000., 3000.],
        "activity_class": ["active"] * 6, "activity_state": ["ready"] * 6,
        "screening_state": ["spatial_support_review"] * 3 + ["mapped_barrier_review", "overlap_review", "spatial_support_review"],
        "household": [False] * 6, "household_agriculture": [False] * 6,
        "road_excluded": [False] * 6, "road_excluded_release": [False] * 6,
        "mask_conflict": [False] * 5 + [True], "minimum_width_m": [3.] * 6,
        "pure_pixels_10m": [0] * 6, "pure_pixels_20m": [0] * 6})
    assert eligible_mask(frame).tolist() == [True, True, False, False, False, False]


def test_shared_pixel_veto_removed_but_actual_date_quality_is_retained():
    days = np.arange(60, 335, 10)
    growth = np.maximum(0, np.sin((days - 70) / 240 * np.pi))
    values = np.column_stack([.2 + .6 * growth, .1 + .5 * growth, -.1 + .4 * growth, .2 - .4 * growth])
    support = np.full(len(days), .8)
    policy = Policy(minimum_years=2)
    assert not compare_season(days, values, values, support, support, .9, 2021, policy)["similar"]
    joint = compare_joint_season(days, values, values, support, support, 2021, policy)
    assert joint["similar"]
    assert joint["evidence_mode"] == "joint_10m_not_independent_parcel_confirmation"
    support[:] = .59
    assert not compare_joint_season(days, values, values, support, support, 2021, policy)["similar"]
    support[:] = .8
    missing = values.copy()
    missing[4:18] = np.nan
    assert not compare_joint_season(days, values, missing, support, support, 2021, policy)["similar"]


def test_sampling_footprint_deduplicates_shared_pixels_without_claiming_independence():
    pixels = [(np.array([1, 2]), np.array([20., 50.])), (np.array([2, 3]), np.array([50., 80.]))]
    result = sampling_footprint([0, 1], pixels)
    assert result == {"unique_10m_footprint_pixels": 3, "parcel_pixel_memberships": 4, "pixels_shared_by_members": 1}
    assert "independent_observations" not in result
