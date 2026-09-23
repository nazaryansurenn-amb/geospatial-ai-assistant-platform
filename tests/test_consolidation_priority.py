import numpy as np
import pytest
from shapely.affinity import rotate
from shapely.geometry import MultiPolygon, box

from wp_core.consolidation_priority import StripPolicy, group_priority, qualifies_group, shape_metrics


def test_rotated_strip_metrics_and_geometry_immutability():
    geometry = rotate(box(0, 0, 300, 20), 31)
    before = geometry.wkb
    result = shape_metrics(geometry, StripPolicy())
    assert np.isclose(result["strip_length_m"], 300.)
    assert np.isclose(result["strip_width_m"], 20.)
    assert np.isclose(result["strip_elongation"], 15.)
    assert result["long_narrow"]
    assert geometry.wkb == before


@pytest.mark.parametrize("geometry", [box(0, 0, 100, 100), box(0, 0, 50, 5), box(0, 0, 300, 60)])
def test_compact_short_and_wide_are_not_strips(geometry):
    assert not shape_metrics(geometry, StripPolicy())["long_narrow"]


def test_disconnected_parts_are_not_one_strip():
    geometry = MultiPolygon([box(0, 0, 100, 10), box(150, 0, 250, 10)])
    assert not shape_metrics(geometry, StripPolicy())["long_narrow"]


def test_priority_uses_official_area_not_parcel_vote_count():
    result = group_priority([1000., 1000., 8000.], [True, True, False], StripPolicy())
    assert result["strip_parcel_count"] == 2
    assert result["strip_area_fraction"] == .2
    assert result["has_strip_members"] and not result["strip_priority"]
    assert group_priority([5000., 5000.], [True, False], StripPolicy())["strip_priority"]


def test_member_and_group_thresholds_are_inclusive_without_rounding():
    assert qualifies_group([10000., 10000., 10000.])
    assert not qualifies_group([10000.01, 10000., 10000.])
    assert not qualifies_group([9999.99, 10000., 10000.])
    assert not qualifies_group([30000.])
    assert not qualifies_group([np.nan, 10000., 10000., 10000.])


def test_shape_priority_does_not_exclude_valid_compact_group():
    areas = [10000.] * 3
    assert qualifies_group(areas)
    assert not group_priority(areas, [False] * 3, StripPolicy())["strip_priority"]


def test_latest_eligibility_expands_size_not_road_or_overlap_rules():
    import pandas as pd
    from wp_core.consolidation_joint_eo import eligible_mask
    frame = pd.DataFrame({"area_official_m2": [8454.16] * 3,
        "activity_class": ["active"] * 3, "activity_state": ["ready"] * 3,
        "screening_state": ["spatial_support_review", "mapped_barrier_review", "overlap_review"],
        **{c: [False] * 3 for c in ("household", "household_agriculture", "road_excluded", "road_excluded_release", "mask_conflict")}})
    assert eligible_mask(frame, 10000.).tolist() == [True, False, False]
    assert eligible_mask(frame, 5000.).tolist() == [False, False, False]
